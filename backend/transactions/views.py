import io
import json
import re
import base64
import csv
from datetime import datetime, timedelta, time
from decimal import Decimal, InvalidOperation

import pandas as pd

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Q
from django.views.decorators.http import require_GET, require_POST
from urllib.parse import urlencode

from core.models import (
    Account,
    Category,
    Currency,
    CurrencyRate,
    ExpenseLink,
    Project,
    Subcategory,
    Transaction,
    TransactionLinkGroup,
    TransactionLinkItem,
    UserPreferences,
)
from .forms import (
    BybitConnectionForm,
    TransactionForm,
    TransactionImportUploadForm,
    TinkoffImportAccountForm,
    TransactionImportMappingForm,
)
from .models import CategorizationRule, ImportSource, ImportedTransaction, TransactionImportSession
from .models import BybitConnection, BybitExternalEvent, BybitSyncRun
from .integrations.bybit.sync import sync_bybit_transaction_log
from .normalizers.base import BaseNormalizer
from .services.import_pipeline import (
    apply_manual_label,
    categorize_session,
    create_or_update_user_rule,
    finalize_session,
    normalize_rows,
    restore_duplicate_candidates,
)


class ImportRowError(Exception):
    """Raised when a row in the import file cannot be processed."""


def _normalize_string(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _read_import_file(uploaded_file, *, original_name=None, sheet_name=None):
    original_name = original_name or getattr(uploaded_file, 'name', 'upload')
    filename = original_name.lower()
    try:
        if filename.endswith('.csv'):
            data = uploaded_file.read()
            buffer = io.BytesIO(data)
            try:
                df = pd.read_csv(buffer, sep=None, engine='python')
            except Exception:
                buffer.seek(0)
                df = pd.read_csv(buffer, sep=';', engine='python')
        else:
            df = pd.read_excel(uploaded_file, sheet_name=sheet_name or 0)
    except Exception as exc:
        raise ImportRowError('Не удалось прочитать файл. Убедитесь, что формат поддерживается.') from exc

    df = df.replace(r'^\s*$', pd.NA, regex=True)
    df = df.dropna(how='all')
    df = df.fillna('')
    columns = [str(col).strip() for col in df.columns]
    df.columns = columns
    sample_rows = json.loads(df.head(10).to_json(orient='records', force_ascii=False, date_format='iso'))
    rows = json.loads(df.to_json(orient='records', force_ascii=False, date_format='iso'))
    return {
        'original_name': original_name,
        'columns': columns,
        'sample_rows': sample_rows,
        'rows': rows,
    }


AUTO_COLUMN_HINTS = {
    'column_date': ['дата', 'date', 'posting', 'operation'],
    'column_amount': ['сумма', 'amount', 'итог', 'total', 'debit', 'credit'],
    'column_currency': ['валют', 'currency', 'curr'],
    'column_account': ['счет', 'счёт', 'account', 'card', 'карта', 'номер карты'],
    'column_project': ['проект', 'project', 'client', 'контрагент', 'partner'],
    'column_category': ['категор', 'category', 'section'],
    'column_subcategory': ['подкат', 'subcategory', 'subcat'],
    'column_comment': ['коммент', 'comment', 'описан', 'description', 'details', 'назначение'],
    'column_type': ['тип', 'вид операц', 'operation type', 'доход', 'расход'],
}

KNOWN_CURRENCY_CODES = {
    'RUB', 'USD', 'EUR', 'KZT', 'KGS', 'GBP', 'CHF', 'JPY', 'CNY', 'UAH', 'BYN', 'CAD', 'AUD', 'NOK', 'SEK',
}
INCOME_VALUE_MARKERS = {'доход', 'поступление', 'пополнение', 'кредит', 'income', 'credit', 'приход'}
EXPENSE_VALUE_MARKERS = {'расход', 'списание', 'перевод', 'дебет', 'expense', 'debit', 'платеж', 'платёж'}


def _normalize_header(value):
    text = _normalize_string(value)
    return re.sub(r'[^a-z0-9а-яё]+', ' ', text.lower()).strip()


def _collect_column_samples(columns, sample_rows, limit=5):
    samples = {}
    for column in columns:
        values = []
        for row in sample_rows:
            cell = row.get(column)
            if cell is None:
                continue
            text = str(cell).strip()
            if text:
                values.append(text)
            if len(values) >= limit:
                break
        samples[column] = values
    return samples


def _looks_like_numeric(values):
    positive_hits = 0
    for value in values:
        text = _normalize_string(value)
        if not text:
            continue
        normalized = text.replace(' ', '').replace('\xa0', '').replace(',', '.')
        try:
            Decimal(normalized)
            positive_hits += 1
        except InvalidOperation:
            continue
    return positive_hits >= max(1, len(values) // 2)


def _looks_like_currency(values):
    hits = 0
    for value in values:
        token = _normalize_string(value).upper()
        if len(token) == 3 and token.isalpha():
            if token in KNOWN_CURRENCY_CODES:
                hits += 1
        elif token in {'РУБ', 'ДОЛЛАР', 'ЕВРО'}:
            hits += 1
    return hits >= max(1, len(values) // 2)


def _looks_like_type_markers(values):
    hits = 0
    for value in values:
        token = _normalize_string(value).lower()
        if not token:
            continue
        if token in INCOME_VALUE_MARKERS or token in EXPENSE_VALUE_MARKERS:
            hits += 1
    return hits >= max(1, len(values) // 2)


def _auto_detect_columns(columns, sample_rows):
    normalized_headers = {col: _normalize_header(col) for col in columns}
    samples = _collect_column_samples(columns, sample_rows or [])
    suggestions = {}
    used_columns = set()

    def pick(field, predicate=None):
        hints = AUTO_COLUMN_HINTS.get(field, [])
        for column in columns:
            if column in used_columns:
                continue
            header = normalized_headers.get(column, '')
            if any(hint in header for hint in hints):
                if predicate and not predicate(samples.get(column, [])):
                    continue
                suggestions[field] = column
                used_columns.add(column)
                return
        if predicate:
            for column in columns:
                if column in used_columns:
                    continue
                if predicate(samples.get(column, [])):
                    suggestions[field] = column
                    used_columns.add(column)
                    return

    pick('column_date')
    pick('column_amount', _looks_like_numeric)
    pick('column_currency', _looks_like_currency)
    pick('column_account')
    pick('column_project')
    pick('column_category')
    pick('column_subcategory')
    pick('column_comment')
    pick('column_type', _looks_like_type_markers)

    return suggestions, samples


def _infer_preset(columns):
    lower_cols = [col.lower() for col in columns]
    if 'дата операции' in lower_cols and 'сумма операции' in lower_cols and 'категория' in lower_cols and 'описание' in lower_cols:
        return 'tinkoff'
    if _is_alfa_columns(columns):
        return 'alfa'
    if _is_t_business_columns(columns):
        return 't_business'
    return 'other'


def _resolve_source_code(preset_code):
    code = _normalize_string(preset_code).lower()
    if code in {'', 'auto', 'other'}:
        return 'generic'
    if code in {'tinkoff', 'alfa', 't_business'}:
        return code
    return 'generic'


def _get_or_create_import_source(preset_code):
    source_code = _resolve_source_code(preset_code)
    display_map = {
        'generic': 'Generic file',
        'tinkoff': 'Tinkoff',
        'alfa': 'Alfa',
        't_business': 'T Business',
    }
    source, _ = ImportSource.objects.get_or_create(
        code=source_code,
        defaults={'name': display_map.get(source_code, source_code.title())},
    )
    return source


TINKOFF_REQUIRED_COLUMNS = ['дата операции', 'сумма операции', 'описание']
ALFA_REQUIRED_COLUMN_GROUPS = [
    {'дата операции', 'сумма', 'категория', 'описание операции'},
    {'operationdate', 'amount', 'category', 'merchant'},
]
T_BUSINESS_REQUIRED_COLUMNS = [
    'тип операции (пополнение/списание)',
    'дата проведения',
    'сумма в валюте счёта',
    'назначение платежа',
]


def _is_tinkoff_columns(columns):
    lower_cols = [str(col).strip().lower() for col in (columns or [])]
    return all(col in lower_cols for col in TINKOFF_REQUIRED_COLUMNS)


def _is_alfa_columns(columns):
    lower_cols = {str(col).strip().lower() for col in (columns or [])}
    return any(group.issubset(lower_cols) for group in ALFA_REQUIRED_COLUMN_GROUPS)


def _is_t_business_columns(columns):
    lower_cols = {str(col).strip().lower() for col in (columns or [])}
    return all(col in lower_cols for col in T_BUSINESS_REQUIRED_COLUMNS)


def _find_column_by_aliases(columns, aliases):
    normalized = {str(col).strip().lower(): col for col in columns}
    for alias in aliases:
        found = normalized.get(alias.lower())
        if found:
            return found
    return ''


def _build_tinkoff_mapping(session, cleaned_account_data):
    columns = session.columns or []
    mapping = {
        'column_date': _find_column_by_aliases(columns, ['Дата операции', 'Дата']),
        'column_amount': _find_column_by_aliases(columns, ['Сумма операции', 'Сумма']),
        'column_currency': _find_column_by_aliases(columns, ['Валюта операции', 'Валюта']),
        'column_comment': _find_column_by_aliases(columns, ['Описание', 'Комментарий']),
        'column_category': _find_column_by_aliases(columns, ['Категория']),
        'column_subcategory': '',
        'column_type': '',
        'default_currency': (cleaned_account_data.get('default_currency') or 'RUB').strip().upper(),
        'income_markers': '',
        'expense_markers': '',
        'default_account': cleaned_account_data.get('default_account'),
        'default_account_name': cleaned_account_data.get('default_account_name', ''),
    }
    return mapping


def _build_alfa_mapping(session, cleaned_account_data):
    columns = session.columns or []
    mapping = {
        'column_date': _find_column_by_aliases(columns, ['Дата операции', 'operationDate']),
        'column_amount': _find_column_by_aliases(columns, ['Сумма', 'amount']),
        'column_currency': _find_column_by_aliases(columns, ['Валюта', 'currency']),
        'column_comment': _find_column_by_aliases(columns, ['Описание операции', 'merchant']),
        'column_category': _find_column_by_aliases(columns, ['Категория', 'category']),
        'column_subcategory': '',
        'column_type': _find_column_by_aliases(columns, ['Тип', 'type']),
        'default_currency': (cleaned_account_data.get('default_currency') or 'RUB').strip().upper(),
        'income_markers': 'пополнение,зачисление,income,credit',
        'expense_markers': 'списание,расход,expense,debit',
        'default_account': cleaned_account_data.get('default_account'),
        'default_account_name': cleaned_account_data.get('default_account_name', ''),
    }
    return mapping


def _build_t_business_mapping(session, cleaned_account_data):
    columns = session.columns or []
    mapping = {
        'column_date': _find_column_by_aliases(columns, ['Дата проведения']),
        'column_amount': _find_column_by_aliases(columns, ['Сумма в валюте счёта', 'Сумма в валюте счета']),
        'column_currency': '',
        'column_comment': _find_column_by_aliases(columns, ['Назначение платежа']),
        'column_category': '',
        'column_subcategory': '',
        'column_type': _find_column_by_aliases(columns, ['Тип операции (пополнение/списание)']),
        'default_currency': (cleaned_account_data.get('default_currency') or 'RUB').strip().upper(),
        'income_markers': 'доход,поступление,пополнение,кредит,income,credit',
        'expense_markers': 'расход,списание,дебет,expense,debit',
        'default_account': cleaned_account_data.get('default_account'),
        'default_account_name': cleaned_account_data.get('default_account_name', ''),
    }
    return mapping


def _ensure_expense_link(user, project, category, subcategory):
    link, _ = ExpenseLink.objects.get_or_create(
        user=user,
        project=project,
        category=category,
        subcategory=subcategory,
        defaults={'status': 'active'},
    )
    return link


def _build_project_structure(user):
    projects = Project.objects.filter(user=user, status='active').order_by('name')
    project_data = []
    for project in projects:
        expense_links = ExpenseLink.objects.filter(
            user=user,
            project=project,
            status='active'
        ).select_related('category', 'subcategory')

        categories_map = {}
        for link in expense_links:
            category = link.category
            if category.status != 'active':
                continue
            if category.id not in categories_map:
                categories_map[category.id] = {
                    'id': category.id,
                    'name': category.name,
                    'subcategories': []
                }
            if link.subcategory and link.subcategory.status == 'active':
                categories_map[category.id]['subcategories'].append({
                    'id': link.subcategory.id,
                    'name': link.subcategory.name
                })
        project_data.append({
            'id': project.id,
            'name': project.name,
            'categories': list(categories_map.values())
        })
    return project_data


def _format_transaction_row(transaction):
    amount_value = float(transaction.amount)
    amount_abs = f"{abs(amount_value):,.2f}".replace(',', ' ').replace('.', ',')
    if amount_abs.endswith(',00'):
        amount_abs = amount_abs[:-3]
    is_income = amount_value >= 0
    localized_date = timezone.localtime(transaction.date)
    expense_link = transaction.expense_link
    subcategory = expense_link.subcategory
    return {
        'id': transaction.id,
        'date': {
            'display': localized_date.strftime('%d.%m.%Y %H:%M'),
            'sort': transaction.date.timestamp(),
        },
        'date_iso': localized_date.strftime('%Y-%m-%dT%H:%M'),
        'type_raw': 'income' if is_income else 'expense',
        'amount': {
            'display': f"<span class=\"amount-value\">{'+' if is_income else '-'}{amount_abs}</span>",
            'sort': amount_value,
        },
        'amount_raw': str(transaction.amount),
        'currency': transaction.currency,
        'currency_code': transaction.currency,
        'account': transaction.account.name,
        'account_id': transaction.account_id,
        'project': expense_link.project.name,
        'project_id': expense_link.project_id,
        'category': expense_link.category.name,
        'category_id': expense_link.category_id,
        'subcategory': subcategory.name if subcategory else '—',
        'subcategory_id': subcategory.id if subcategory else None,
        'expense_link_id': expense_link.id if expense_link else None,
        'comment': transaction.comment or '',
        'comment_raw': transaction.comment or '',
    }


def _format_amount_short(value):
    amount_value = float(value or 0)
    text = f"{abs(amount_value):,.2f}".replace(",", " ").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    sign = "+" if amount_value >= 0 else "-"
    return f"{sign}{text}", amount_value >= 0


def _rate_on_or_before(currency_code, rate_date):
    code = (currency_code or "").strip().upper()
    if code == "USDT":
        code = "USD"
    if code == "RUB":
        return Decimal("1")
    row = (
        CurrencyRate.objects.filter(currency=code, date__lte=rate_date)
        .order_by("-date")
        .values_list("amount", flat=True)
        .first()
    )
    return row


def _convert_transaction_amount(tx, target_currency):
    target = (target_currency or "").strip().upper()
    source = (tx.currency or "").strip().upper()
    if not target or source == target:
        return tx.amount
    rate_date = timezone.localtime(tx.date).date()
    source_rate = _rate_on_or_before(source, rate_date)
    target_rate = _rate_on_or_before(target, rate_date)
    if source_rate is None or target_rate in (None, Decimal("0")):
        return None
    return (tx.amount * source_rate) / target_rate


def _convert_amount_value(amount, currency_code, occurred_at, target_currency):
    target = (target_currency or "").strip().upper()
    source = (currency_code or "").strip().upper()
    if not target or source == target:
        return Decimal(amount)
    rate_date = timezone.localtime(occurred_at).date()
    source_rate = _rate_on_or_before(source, rate_date)
    target_rate = _rate_on_or_before(target, rate_date)
    if source_rate is None or target_rate in (None, Decimal("0")):
        return None
    return (Decimal(amount) * source_rate) / target_rate


def _subcategory_name_normalized(tx):
    if not tx.expense_link_id or not tx.expense_link.subcategory:
        return ""
    return " ".join((tx.expense_link.subcategory.name or "").strip().lower().split())


def _is_internal_transfer_tx(tx):
    return _subcategory_name_normalized(tx) == "перевод между счетами"


def _is_people_transfer_tx(tx):
    return _subcategory_name_normalized(tx) == "перевод между людьми"


def _build_export_rows(
    user,
    queryset,
    report_currency,
    *,
    exclude_internal_transfers=False,
    exclude_people_transfers=False,
    net_reimbursements=False,
):
    tx_list = list(queryset)
    tx_map = {tx.id: tx for tx in tx_list}
    adjusted_amounts = {tx.id: Decimal(tx.amount) for tx in tx_list}
    skip_ids = set()

    if exclude_internal_transfers:
        skip_ids.update(tx.id for tx in tx_list if _is_internal_transfer_tx(tx))
    if exclude_people_transfers:
        skip_ids.update(tx.id for tx in tx_list if _is_people_transfer_tx(tx))

    if net_reimbursements:
        tx_ids = {tx.id for tx in tx_list if tx.id not in skip_ids}
        reimbursement_groups = (
            TransactionLinkGroup.objects.filter(
                user=user,
                status=TransactionLinkGroup.Status.ACTIVE,
                link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
                items__transaction_id__in=tx_ids,
            )
            .distinct()
            .prefetch_related("items__transaction")
        )
        for group in reimbursement_groups:
            primary_ids = []
            offset_ids = []
            for item in group.items.all():
                if item.transaction_id not in tx_ids:
                    continue
                if item.role == TransactionLinkItem.Role.PRIMARY and item.transaction.amount < 0:
                    primary_ids.append(item.transaction_id)
                elif item.role == TransactionLinkItem.Role.OFFSET and item.transaction.amount > 0:
                    offset_ids.append(item.transaction_id)
            if not primary_ids or not offset_ids:
                continue

            currency_buckets = {}
            for tx_id in primary_ids + offset_ids:
                tx = tx_map.get(tx_id)
                if not tx:
                    continue
                currency_buckets.setdefault((tx.currency or "").upper(), {"primary": [], "offset": []})
                bucket_key = "primary" if tx_id in primary_ids else "offset"
                currency_buckets[(tx.currency or "").upper()][bucket_key].append(tx_id)

            for currency_code, bucket in currency_buckets.items():
                if not bucket["primary"] or not bucket["offset"]:
                    continue
                remaining = sum((adjusted_amounts[tx_id] for tx_id in bucket["offset"]), Decimal("0"))
                ordered_primary_ids = sorted(
                    bucket["primary"],
                    key=lambda tx_id: (tx_map[tx_id].date, tx_id),
                )
                for primary_id in ordered_primary_ids:
                    if remaining <= 0:
                        break
                    current_amount = adjusted_amounts.get(primary_id, Decimal("0"))
                    if current_amount >= 0:
                        continue
                    reduction = min(abs(current_amount), remaining)
                    adjusted_amounts[primary_id] = current_amount + reduction
                    remaining -= reduction
                skip_ids.update(bucket["offset"])

    export_rows = []
    for tx in tx_list:
        if tx.id in skip_ids:
            continue
        export_amount = adjusted_amounts.get(tx.id, Decimal(tx.amount))
        if net_reimbursements and export_amount == 0:
            continue
        converted_amount = _convert_amount_value(export_amount, tx.currency, tx.date, report_currency)
        export_rows.append({
            "tx": tx,
            "amount": export_amount,
            "converted_amount": converted_amount,
        })
    return export_rows


def _parse_split_items(raw_items, original_amount):
    if isinstance(raw_items, str):
        try:
            items = json.loads(raw_items)
        except json.JSONDecodeError as exc:
            raise ValueError("Не удалось разобрать части сплита.") from exc
    else:
        items = raw_items
    if not isinstance(items, list) or len(items) < 2:
        raise ValueError("Добавьте минимум две части.")

    parsed = []
    total = Decimal("0")
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Часть #{idx}: некорректный формат.")
        amount_raw = str(item.get("amount", "")).strip().replace(" ", "").replace(",", ".")
        if not amount_raw:
            raise ValueError(f"Часть #{idx}: укажите сумму.")
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation as exc:
            raise ValueError(f"Часть #{idx}: некорректная сумма.") from exc
        if amount == 0:
            raise ValueError(f"Часть #{idx}: сумма не может быть 0.")
        expense_link_raw = str(item.get("expense_link_id", "")).strip()
        if not expense_link_raw.isdigit():
            raise ValueError(f"Часть #{idx}: выберите категорию.")
        comment = _normalize_string(item.get("comment"))
        parsed.append(
            {
                "amount": amount,
                "expense_link_id": int(expense_link_raw),
                "comment": comment,
            }
        )
        total += amount

    if total != original_amount:
        total_text = str(total)
        original_text = str(original_amount)
        raise ValueError(f"Сумма частей ({total_text}) должна быть равна исходной сумме ({original_text}).")
    return parsed


def _review_display_value(imported_tx, mapping_key, fallback):
    mapping = imported_tx.session.metadata.get("last_mapping", {}) if isinstance(imported_tx.session.metadata, dict) else {}
    column_name = mapping.get(mapping_key)
    raw_payload = imported_tx.raw_payload or {}
    if column_name and column_name in raw_payload and str(raw_payload.get(column_name) or "").strip():
        return str(raw_payload.get(column_name)).strip()
    return fallback or "—"


@login_required
def transaction_import(request):
    session_id = request.GET.get('session')
    session = None
    mapping_form = None
    tinkoff_account_form = None
    upload_form = TransactionImportUploadForm()
    result = None
    preset_initial = {}
    preferences, _ = UserPreferences.objects.get_or_create(user=request.user)

    if session_id:
        session = get_object_or_404(TransactionImportSession, pk=session_id, user=request.user)
        preset_initial = BANK_PRESET_MAPPINGS.get(session.metadata.get('bank_preset', 'other'), {})

    if request.method == 'POST':
        step = request.POST.get('step', 'upload')
        if step == 'upload':
            upload_form = TransactionImportUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                uploaded = upload_form.cleaned_data['file']
                preset_choice = upload_form.cleaned_data.get('bank_preset') or 'auto'
                include_tinkoff_invest_rounding = bool(upload_form.cleaned_data.get('include_tinkoff_invest_rounding'))
                original_name = uploaded.name
                data = uploaded.read()
                preset_detected = preset_choice
                if original_name.lower().endswith(('.xlsx', '.xls')):
                    try:
                        workbook = pd.ExcelFile(io.BytesIO(data))
                        sheet_names = workbook.sheet_names
                    except Exception as exc:
                        upload_form.add_error('file', f'Не удалось прочитать Excel: {exc}')
                    else:
                        if not sheet_names:
                            upload_form.add_error('file', 'В книге нет листов.')
                        else:
                            if len(sheet_names) == 1:
                                selected_sheet = sheet_names[0]
                                try:
                                    parsed = _read_import_file(io.BytesIO(data), original_name=original_name, sheet_name=selected_sheet)
                                except ImportRowError as exc:
                                    upload_form.add_error('file', str(exc))
                                    parsed = None
                                if parsed:
                                    if preset_choice == 'auto':
                                        preset_detected = _infer_preset(parsed['columns'])
                                    if preset_choice == 'tinkoff' and not _is_tinkoff_columns(parsed['columns']):
                                        upload_form.add_error('file', 'Файл не похож на типовую выгрузку Тинькофф. Проверьте источник или выберите «Другое».')
                                    elif preset_choice == 'alfa' and not _is_alfa_columns(parsed['columns']):
                                        upload_form.add_error('file', 'Файл не похож на типовую выгрузку Альфа-Банка. Проверьте источник или выберите «Другое».')
                                    elif preset_choice == 't_business' and not _is_t_business_columns(parsed['columns']):
                                        upload_form.add_error('file', 'Файл не похож на типовую выгрузку Т Бизнес. Проверьте источник или выберите «Другое».')
                                    else:
                                        session = TransactionImportSession.objects.create(
                                            user=request.user,
                                            source=_get_or_create_import_source(preset_detected),
                                            original_name=f"{parsed['original_name']} — {selected_sheet}",
                                            columns=parsed['columns'],
                                            sample_rows=parsed['sample_rows'],
                                            rows=parsed['rows'],
                                            metadata={
                                                'bank_preset': preset_detected,
                                                'sheet_name': selected_sheet,
                                                'include_tinkoff_invest_rounding': include_tinkoff_invest_rounding and preset_detected == 'tinkoff',
                                            },
                                        )
                                        return redirect(f"{reverse('transactions:import')}?session={session.id}")
                            else:
                                if preset_choice == 'auto':
                                    try:
                                        preview_df = pd.read_excel(io.BytesIO(data), sheet_name=sheet_names[0])
                                        preview_cols = [str(col).strip() for col in preview_df.columns]
                                        preset_detected = _infer_preset(preview_cols)
                                    except Exception:
                                        preset_detected = 'other'
                                request.session['import_excel_pending'] = {
                                    'data': base64.b64encode(data).decode('ascii'),
                                    'original_name': original_name,
                                    'bank_preset': preset_detected,
                                    'include_tinkoff_invest_rounding': include_tinkoff_invest_rounding and preset_detected == 'tinkoff',
                                }
                                request.session['import_excel_sheets'] = sheet_names
                                return render(request, 'transactions/import.html', {
                                    'step': 'sheet',
                                    'sheet_names': sheet_names,
                                    'original_name': original_name,
                                })
                else:
                    try:
                        parsed = _read_import_file(io.BytesIO(data), original_name=original_name)
                    except ImportRowError as exc:
                        upload_form.add_error('file', str(exc))
                    else:
                        if preset_choice == 'auto':
                            preset_detected = _infer_preset(parsed['columns'])
                        if preset_choice == 'tinkoff' and not _is_tinkoff_columns(parsed['columns']):
                            upload_form.add_error('file', 'Файл не похож на типовую выгрузку Тинькофф. Проверьте источник или выберите «Другое».')
                            parsed = None
                        elif preset_choice == 'alfa' and not _is_alfa_columns(parsed['columns']):
                            upload_form.add_error('file', 'Файл не похож на типовую выгрузку Альфа-Банка. Проверьте источник или выберите «Другое».')
                            parsed = None
                        elif preset_choice == 't_business' and not _is_t_business_columns(parsed['columns']):
                            upload_form.add_error('file', 'Файл не похож на типовую выгрузку Т Бизнес. Проверьте источник или выберите «Другое».')
                            parsed = None
                        if not parsed:
                            return render(request, 'transactions/import.html', {
                                'step': 'upload',
                                'upload_form': upload_form,
                            })
                        session = TransactionImportSession.objects.create(
                            user=request.user,
                            source=_get_or_create_import_source(preset_detected),
                            original_name=parsed['original_name'],
                            columns=parsed['columns'],
                            sample_rows=parsed['sample_rows'],
                            rows=parsed['rows'],
                            metadata={
                                'bank_preset': preset_detected,
                                'include_tinkoff_invest_rounding': include_tinkoff_invest_rounding and preset_detected == 'tinkoff',
                            },
                        )
                        return redirect(f"{reverse('transactions:import')}?session={session.id}")
        elif step == 'sheet_select':
            pending = request.session.get('import_excel_pending')
            sheet_names = request.session.get('import_excel_sheets', [])
            sheet_name = request.POST.get('sheet')
            if not pending or sheet_name not in sheet_names:
                upload_form = TransactionImportUploadForm()
                request.session.pop('import_excel_pending', None)
                request.session.pop('import_excel_sheets', None)
                upload_form.add_error(None, 'Сессия выбора листа устарела. Загрузите файл заново.')
            else:
                file_bytes = base64.b64decode(pending['data'])
                try:
                    parsed = _read_import_file(io.BytesIO(file_bytes), original_name=pending['original_name'], sheet_name=sheet_name)
                except ImportRowError as exc:
                    upload_form = TransactionImportUploadForm()
                    upload_form.add_error('file', str(exc))
                else:
                    if pending.get('bank_preset') == 'tinkoff' and not _is_tinkoff_columns(parsed['columns']):
                        upload_form = TransactionImportUploadForm()
                        upload_form.add_error('file', 'Файл не похож на типовую выгрузку Тинькофф. Проверьте источник или выберите «Другое».')
                        request.session.pop('import_excel_pending', None)
                        request.session.pop('import_excel_sheets', None)
                        return render(request, 'transactions/import.html', {
                            'step': 'upload',
                            'upload_form': upload_form,
                        })
                    if pending.get('bank_preset') == 'alfa' and not _is_alfa_columns(parsed['columns']):
                        upload_form = TransactionImportUploadForm()
                        upload_form.add_error('file', 'Файл не похож на типовую выгрузку Альфа-Банка. Проверьте источник или выберите «Другое».')
                        request.session.pop('import_excel_pending', None)
                        request.session.pop('import_excel_sheets', None)
                        return render(request, 'transactions/import.html', {
                            'step': 'upload',
                            'upload_form': upload_form,
                        })
                    if pending.get('bank_preset') == 't_business' and not _is_t_business_columns(parsed['columns']):
                        upload_form = TransactionImportUploadForm()
                        upload_form.add_error('file', 'Файл не похож на типовую выгрузку Т Бизнес. Проверьте источник или выберите «Другое».')
                        request.session.pop('import_excel_pending', None)
                        request.session.pop('import_excel_sheets', None)
                        return render(request, 'transactions/import.html', {
                            'step': 'upload',
                            'upload_form': upload_form,
                        })
                    session = TransactionImportSession.objects.create(
                        user=request.user,
                        source=_get_or_create_import_source(pending.get('bank_preset', 'other')),
                        original_name=f"{parsed['original_name']} — {sheet_name}",
                        columns=parsed['columns'],
                        sample_rows=parsed['sample_rows'],
                        rows=parsed['rows'],
                        metadata={
                            'bank_preset': pending.get('bank_preset', 'other'),
                            'sheet_name': sheet_name,
                            'include_tinkoff_invest_rounding': bool(pending.get('include_tinkoff_invest_rounding')),
                        },
                    )
                    request.session.pop('import_excel_pending', None)
                    request.session.pop('import_excel_sheets', None)
                    return redirect(f"{reverse('transactions:import')}?session={session.id}")
        elif step == 'bank_account' and session and session.metadata.get('bank_preset') in {'tinkoff', 'alfa', 't_business'}:
            tinkoff_account_form = TinkoffImportAccountForm(request.POST, user=request.user)
            if tinkoff_account_form.is_valid():
                if session.metadata.get('bank_preset') == 'alfa':
                    mapping_data = _build_alfa_mapping(session, tinkoff_account_form.cleaned_data)
                elif session.metadata.get('bank_preset') == 't_business':
                    mapping_data = _build_t_business_mapping(session, tinkoff_account_form.cleaned_data)
                else:
                    mapping_data = _build_tinkoff_mapping(session, tinkoff_account_form.cleaned_data)
                result = normalize_rows(request.user, session, mapping_data)
                categorization_result = {"categorized": 0, "matched": 0, "needs_review": 0}
                if result.get("normalized", 0) > 0:
                    categorization_result = categorize_session(request.user, session, mapping_data=mapping_data)
                    result.update(categorization_result)
                mapping_snapshot = mapping_data.copy()
                account_obj = mapping_snapshot.pop('default_account', None)
                if account_obj:
                    mapping_snapshot['default_account_id'] = account_obj.id
                session.metadata['last_mapping'] = mapping_snapshot
                session.save(update_fields=['metadata'])
                has_errors = bool(result['errors'])
                if has_errors:
                    error_rows = [err.get('row_data') for err in result['errors'] if err.get('row_data')]
                    if error_rows:
                        session.rows = error_rows
                        session.sample_rows = error_rows[:10]
                        session.save(update_fields=['rows', 'sample_rows'])
                return render(request, 'transactions/import.html', {
                    'step': 'result',
                    'result': result,
                    'session': session,
                    'duplicate_candidates': session.metadata.get('duplicate_candidates', []) if isinstance(session.metadata, dict) else [],
                    'confirmed_duplicate_indices': session.metadata.get('confirmed_duplicate_indices', []) if isinstance(session.metadata, dict) else [],
                })
        elif step == 'mapping' and session:
            preset_initial = BANK_PRESET_MAPPINGS.get(session.metadata.get('bank_preset', 'other'), {})
            if preferences.default_account:
                preset_initial.setdefault('default_account', preferences.default_account)
                preset_initial.setdefault('default_account_name', preferences.default_account.name)
            if preferences.default_project:
                preset_initial.setdefault('default_project', preferences.default_project)
                preset_initial.setdefault('default_project_name', preferences.default_project.name)
            mapping_form = TransactionImportMappingForm(
                request.POST,
                columns=session.columns,
                user=request.user,
                preset_initial=preset_initial,
            )
            if mapping_form.is_valid():
                mapping_data = mapping_form.cleaned_data
                result = normalize_rows(request.user, session, mapping_data)
                categorization_result = {"categorized": 0, "matched": 0, "needs_review": 0}
                if result.get("normalized", 0) > 0:
                    categorization_result = categorize_session(request.user, session, mapping_data=mapping_data)
                    result.update(categorization_result)
                mapping_snapshot = mapping_data.copy()
                account_obj = mapping_snapshot.pop('default_account', None)
                project_obj = mapping_snapshot.pop('default_project', None)
                if account_obj:
                    mapping_snapshot['default_account_id'] = account_obj.id
                if project_obj:
                    mapping_snapshot['default_project_id'] = project_obj.id
                session.metadata['last_mapping'] = mapping_snapshot
                session.save(update_fields=['metadata'])
                has_errors = bool(result['errors'])
                if has_errors:
                    error_rows = [err.get('row_data') for err in result['errors'] if err.get('row_data')]
                    if error_rows:
                        session.rows = error_rows
                        session.sample_rows = error_rows[:10]
                        session.save(update_fields=['rows', 'sample_rows'])
                return render(request, 'transactions/import.html', {
                    'step': 'result',
                    'result': result,
                    'session': session,
                    'duplicate_candidates': session.metadata.get('duplicate_candidates', []) if isinstance(session.metadata, dict) else [],
                    'confirmed_duplicate_indices': session.metadata.get('confirmed_duplicate_indices', []) if isinstance(session.metadata, dict) else [],
                })
        elif step == 'restore_duplicates' and session:
            selected_indices = [int(item) for item in request.POST.getlist('duplicate_indices') if item.isdigit()]
            if not selected_indices:
                messages.error(request, 'Выберите хотя бы один дубль, который нужно считать реальной операцией.')
                return redirect(f"{reverse('transactions:import')}?session={session.id}")
            restore_result = restore_duplicate_candidates(request.user, session, selected_indices)
            mapping_data = session.metadata.get('last_mapping', {}) if isinstance(session.metadata, dict) else {}
            categorization_result = {"categorized": 0, "matched": 0, "needs_review": session.needs_review_rows}
            if restore_result.get("created", 0) > 0:
                categorization_result = categorize_session(request.user, session, mapping_data=mapping_data)
            result = {
                "normalized": ImportedTransaction.objects.filter(session=session, is_split_parent=False).count(),
                "duplicates": len(session.metadata.get('duplicate_candidates', [])) if isinstance(session.metadata, dict) else 0,
                "skipped": 0,
                "errors": [],
                "restored_duplicates": restore_result.get("created", 0),
                **categorization_result,
            }
            session.refresh_from_db()
            return render(request, 'transactions/import.html', {
                'step': 'result',
                'result': result,
                'session': session,
                'duplicate_candidates': session.metadata.get('duplicate_candidates', []) if isinstance(session.metadata, dict) else [],
                'confirmed_duplicate_indices': session.metadata.get('confirmed_duplicate_indices', []) if isinstance(session.metadata, dict) else [],
            })
        elif step == 'discard' and session:
            session.delete()
            return redirect('transactions:list')
        else:
            upload_form = TransactionImportUploadForm()

    if session and result is None:
        if session.metadata.get('bank_preset') in {'tinkoff', 'alfa', 't_business'}:
            if not tinkoff_account_form:
                tinkoff_account_form = TinkoffImportAccountForm(user=request.user)
            bank_preset = session.metadata.get('bank_preset')
            bank_label = {
                'tinkoff': 'Тинькофф',
                'alfa': 'Альфа-Банк',
                't_business': 'Т Бизнес',
            }.get(bank_preset, 'банка')
            return render(request, 'transactions/import.html', {
                'step': 'bank_account',
                'session': session,
                'tinkoff_account_form': tinkoff_account_form,
                'bank_label': bank_label,
            })
        auto_mapping, column_samples = _auto_detect_columns(session.columns, session.sample_rows)
        if not mapping_form:
            preset_initial = BANK_PRESET_MAPPINGS.get(session.metadata.get('bank_preset', 'other'), {})
            stored_mapping = session.metadata.get('last_mapping', {})
            initial_data = {'default_currency': 'RUB'}
            if preferences.default_account:
                initial_data.setdefault('default_account', preferences.default_account)
                initial_data.setdefault('default_account_name', preferences.default_account.name)
            if preferences.default_project:
                initial_data.setdefault('default_project', preferences.default_project)
                initial_data.setdefault('default_project_name', preferences.default_project.name)
            converted_mapping = stored_mapping.copy()
            account_id = converted_mapping.pop('default_account_id', None)
            project_id = converted_mapping.pop('default_project_id', None)
            if account_id:
                try:
                    converted_mapping['default_account'] = Account.objects.get(pk=account_id, user=request.user)
                except Account.DoesNotExist:
                    pass
            if project_id:
                try:
                    converted_mapping['default_project'] = Project.objects.get(pk=project_id, user=request.user)
                except Project.DoesNotExist:
                    pass
            initial_data.update(preset_initial)
            initial_data.update(converted_mapping)
            for field_name, column_name in auto_mapping.items():
                initial_data.setdefault(field_name, column_name)
            mapping_form = TransactionImportMappingForm(
                columns=session.columns,
                user=request.user,
                preset_initial=initial_data,
            )
        sample_matrix = [
            [row.get(col, '') for col in session.columns]
            for row in session.sample_rows
        ]
        return render(request, 'transactions/import.html', {
            'step': 'mapping',
            'mapping_form': mapping_form,
            'session': session,
            'sample_rows': sample_matrix,
            'columns': session.columns,
            'auto_mapping': auto_mapping,
            'column_samples': column_samples,
        })

    return render(request, 'transactions/import.html', {
        'step': 'upload',
        'upload_form': upload_form,
    })


@login_required
def transaction_import_sessions(request):
    sessions = TransactionImportSession.objects.filter(user=request.user).order_by('-created_at')
    return render(
        request,
        'transactions/import-sessions.html',
        {
            'sessions': sessions,
        },
    )


def _render_rule_conditions(conditions):
    field_names = {
        "merchant_norm": "мерчант",
        "description_norm": "описание",
        "source_category_norm": "категория источника",
        "direction": "направление",
        "currency": "валюта",
    }
    operator_names = {
        "equals": "=",
        "contains": "содержит",
        "regex": "regex",
    }
    rendered = []
    for condition in conditions or []:
        field = field_names.get(condition.get("field"), condition.get("field", ""))
        operator = operator_names.get(condition.get("operator"), condition.get("operator", ""))
        value = condition.get("value", "")
        rendered.append(f"{field} {operator} '{value}'")
    return "; ".join(rendered)


@login_required
def transaction_rules(request):
    source_code = (request.GET.get("source") or "").strip().lower()
    is_active = (request.GET.get("is_active") or "").strip().lower()

    rules_qs = (
        CategorizationRule.objects.filter(user=request.user)
        .select_related(
            "source",
            "action_expense_link__project",
            "action_expense_link__category",
            "action_expense_link__subcategory",
        )
        .order_by("-is_active", "source__name", "-updated_at", "-id")
    )
    if source_code:
        rules_qs = rules_qs.filter(source__code=source_code)
    if is_active == "1":
        rules_qs = rules_qs.filter(is_active=True)
    elif is_active == "0":
        rules_qs = rules_qs.filter(is_active=False)

    rules = []
    for rule in rules_qs:
        target = f"{rule.action_expense_link.project.name} -> {rule.action_expense_link.category.name}"
        if rule.action_expense_link.subcategory:
            target += f" -> {rule.action_expense_link.subcategory.name}"
        rules.append(
            {
                "id": rule.id,
                "name": rule.name,
                "source": rule.source.code,
                "is_active": rule.is_active,
                "priority": rule.priority,
                "confidence": rule.confidence,
                "conditions": _render_rule_conditions(rule.conditions),
                "target": target,
                "updated_at": rule.updated_at,
            }
        )

    sources = ImportSource.objects.filter(is_active=True).order_by("name")
    return render(
        request,
        "transactions/rules.html",
        {
            "rules": rules,
            "sources": sources,
            "source_code": source_code,
            "is_active": is_active,
        },
    )


@login_required
@require_POST
def transaction_rule_delete(request, rule_id):
    rule = get_object_or_404(CategorizationRule, pk=rule_id, user=request.user)
    rule.delete()
    messages.success(request, f"Правило #{rule_id} удалено.")

    params = {}
    source_code = (request.POST.get("source") or "").strip()
    is_active = (request.POST.get("is_active") or "").strip()
    if source_code:
        params["source"] = source_code
    if is_active:
        params["is_active"] = is_active
    if params:
        return redirect(f"{reverse('transactions:rules')}?{urlencode(params)}")
    return redirect("transactions:rules")


def _format_link_transaction_option(transaction):
    local_date = timezone.localtime(transaction.date).strftime("%d.%m.%Y %H:%M")
    amount_text, _ = _format_amount_short(transaction.amount)
    account_name = transaction.account.name
    category_name = transaction.expense_link.category.name if transaction.expense_link_id else "—"
    comment = (transaction.comment or "").strip()
    comment_short = comment[:40] + ("..." if len(comment) > 40 else "")
    return f"#{transaction.id} | {local_date} | {amount_text} {transaction.currency} | {account_name} | {category_name}" + (
        f" | {comment_short}" if comment_short else ""
    )


def _tx_category_path(transaction):
    if not transaction.expense_link_id:
        return "—"
    category = transaction.expense_link.category.name if transaction.expense_link and transaction.expense_link.category else "—"
    subcategory = (
        transaction.expense_link.subcategory.name
        if transaction.expense_link and transaction.expense_link.subcategory
        else ""
    )
    return f"{category} -> {subcategory}" if subcategory else category


def _tx_comment_short(transaction, limit=72):
    comment = (transaction.comment or "").strip()
    if not comment:
        return "—"
    return comment[:limit] + ("..." if len(comment) > limit else "")


def _serialize_link_tx(transaction):
    amount_text, is_income = _format_amount_short(transaction.amount)
    tx_kind = "Возврат/доход" if is_income else "Расход"
    return {
        "id": transaction.id,
        "date": timezone.localtime(transaction.date).strftime("%d.%m.%Y %H:%M"),
        "amount_text": amount_text,
        "amount_positive": is_income,
        "kind": tx_kind,
        "currency": transaction.currency,
        "account": transaction.account.name if transaction.account_id else "—",
        "category_path": _tx_category_path(transaction),
        "comment": _tx_comment_short(transaction),
    }


def _build_suggestion_data(base_tx, candidate_tx):
    day_gap = abs((base_tx.date.date() - candidate_tx.date.date()).days)
    amount_gap = abs(abs(candidate_tx.amount) - abs(base_tx.amount))
    same_currency = base_tx.currency == candidate_tx.currency
    tx_data = _serialize_link_tx(candidate_tx)
    tx_data["same_currency"] = same_currency
    tx_data["day_gap"] = day_gap
    tx_data["amount_gap"] = str(amount_gap)
    comment_compact = (tx_data["comment"] or "").strip()
    if len(comment_compact) > 24:
        comment_compact = comment_compact[:24] + "..."
    tx_data["full_label"] = (
        f"#{tx_data['id']} | {tx_data['date']} | {tx_data['amount_text']} {tx_data['currency']} | "
        f"{tx_data['account']} | {tx_data['kind']} | {tx_data['category_path']} | {tx_data['comment']}"
    )
    tx_data["label"] = (
        f"#{tx_data['id']} | {tx_data['date']} | {tx_data['amount_text']} {tx_data['currency']} | {comment_compact}"
    )
    return tx_data


def _normalize_name(value):
    return " ".join((value or "").strip().lower().split())


def _is_reimbursement_tx(transaction):
    sub = _normalize_name(
        transaction.expense_link.subcategory.name if transaction.expense_link_id and transaction.expense_link.subcategory else ""
    )
    return sub == "возврат"


def _is_internal_transfer_tx(transaction):
    sub = _normalize_name(
        transaction.expense_link.subcategory.name if transaction.expense_link_id and transaction.expense_link.subcategory else ""
    )
    return "перевод между счет" in sub


def _is_people_transfer_tx(transaction):
    sub = _normalize_name(
        transaction.expense_link.subcategory.name if transaction.expense_link_id and transaction.expense_link.subcategory else ""
    )
    return "перевод между люд" in sub


def _is_exchange_tx(transaction):
    category = _normalize_name(transaction.expense_link.category.name if transaction.expense_link_id else "")
    sub = _normalize_name(
        transaction.expense_link.subcategory.name if transaction.expense_link_id and transaction.expense_link.subcategory else ""
    )
    return "обмен" in sub or "обмен" in category


def _suggest_reimbursement_candidates(refund_tx, candidates, limit=20):
    ranked = []
    for tx in candidates:
        if tx.id == refund_tx.id:
            continue
        day_gap = abs((refund_tx.date.date() - tx.date.date()).days)
        same_currency = tx.currency == refund_tx.currency
        is_before = tx.date <= refund_tx.date
        amount_gap = abs(abs(tx.amount) - abs(refund_tx.amount))
        sign_rank = 0 if tx.amount < 0 else 1
        ranked.append(((sign_rank, 0 if same_currency else 1, 0 if is_before else 1, day_gap, amount_gap), tx))
    ranked.sort(key=lambda item: item[0])
    return [tx for _, tx in ranked[:limit]]


def _suggest_pair_candidates(base_tx, candidates, prefer_diff_currency=False, limit=8):
    ranked = []
    for tx in candidates:
        if tx.id == base_tx.id:
            continue
        if base_tx.amount * tx.amount >= 0:
            continue
        day_gap = abs((base_tx.date.date() - tx.date.date()).days)
        same_currency = tx.currency == base_tx.currency
        amount_gap = abs(abs(tx.amount) - abs(base_tx.amount))
        currency_rank = 0
        if prefer_diff_currency:
            currency_rank = 0 if not same_currency else 1
        else:
            currency_rank = 0 if same_currency else 1
        ranked.append(((currency_rank, day_gap, amount_gap), tx))
    ranked.sort(key=lambda item: item[0])
    return [tx for _, tx in ranked[:limit]]


def _create_link_pair(user, link_type, tx_a, tx_b, note=""):
    if TransactionLinkItem.objects.filter(transaction_id__in=[tx_a.id, tx_b.id]).exists():
        return None, "Одна из транзакций уже участвует в другой связке."

    if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
        return _upsert_reimbursement_group(user, [tx_a, tx_b], note)

    outgoing, incoming = tx_a, tx_b
    if outgoing.amount > incoming.amount:
        outgoing, incoming = incoming, outgoing
    if outgoing.amount >= 0 or incoming.amount <= 0:
        return None, "Для пары выберите исходящую и входящую транзакции."
    group = TransactionLinkGroup.objects.create(
        user=user,
        link_type=link_type,
        note=note,
    )
    TransactionLinkItem.objects.bulk_create(
        [
            TransactionLinkItem(group=group, transaction=outgoing, role=TransactionLinkItem.Role.OUTGOING),
            TransactionLinkItem(group=group, transaction=incoming, role=TransactionLinkItem.Role.INCOMING),
        ]
    )
    return group, None


def _create_reimbursement_group(user, transactions, note=""):
    if len(transactions) < 2:
        return None, "Для возврата выберите минимум 2 транзакции."
    tx_ids = [tx.id for tx in transactions]
    if len(set(tx_ids)) != len(tx_ids):
        return None, "Список транзакций содержит дубли."
    if TransactionLinkItem.objects.filter(transaction_id__in=tx_ids).exists():
        return None, "Одна из транзакций уже участвует в другой связке."

    primaries = [tx for tx in transactions if tx.amount < 0]
    offsets = [tx for tx in transactions if tx.amount > 0]
    if not primaries or not offsets:
        return None, "Для возврата нужны расход(ы) и входящий возврат."

    group = TransactionLinkGroup.objects.create(
        user=user,
        link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
        note=note,
    )
    items = []
    for tx in primaries:
        items.append(TransactionLinkItem(group=group, transaction=tx, role=TransactionLinkItem.Role.PRIMARY))
    for tx in offsets:
        items.append(TransactionLinkItem(group=group, transaction=tx, role=TransactionLinkItem.Role.OFFSET))
    TransactionLinkItem.objects.bulk_create(items)
    return group, None


def _upsert_reimbursement_group(user, transactions, note=""):
    if len(transactions) < 2:
        return None, "Для возврата выберите минимум 2 транзакции."

    tx_ids = [tx.id for tx in transactions]
    if len(set(tx_ids)) != len(tx_ids):
        return None, "Список транзакций содержит дубли."

    existing_items = list(
        TransactionLinkItem.objects.select_related("group").filter(transaction_id__in=tx_ids)
    )
    foreign_items = [item for item in existing_items if item.group.user_id != user.id]
    if foreign_items:
        return None, "Часть транзакций уже участвует в чужой связке."

    existing_groups = {item.group_id: item.group for item in existing_items}
    if len(existing_groups) > 1:
        return None, "Нельзя объединить транзакции из разных связок."

    target_group = next(iter(existing_groups.values()), None)
    if target_group and target_group.link_type != TransactionLinkGroup.LinkType.REIMBURSEMENT:
        return None, "Транзакция уже участвует в связке другого типа."

    if target_group is None:
        target_group = TransactionLinkGroup.objects.create(
            user=user,
            link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
            note=note,
        )
    elif note and not target_group.note:
        target_group.note = note
        target_group.save(update_fields=["note"])

    existing_tx_ids_in_group = set(
        TransactionLinkItem.objects.filter(group=target_group).values_list("transaction_id", flat=True)
    )

    new_items = []
    for tx in transactions:
        if tx.id in existing_tx_ids_in_group:
            continue
        role = TransactionLinkItem.Role.PRIMARY if tx.amount < 0 else TransactionLinkItem.Role.OFFSET
        if tx.amount == 0:
            return None, "Нулевая транзакция не может участвовать в возврате."
        new_items.append(TransactionLinkItem(group=target_group, transaction=tx, role=role))

    if new_items:
        TransactionLinkItem.objects.bulk_create(new_items)

    all_items = list(TransactionLinkItem.objects.select_related("transaction").filter(group=target_group))
    has_primary = any(item.role == TransactionLinkItem.Role.PRIMARY and item.transaction.amount < 0 for item in all_items)
    has_offset = any(item.role == TransactionLinkItem.Role.OFFSET and item.transaction.amount > 0 for item in all_items)
    if not has_primary or not has_offset:
        return None, "Для возврата нужны расход(ы) и входящий возврат."

    if not new_items:
        return None, "Все выбранные транзакции уже в этой связке."
    return target_group, None


def _build_links_redirect(request, fallback_type="", fallback_pending="", fallback_account="", fallback_days=60):
    type_value = (request.POST.get("type") or fallback_type or "").strip().lower()
    pending_value = (request.POST.get("pending") or fallback_pending or "").strip().lower()
    account_value = (request.POST.get("account") or fallback_account or "").strip()
    days_value = (request.POST.get("days") or str(fallback_days or 60)).strip()
    params = {}
    if type_value:
        params["type"] = type_value
    if pending_value:
        params["pending"] = pending_value
    if account_value:
        params["account"] = account_value
    if days_value:
        params["days"] = days_value
    if not params:
        return redirect("transactions:links")
    return redirect(f"{reverse('transactions:links')}?{urlencode(params)}")


@login_required
def transaction_links(request):
    user = request.user
    type_filter = (request.GET.get("type") or "").strip().lower()
    pending_filter = (request.GET.get("pending") or "").strip().lower()
    account_filter = (request.GET.get("account") or "").strip()
    days_filter_raw = (request.GET.get("days") or "").strip()
    try:
        days_filter = int(days_filter_raw) if days_filter_raw else 60
    except ValueError:
        days_filter = 60
    days_filter = max(7, min(days_filter, 365))
    cutoff_dt = timezone.now() - timedelta(days=days_filter)
    redirect_with_filters = lambda: _build_links_redirect(
        request,
        fallback_type=type_filter,
        fallback_pending=pending_filter,
        fallback_account=account_filter,
        fallback_days=days_filter,
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_link":
            link_type = (request.POST.get("link_type") or "").strip()
            first_id = request.POST.get("first_transaction_id")
            second_id = request.POST.get("second_transaction_id")
            second_ids_multi = [value for value in request.POST.getlist("second_transaction_ids") if value and value.isdigit()]
            note = (request.POST.get("note") or "").strip()
            if link_type not in {
                TransactionLinkGroup.LinkType.REIMBURSEMENT,
                TransactionLinkGroup.LinkType.TRANSFER_PAIR,
                TransactionLinkGroup.LinkType.EXCHANGE_PAIR,
            }:
                messages.error(request, "Выберите корректный тип связки.")
                return redirect_with_filters()
            if not (first_id and first_id.isdigit()):
                messages.error(request, "Выберите базовую транзакцию.")
                return redirect_with_filters()
            if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
                has_second = bool(second_ids_multi or (second_id and second_id.isdigit()))
                if not has_second:
                    messages.error(request, "Выберите минимум одну операцию для связки возврата.")
                    return redirect_with_filters()
            else:
                if not (second_id and second_id.isdigit()):
                    messages.error(request, "Выберите обе транзакции для связки.")
                    return redirect_with_filters()
            if first_id == second_id:
                messages.error(request, "Нельзя связать транзакцию саму с собой.")
                return redirect_with_filters()

            tx_ids = [int(first_id)]
            if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
                if second_ids_multi:
                    tx_ids.extend(int(value) for value in second_ids_multi)
                elif second_id and second_id.isdigit():
                    tx_ids.append(int(second_id))
            else:
                tx_ids.append(int(second_id))
            tx_qs = Transaction.objects.select_related("account", "expense_link__category").filter(
                account__user=user,
                is_split_parent=False,
                id__in=tx_ids,
            )
            tx_map = {tx.id: tx for tx in tx_qs}
            tx_a = tx_map.get(int(first_id))
            if not tx_a:
                messages.error(request, "Транзакции не найдены.")
                return redirect_with_filters()
            if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
                selected_other_ids = []
                if second_ids_multi:
                    selected_other_ids = [int(value) for value in second_ids_multi]
                elif second_id and second_id.isdigit():
                    selected_other_ids = [int(second_id)]
                selected_transactions = [tx_a] + [tx_map.get(tx_id) for tx_id in selected_other_ids if tx_map.get(tx_id)]
                if len(selected_transactions) != len(selected_other_ids) + 1:
                    messages.error(request, "Часть выбранных транзакций не найдена.")
                    return redirect_with_filters()
                group, error_msg = _upsert_reimbursement_group(user, selected_transactions, note)
            else:
                tx_b = tx_map.get(int(second_id))
                if not tx_b:
                    messages.error(request, "Транзакции не найдены.")
                    return redirect_with_filters()
                group, error_msg = _create_link_pair(user, link_type, tx_a, tx_b, note)
            if error_msg:
                messages.error(request, error_msg)
                return redirect_with_filters()
            messages.success(request, f"Связка #{group.id} создана.")
            return redirect_with_filters()

        if action == "create_link_suggested":
            link_type = (request.POST.get("link_type") or "").strip()
            first_id = request.POST.get("first_transaction_id")
            second_id = request.POST.get("second_transaction_id")
            second_ids_multi = [value for value in request.POST.getlist("second_transaction_ids") if value and value.isdigit()]
            note = (request.POST.get("note") or "").strip()
            if link_type not in {
                TransactionLinkGroup.LinkType.REIMBURSEMENT,
                TransactionLinkGroup.LinkType.TRANSFER_PAIR,
                TransactionLinkGroup.LinkType.EXCHANGE_PAIR,
            }:
                messages.error(request, "Некорректный тип связки.")
                return redirect_with_filters()
            if not (first_id and first_id.isdigit()):
                messages.error(request, "Выберите базовую транзакцию.")
                return redirect_with_filters()
            if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
                has_second = bool(second_ids_multi or (second_id and second_id.isdigit()))
                if not has_second:
                    messages.error(request, "Выберите минимум одну операцию для возврата.")
                    return redirect_with_filters()
            else:
                if not (second_id and second_id.isdigit()):
                    messages.error(request, "Выберите обе транзакции для связывания.")
                    return redirect_with_filters()
            tx_ids = [int(first_id)]
            if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
                if second_ids_multi:
                    tx_ids.extend(int(value) for value in second_ids_multi)
                elif second_id and second_id.isdigit():
                    tx_ids.append(int(second_id))
            else:
                tx_ids.append(int(second_id))
            tx_map = {
                tx.id: tx
                for tx in Transaction.objects.select_related("account", "expense_link__category").filter(
                    account__user=user,
                    is_split_parent=False,
                    id__in=tx_ids,
                )
            }
            tx_a = tx_map.get(int(first_id))
            if not tx_a:
                messages.error(request, "Транзакции не найдены.")
                return redirect_with_filters()
            if link_type == TransactionLinkGroup.LinkType.REIMBURSEMENT:
                selected_other_ids = []
                if second_ids_multi:
                    selected_other_ids = [int(value) for value in second_ids_multi]
                elif second_id and second_id.isdigit():
                    selected_other_ids = [int(second_id)]
                selected_transactions = [tx_a] + [tx_map.get(tx_id) for tx_id in selected_other_ids if tx_map.get(tx_id)]
                if len(selected_transactions) != len(selected_other_ids) + 1:
                    messages.error(request, "Часть выбранных транзакций не найдена.")
                    return redirect_with_filters()
                group, error_msg = _upsert_reimbursement_group(user, selected_transactions, note)
            else:
                tx_b = tx_map.get(int(second_id))
                if not tx_b:
                    messages.error(request, "Транзакции не найдены.")
                    return redirect_with_filters()
                group, error_msg = _create_link_pair(user, link_type, tx_a, tx_b, note)
            if error_msg:
                messages.error(request, error_msg)
                return redirect_with_filters()
            messages.success(request, f"Связка #{group.id} создана по рекомендации.")
            return redirect_with_filters()

        if action == "delete_link":
            group_id = request.POST.get("group_id")
            if not group_id or not group_id.isdigit():
                messages.error(request, "Некорректный идентификатор связки.")
                return redirect_with_filters()
            group = get_object_or_404(TransactionLinkGroup, pk=int(group_id), user=user)
            group.delete()
            messages.success(request, f"Связка #{group_id} удалена.")
            return redirect_with_filters()

    groups_qs = TransactionLinkGroup.objects.filter(user=user).prefetch_related(
        "items__transaction__account",
        "items__transaction__expense_link__category",
        "items__transaction__expense_link__subcategory",
    )
    if type_filter in {
        TransactionLinkGroup.LinkType.REIMBURSEMENT,
        TransactionLinkGroup.LinkType.TRANSFER_PAIR,
        TransactionLinkGroup.LinkType.EXCHANGE_PAIR,
    }:
        groups_qs = groups_qs.filter(link_type=type_filter)
    groups_qs = groups_qs.order_by("-id")

    linked_tx_ids = set(
        TransactionLinkItem.objects.filter(group__user=user).values_list("transaction_id", flat=True)
    )
    base_unlinked_qs = (
        Transaction.objects.filter(account__user=user, date__gte=cutoff_dt)
        .filter(is_split_parent=False)
        .exclude(id__in=linked_tx_ids)
        .select_related("account", "expense_link__category", "expense_link__subcategory")
    )
    if account_filter and account_filter.isdigit():
        base_unlinked_qs = base_unlinked_qs.filter(account_id=int(account_filter))

    tx_candidates = (
        base_unlinked_qs
        .exclude(id__in=linked_tx_ids)
        .order_by("-date", "-id")[:400]
    )
    tx_options = [
        {
            "id": tx.id,
            "label": _format_link_transaction_option(tx),
        }
        for tx in tx_candidates
    ]

    unlinked_all = list(base_unlinked_qs.order_by("-date", "-id"))
    unlinked_negatives = [tx for tx in unlinked_all if tx.amount < 0]
    unlinked_transfers = [tx for tx in unlinked_all if _is_internal_transfer_tx(tx)]
    unlinked_people_transfers = [tx for tx in unlinked_all if _is_people_transfer_tx(tx)]
    reimbursement_primary_qs = Transaction.objects.filter(
        account__user=user,
        is_split_parent=False,
        date__gte=cutoff_dt,
        link_items__group__user=user,
        link_items__group__status=TransactionLinkGroup.Status.ACTIVE,
        link_items__group__link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
        link_items__role=TransactionLinkItem.Role.PRIMARY,
    ).select_related("account", "expense_link__category", "expense_link__subcategory")
    if account_filter and account_filter.isdigit():
        reimbursement_primary_qs = reimbursement_primary_qs.filter(account_id=int(account_filter))
    reimbursement_primary_by_id = {tx.id: tx for tx in reimbursement_primary_qs}
    unlinked_ids = {item.id for item in unlinked_all}
    reimbursement_candidates = unlinked_all + [
        tx for tx_id, tx in reimbursement_primary_by_id.items() if tx_id not in unlinked_ids
    ]

    pending_reimbursements = []
    for tx in unlinked_all:
        if not _is_reimbursement_tx(tx):
            continue
        if tx.amount <= 0:
            continue
        suggestions = _suggest_reimbursement_candidates(tx, reimbursement_candidates, limit=300)
        suggestion_items = [_build_suggestion_data(tx, candidate) for candidate in suggestions]
        pending_reimbursements.append(
            {
                "tx": _serialize_link_tx(tx),
                "suggestions": suggestion_items,
                "suggestion_type": TransactionLinkGroup.LinkType.REIMBURSEMENT,
            }
        )

    pending_transfers = []
    for tx in unlinked_transfers:
        suggestions = _suggest_pair_candidates(tx, unlinked_transfers, prefer_diff_currency=False, limit=6)
        if not suggestions:
            continue
        suggestion_items = [_build_suggestion_data(tx, candidate) for candidate in suggestions]
        pending_transfers.append(
            {
                "tx": _serialize_link_tx(tx),
                "suggestions": suggestion_items,
                "suggestion_type": TransactionLinkGroup.LinkType.TRANSFER_PAIR,
            }
        )

    pending_people_transfers = []
    for tx in unlinked_people_transfers:
        suggestions = _suggest_pair_candidates(tx, unlinked_people_transfers, prefer_diff_currency=False, limit=6)
        if not suggestions:
            continue
        suggestion_items = [_build_suggestion_data(tx, candidate) for candidate in suggestions]
        pending_people_transfers.append(
            {
                "tx": _serialize_link_tx(tx),
                "suggestions": suggestion_items,
                "suggestion_type": TransactionLinkGroup.LinkType.TRANSFER_PAIR,
            }
        )

    if pending_filter == "reimbursement":
        pending_transfers = []
        pending_people_transfers = []
    elif pending_filter == "transfer":
        pending_reimbursements = []
        pending_people_transfers = []
    elif pending_filter == "people_transfer":
        pending_reimbursements = []
        pending_transfers = []

    groups = []
    for group in groups_qs:
        group_items = []
        for item in group.items.all():
            tx = item.transaction
            amount_text, _ = _format_amount_short(tx.amount)
            subcategory_name = tx.expense_link.subcategory.name if tx.expense_link and tx.expense_link.subcategory else ""
            category_path = (
                f"{tx.expense_link.category.name} -> {subcategory_name}"
                if subcategory_name
                else tx.expense_link.category.name
            )
            kind = "Доход" if tx.amount > 0 else "Расход"
            group_items.append(
                {
                    "role_key": item.role,
                    "role": item.get_role_display(),
                    "tx_id": item.transaction_id,
                    "date": timezone.localtime(tx.date),
                    "amount_text": amount_text,
                    "currency": tx.currency,
                    "account": tx.account.name,
                    "category_path": category_path,
                    "kind": kind,
                    "comment": (tx.comment or "").strip() or "—",
                }
            )
        groups.append(
            {
                "id": group.id,
                "link_type": group.get_link_type_display(),
                "note": group.note,
                "created_at": group.created_at,
                "items": group_items,
            }
        )

    return render(
        request,
        "transactions/links.html",
        {
            "groups": groups,
            "tx_options": tx_options,
            "type_filter": type_filter,
            "pending_filter": pending_filter,
            "days_filter": days_filter,
            "account_filter": account_filter,
            "accounts": Account.objects.filter(user=user, status="active").order_by("name"),
            "pending_reimbursements": pending_reimbursements[:120],
            "pending_transfers": pending_transfers[:120],
            "pending_people_transfers": pending_people_transfers[:120],
            "link_type_choices": [
                (TransactionLinkGroup.LinkType.REIMBURSEMENT, "Возврат"),
                (TransactionLinkGroup.LinkType.TRANSFER_PAIR, "Перевод между счетами"),
                (TransactionLinkGroup.LinkType.EXCHANGE_PAIR, "Обмен"),
            ],
        },
    )


@login_required
@require_GET
def transaction_links_lookup(request):
    user = request.user
    query = (request.GET.get("q") or "").strip()
    mode = (request.GET.get("mode") or "pair").strip().lower()
    base_id_raw = (request.GET.get("base_id") or "").strip()

    if len(query) < 2:
        return JsonResponse({"results": []})

    linked_tx_ids = set(
        TransactionLinkItem.objects.filter(group__user=user).values_list("transaction_id", flat=True)
    )

    qs = Transaction.objects.filter(account__user=user, is_split_parent=False).select_related(
        "account", "expense_link__category", "expense_link__subcategory"
    )
    if base_id_raw.isdigit():
        qs = qs.exclude(id=int(base_id_raw))

    if mode == "reimbursement":
        reimbursement_primary_ids = set(
            TransactionLinkItem.objects.filter(
                group__user=user,
                group__status=TransactionLinkGroup.Status.ACTIVE,
                group__link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
                role=TransactionLinkItem.Role.PRIMARY,
            ).values_list("transaction_id", flat=True)
        )
        qs = qs.filter(Q(id__in=reimbursement_primary_ids) | ~Q(id__in=linked_tx_ids))
    else:
        qs = qs.exclude(id__in=linked_tx_ids)

    search_q = (
        Q(comment__icontains=query)
        | Q(account__name__icontains=query)
        | Q(expense_link__category__name__icontains=query)
        | Q(expense_link__subcategory__name__icontains=query)
        | Q(currency__icontains=query)
    )
    if query.isdigit():
        search_q = search_q | Q(id=int(query))
    qs = qs.filter(search_q).order_by("-date", "-id")[:200]

    results = []
    for tx in qs:
        results.append(
            {
                "id": tx.id,
                "label": _format_link_transaction_option(tx),
            }
        )
    return JsonResponse({"results": results})

@login_required
def transaction_import_review(request):
    session_id = request.GET.get("session") or request.POST.get("session")
    if not session_id:
        return redirect("transactions:import")
    session = get_object_or_404(TransactionImportSession, pk=session_id, user=request.user)

    description_filter = (request.GET.get("description") or request.POST.get("description") or "").strip()
    source_category_filter = (request.GET.get("source_category") or request.POST.get("source_category") or "").strip()

    review_qs = ImportedTransaction.objects.filter(
        session=session,
        user=request.user,
        final_transaction__isnull=True,
        is_split_parent=False,
    ).order_by("-occurred_at", "id")
    if description_filter:
        review_qs = review_qs.filter(description_norm__icontains=description_filter)
    if source_category_filter:
        review_qs = review_qs.filter(source_category_norm__icontains=source_category_filter)

    def _is_ajax_request():
        return request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _redirect_review():
        params = {"session": session.id}
        if description_filter:
            params["description"] = description_filter
        if source_category_filter:
            params["source_category"] = source_category_filter
        return redirect(f"{reverse('transactions:review')}?{urlencode(params)}")

    def _error_response(message, status=400):
        if _is_ajax_request():
            return JsonResponse({"ok": False, "message": message}, status=status)
        messages.error(request, message)
        return _redirect_review()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "split_transaction":
            single_id = request.POST.get("single_transaction_id", "")
            if not single_id.isdigit():
                return _error_response("Не выбрана транзакция для разделения.")
            source_tx = get_object_or_404(
                ImportedTransaction,
                id=int(single_id),
                session=session,
                user=request.user,
                final_transaction__isnull=True,
                is_split_parent=False,
            )
            if source_tx.split_children.exists():
                return _error_response("Транзакция уже была разделена.")
            try:
                split_items = _parse_split_items(request.POST.get("split_items", "[]"), source_tx.amount or Decimal("0"))
            except ValueError as exc:
                return _error_response(str(exc))

            link_ids = {item["expense_link_id"] for item in split_items}
            links = {
                link.id: link
                for link in ExpenseLink.objects.filter(
                    id__in=link_ids,
                    user=request.user,
                    status="active",
                )
            }
            missing_links = [link_id for link_id in link_ids if link_id not in links]
            if missing_links:
                return _error_response("Часть категорий не найдена или недоступна.")

            created_items = []
            now = timezone.now()
            with db_transaction.atomic():
                source_tx.is_split_parent = True
                source_tx.locked = True
                source_tx.save(update_fields=["is_split_parent", "locked", "updated_at"])
                for index, part in enumerate(split_items, start=1):
                    amount = part["amount"]
                    comment = part["comment"] or (source_tx.original_description or "")
                    fingerprint = BaseNormalizer.make_fingerprint(
                        source_tx.fingerprint,
                        "split",
                        str(index),
                        str(now.timestamp()),
                    )
                    created_items.append(
                        ImportedTransaction(
                            session=source_tx.session,
                            user=source_tx.user,
                            raw_payload=source_tx.raw_payload,
                            normalized_payload={
                                **(source_tx.normalized_payload or {}),
                                "split_parent_id": source_tx.id,
                                "split_index": index,
                            },
                            occurred_at=source_tx.occurred_at,
                            amount=amount,
                            amount_abs=abs(amount),
                            currency=source_tx.currency,
                            direction=ImportedTransaction.Direction.INCOME if amount > 0 else ImportedTransaction.Direction.EXPENSE,
                            merchant_norm=source_tx.merchant_norm,
                            description_norm=BaseNormalizer.normalize_text(comment) or source_tx.description_norm,
                            source_category_norm=source_tx.source_category_norm,
                            external_id=source_tx.external_id,
                            original_description=comment or None,
                            fingerprint=fingerprint,
                            categorization_status=ImportedTransaction.CategorizationStatus.MANUAL,
                            resolved_expense_link=links[part["expense_link_id"]],
                            locked=True,
                            split_from=source_tx,
                        )
                    )
                ImportedTransaction.objects.bulk_create(created_items, batch_size=100)
            from .services.import_pipeline import refresh_session_review_counters
            refresh_session_review_counters(request.user, session)
            if _is_ajax_request():
                return JsonResponse({"ok": True, "message": "Транзакция разделена.", "reload": True})
            messages.success(request, "Транзакция разделена.")
            return _redirect_review()

        if action == "save_comment":
            tx_id = request.POST.get("single_transaction_id", "")
            if not tx_id.isdigit():
                return _error_response("Некорректная транзакция.")
            comment_value = (request.POST.get("single_comment") or "").strip()
            updated = ImportedTransaction.objects.filter(
                id=int(tx_id),
                session=session,
                user=request.user,
            ).update(original_description=comment_value)
            if not updated:
                return _error_response("Транзакция не найдена.", status=404)
            if _is_ajax_request():
                return JsonResponse({"ok": True, "message": "Комментарий сохранен."})
            messages.success(request, "Комментарий сохранен.")
            return _redirect_review()

        if action == "finalize":
            result = finalize_session(request.user, session)
            msg = f"Финализация завершена. Создано транзакций: {result['created']}."
            if result.get("remaining_review"):
                msg += f" Осталось на проверке: {result['remaining_review']}."
            messages.success(request, msg)
            return redirect("transactions:list")

        if action in {"apply_single_label", "apply_single_label_and_rule"}:
            single_id = request.POST.get("single_transaction_id", "")
            selected_ids = [int(single_id)] if single_id.isdigit() else []
            expense_link_id = request.POST.get("single_expense_link_id")
        else:
            selected_ids = [int(item) for item in request.POST.getlist("transaction_ids") if item.isdigit()]
            expense_link_id = request.POST.get("expense_link_id")
        if not selected_ids:
            return _error_response("Выберите хотя бы одну транзакцию для разметки.")
        if not expense_link_id:
            return _error_response("Выберите категорию для массовой разметки.")

        expense_link = get_object_or_404(
            ExpenseLink.objects.select_related("project", "category", "subcategory"),
            pk=expense_link_id,
            user=request.user,
            status="active",
        )

        if action in {"apply_single_label", "apply_single_label_and_rule"} and selected_ids:
            single_comment = (request.POST.get("single_comment") or "").strip()
            if single_comment:
                ImportedTransaction.objects.filter(
                    id=selected_ids[0],
                    session=session,
                    user=request.user,
                ).update(original_description=single_comment)

        created_rule = None
        if action in {"apply_label_and_rule", "apply_single_label_and_rule"}:
            selected_items = list(
                ImportedTransaction.objects.filter(
                    id__in=selected_ids,
                    session=session,
                    user=request.user,
                )
            )
            filters = {
                "description_norm": request.POST.get("description", ""),
                "source_category_norm": request.POST.get("source_category", ""),
            }
            if not any((filters.get("description_norm"), filters.get("source_category_norm"))):
                if selected_items:
                    description_values = {item.description_norm for item in selected_items if item.description_norm}
                    source_category_values = {item.source_category_norm for item in selected_items if item.source_category_norm}
                    if len(description_values) == 1:
                        filters["description_norm"] = next(iter(description_values))
                    if len(source_category_values) == 1:
                        filters["source_category_norm"] = next(iter(source_category_values))

            seed_tx = selected_items[0] if selected_items else None
            if seed_tx:
                filters["direction"] = seed_tx.direction
                filters["currency"] = seed_tx.currency
            try:
                created_rule = create_or_update_user_rule(
                    request.user,
                    session,
                    expense_link=expense_link,
                    rule_name=request.POST.get("rule_name", ""),
                    filters=filters,
                )
            except ValueError as exc:
                return _error_response(str(exc))

        result = apply_manual_label(
            request.user,
            session=session,
            transaction_ids=selected_ids,
            expense_link=expense_link,
            created_rule=created_rule,
        )
        session.refresh_from_db(fields=["needs_review_rows"])
        if created_rule:
            success_message = f"Размечено: {result['updated']}. Правило '{created_rule.name}' сохранено."
        else:
            success_message = f"Размечено транзакций: {result['updated']}."

        if _is_ajax_request():
            return JsonResponse(
                {
                    "ok": True,
                    "message": success_message,
                    "updated_ids": selected_ids,
                    "needs_review_rows": session.needs_review_rows,
                    "status": session.status,
                }
            )

        messages.success(request, success_message)
        return _redirect_review()

    paginator = Paginator(review_qs, 100)
    page_number = request.GET.get("page")
    review_page = paginator.get_page(page_number)
    review_rows = []
    for item in review_page.object_list:
        amount_text, is_income = _format_amount_short(item.amount)
        review_rows.append(
            {
                "id": item.id,
                "occurred_at": item.occurred_at,
                "amount_text": amount_text,
                "amount_raw": str(item.amount or ""),
                "is_income": is_income,
                "currency": item.currency,
                "description_display": _review_display_value(item, "column_comment", item.original_description or item.description_norm),
                "source_category_display": _review_display_value(item, "column_category", item.source_category_norm),
                "comment_display": item.original_description or "",
                "status": item.categorization_status,
                "resolved_expense_link_id": item.resolved_expense_link_id,
            }
        )

    expense_links = (
        ExpenseLink.objects.filter(
            user=request.user,
            status="active",
            project__status="active",
            category__status="active",
        )
        .select_related("project", "category", "subcategory")
        .order_by("project__name", "category__name", "subcategory__name")
    )

    return render(
        request,
        "transactions/review.html",
        {
            "session": session,
            "items": review_page,
            "review_rows": review_rows,
            "description_filter": description_filter,
            "source_category_filter": source_category_filter,
            "expense_links": expense_links,
        },
    )


@login_required
def transaction_list(request):
    user = request.user

    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    updated_prefs = False
    if preferences.default_account and preferences.default_account.status != 'active':
        preferences.default_account = None
        updated_prefs = True
    if preferences.default_project and preferences.default_project.status != 'active':
        preferences.default_project = None
        updated_prefs = True
    if updated_prefs:
        preferences.save(update_fields=['default_account', 'default_project'])
    initial_form_data = {}
    if preferences.default_account:
        initial_form_data['account'] = preferences.default_account.pk
        initial_form_data['currency'] = preferences.default_account.currency

    if request.method == 'POST':
        form = TransactionForm(user, request.POST)
        if form.is_valid():
            transaction = form.save(commit=False)
            transaction.account = form.cleaned_data['account']
            transaction.currency = form.cleaned_data['currency']
            transaction.comment = form.cleaned_data.get('comment', '')
            transaction.save()
            return redirect('transactions:list')
        else:
            project_tree = _build_project_structure(user)
            accounts = Account.objects.filter(user=user, status='active').order_by('name')
            projects = Project.objects.filter(user=user, status='active').order_by('name')
            categories = Category.objects.filter(user=user, status='active').order_by('name')
            subcategories = Subcategory.objects.filter(user=user, status='active').order_by('name')
            context = {
                'form': form,
                'accounts': accounts,
                'project_tree': project_tree,
                'projects': projects,
                'categories': categories,
                'subcategories': subcategories,
                'accounts_data': [
                    {'id': account.id, 'currency': account.currency}
                    for account in accounts
                ],
                'default_account_id': preferences.default_account_id,
                'default_project_id': preferences.default_project_id,
                'error_modal': True,
                'expense_links_options': list(
                    ExpenseLink.objects.filter(
                        user=user,
                        status='active',
                        project__status='active',
                        category__status='active',
                    )
                    .select_related('project', 'category', 'subcategory')
                    .order_by('project__name', 'category__name', 'subcategory__name')
                    .values(
                        'id',
                        'project__name',
                        'category__name',
                        'subcategory__name',
                    )
                ),
            }
            return render(request, 'transactions/transaction-list.html', context)

    form = TransactionForm(user, initial=initial_form_data)
    project_tree = _build_project_structure(user)
    accounts = Account.objects.filter(user=user, status='active').order_by('name')
    projects = Project.objects.filter(user=user, status='active').order_by('name')

    categories = Category.objects.filter(user=user, status='active').order_by('name')
    subcategories = Subcategory.objects.filter(user=user, status='active').order_by('name')

    context = {
        'form': form,
        'accounts': accounts,
        'project_tree': project_tree,
        'projects': projects,
        'categories': categories,
        'subcategories': subcategories,
        'accounts_data': [
            {'id': account.id, 'currency': account.currency}
            for account in accounts
        ],
        'accounts_options': [
            {'id': account.id, 'name': account.name, 'currency': account.currency}
            for account in accounts
        ],
        'currency_choices': [
            {'code': code, 'label': label}
            for code, label in form.fields['currency'].choices
        ],
        'default_account_id': preferences.default_account_id,
        'default_project_id': preferences.default_project_id,
        'expense_links_options': list(
            ExpenseLink.objects.filter(
                user=user,
                status='active',
                project__status='active',
                category__status='active',
            )
            .select_related('project', 'category', 'subcategory')
            .order_by('project__name', 'category__name', 'subcategory__name')
            .values(
                'id',
                'project__name',
                'category__name',
                'subcategory__name',
            )
        ),
    }
    return render(request, 'transactions/transaction-list.html', context)


TRANSACTION_SORT_COLUMN_MAP = {
    0: 'date',
    1: 'amount',
    2: 'currency',
    3: 'account__name',
    4: 'expense_link__project__name',
    5: 'expense_link__category__name',
    6: 'expense_link__subcategory__name',
    7: 'comment',
}


def _build_transactions_queryset(user, params):
    def _multi_values(name):
        raw_values = []
        if hasattr(params, "getlist"):
            raw_values.extend(params.getlist(name))
            raw_values.extend(params.getlist(f"{name}[]"))
        single_raw = params.get(name)
        if single_raw:
            raw_values.append(single_raw)
        values = []
        for raw in raw_values:
            for part in str(raw).split("|"):
                item = part.strip()
                if item and item not in values:
                    values.append(item)
        return values

    search_query = (params.get('search_query') or '').strip()
    project_filters = _multi_values('project')
    account_filters = _multi_values('account')
    category_filters = _multi_values('category')
    subcategory_filters = _multi_values('subcategory')
    date_start = params.get('date_start')
    date_end = params.get('date_end')

    queryset = Transaction.objects.filter(account__user=user).select_related(
        'account',
        'expense_link__project',
        'expense_link__category',
        'expense_link__subcategory'
    ).filter(is_split_parent=False)

    if project_filters:
        queryset = queryset.filter(expense_link__project__name__in=project_filters)
    if account_filters:
        queryset = queryset.filter(account__name__in=account_filters)
    if category_filters:
        queryset = queryset.filter(expense_link__category__name__in=category_filters)
    if subcategory_filters:
        include_none = '__none' in subcategory_filters
        named_subcategories = [name for name in subcategory_filters if name != '__none']
        if include_none and named_subcategories:
            queryset = queryset.filter(
                Q(expense_link__subcategory__isnull=True) |
                Q(expense_link__subcategory__name__in=named_subcategories)
            )
        elif include_none:
            queryset = queryset.filter(expense_link__subcategory__isnull=True)
        else:
            queryset = queryset.filter(expense_link__subcategory__name__in=named_subcategories)

    if date_start:
        try:
            qs_start = datetime.strptime(date_start, '%Y-%m-%d')
            if timezone.is_naive(qs_start):
                qs_start = timezone.make_aware(qs_start)
            queryset = queryset.filter(date__gte=qs_start)
        except ValueError:
            pass
    if date_end:
        try:
            qs_end = datetime.strptime(date_end, '%Y-%m-%d')
            if timezone.is_naive(qs_end):
                qs_end = timezone.make_aware(qs_end)
            qs_end = qs_end.replace(hour=23, minute=59, second=59)
            queryset = queryset.filter(date__lte=qs_end)
        except ValueError:
            pass

    if search_query:
        search_filter = (
            Q(account__name__icontains=search_query) |
            Q(expense_link__project__name__icontains=search_query) |
            Q(expense_link__category__name__icontains=search_query) |
            Q(expense_link__subcategory__name__icontains=search_query) |
            Q(comment__icontains=search_query)
        )
        amount_candidate = search_query.replace(" ", "").replace("\xa0", "").replace(",", ".")
        amount_candidate = re.sub(r"[^0-9.\-+]", "", amount_candidate)
        try:
            if amount_candidate and amount_candidate not in {"-", "+", "."}:
                parsed_amount = Decimal(amount_candidate)
                search_filter |= Q(amount=parsed_amount) | Q(amount=-parsed_amount)
        except InvalidOperation:
            pass
        queryset = queryset.filter(search_filter)

    order_column_raw = params.get('order[0][column]')
    order_dir = (params.get('order[0][dir]') or 'desc').lower()
    order_field = None
    try:
        order_column = int(order_column_raw) if order_column_raw is not None else None
        order_field = TRANSACTION_SORT_COLUMN_MAP.get(order_column)
    except (TypeError, ValueError):
        order_field = None

    if order_field:
        if order_dir == 'asc':
            queryset = queryset.order_by(order_field, 'id')
        else:
            queryset = queryset.order_by(f'-{order_field}', '-id')
    else:
        queryset = queryset.order_by('-date', '-id')

    return queryset


def _build_transaction_filter_options(user, params):
    option_params = params.copy()
    queryset = _build_transactions_queryset(user, option_params)
    rows = queryset.values(
        "account__name",
        "expense_link__project__name",
        "expense_link__category__name",
        "expense_link__subcategory__name",
    ).distinct()

    projects = sorted({row["expense_link__project__name"] for row in rows if row["expense_link__project__name"]})
    accounts = sorted({row["account__name"] for row in rows if row["account__name"]})
    categories = sorted({row["expense_link__category__name"] for row in rows if row["expense_link__category__name"]})
    subcategories = sorted({row["expense_link__subcategory__name"] for row in rows if row["expense_link__subcategory__name"]})
    if queryset.filter(expense_link__subcategory__isnull=True).exists():
        subcategories.append("__none")
    return {
        "project": projects,
        "account": accounts,
        "category": categories,
        "subcategory": subcategories,
    }


@login_required
def transaction_data(request):
    user = request.user
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 20))
    base_queryset = Transaction.objects.filter(account__user=user, is_split_parent=False)
    queryset = _build_transactions_queryset(user, request.GET)

    records_total = base_queryset.count()
    records_filtered = queryset.count()
    if length <= 0:
        length = records_filtered or 1
    paginator = Paginator(queryset, length)
    page_number = start // length + 1
    page = paginator.get_page(page_number)

    data = [_format_transaction_row(transaction) for transaction in page.object_list]

    return JsonResponse({
        'draw': draw,
        'recordsTotal': records_total,
        'recordsFiltered': records_filtered,
        'data': data,
        'filter_options': _build_transaction_filter_options(user, request.GET),
    })


@login_required
def transaction_export(request):
    queryset = _build_transactions_queryset(request.user, request.GET)
    requested_columns = []
    for raw_column in request.GET.getlist("columns"):
        requested_columns.extend(item.strip() for item in str(raw_column).split("|") if item.strip())
    allowed_columns = {
        "date": "Дата",
        "amount": "Сумма",
        "currency": "Валюта",
        "converted_amount": "Сумма в валюте отчета",
        "selected_currency": "Выбранная валюта",
        "account": "Счет",
        "project": "Проект",
        "category": "Категория",
        "subcategory": "Подкатегория",
        "comment": "Комментарий",
    }
    if not requested_columns:
        requested_columns = ["date", "amount", "currency", "account", "project", "category", "subcategory", "comment"]
    requested_columns = [column for column in requested_columns if column in allowed_columns]
    if not requested_columns:
        requested_columns = ["date", "amount", "currency", "account", "project", "category", "subcategory", "comment"]
    report_currency = (request.GET.get("report_currency") or "RUB").strip().upper()
    exclude_internal_transfers = str(request.GET.get("exclude_internal_transfers") or "").strip() in {"1", "true", "on", "yes"}
    exclude_people_transfers = str(request.GET.get("exclude_people_transfers") or "").strip() in {"1", "true", "on", "yes"}
    net_reimbursements = str(request.GET.get("net_reimbursements") or "").strip() in {"1", "true", "on", "yes"}
    export_rows = _build_export_rows(
        request.user,
        queryset,
        report_currency,
        exclude_internal_transfers=exclude_internal_transfers,
        exclude_people_transfers=exclude_people_transfers,
        net_reimbursements=net_reimbursements,
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="transactions_export.csv"'
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=';')
    writer.writerow([allowed_columns[column] for column in requested_columns])

    for row in export_rows:
        tx = row["tx"]
        converted_amount = row["converted_amount"] if "converted_amount" in requested_columns else None
        values = {
            "date": timezone.localtime(tx.date).strftime('%d.%m.%Y %H:%M'),
            "amount": str(row["amount"]),
            "currency": tx.currency,
            "converted_amount": str(converted_amount.quantize(Decimal("0.01"))) if converted_amount is not None else "",
            "selected_currency": report_currency,
            "account": tx.account.name if tx.account_id else '',
            "project": tx.expense_link.project.name if tx.expense_link_id else '',
            "category": tx.expense_link.category.name if tx.expense_link_id else '',
            "subcategory": tx.expense_link.subcategory.name if tx.expense_link and tx.expense_link.subcategory else '',
            "comment": tx.comment or '',
        }
        writer.writerow([values[column] for column in requested_columns])

    return response


@login_required
@require_POST
def transaction_update(request, pk):
    transaction = get_object_or_404(
        Transaction.objects.select_related(
            'account',
            'expense_link__project',
            'expense_link__category',
            'expense_link__subcategory',
        ),
        pk=pk,
        account__user=request.user,
        is_split_parent=False,
    )
    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'success': False, 'error': 'Не удалось разобрать данные'}, status=400)

    errors = {}

    date_value = transaction.date
    date_str = payload.get('date')
    if not date_str:
        errors['date'] = 'Укажите дату'
    else:
        try:
            date_value = datetime.strptime(date_str, '%Y-%m-%dT%H:%M')
            if timezone.is_naive(date_value):
                date_value = timezone.make_aware(date_value, timezone.get_current_timezone())
        except ValueError:
            errors['date'] = 'Некорректный формат даты'

    amount = transaction.amount
    amount_raw = payload.get('amount')
    try:
        amount = Decimal(str(amount_raw).replace(' ', '').replace(',', '.'))
    except (InvalidOperation, TypeError, AttributeError):
        errors['amount'] = 'Некорректная сумма'

    currency = _normalize_string(payload.get('currency')) or transaction.currency

    account = None
    account_id = payload.get('account_id')
    if account_id:
        try:
            account = Account.objects.get(pk=account_id, user=request.user, status='active')
        except Account.DoesNotExist:
            errors['account'] = 'Счёт не найден'
    else:
        errors['account'] = 'Укажите счёт'

    expense_link = None
    expense_link_id = payload.get('expense_link_id')
    if expense_link_id:
        try:
            expense_link = ExpenseLink.objects.select_related('project', 'category', 'subcategory').get(
                pk=expense_link_id,
                user=request.user,
                status='active',
                project__status='active',
                category__status='active',
            )
        except ExpenseLink.DoesNotExist:
            errors['expense_link'] = 'Категория не найдена'
    else:
        project = None
        project_id = payload.get('project_id')
        if project_id:
            try:
                project = Project.objects.get(pk=project_id, user=request.user, status='active')
            except Project.DoesNotExist:
                errors['project'] = 'Проект не найден'
        else:
            errors['project'] = 'Укажите проект'

        category = None
        category_id = payload.get('category_id')
        if category_id:
            try:
                category = Category.objects.get(pk=category_id, user=request.user, status='active')
            except Category.DoesNotExist:
                errors['category'] = 'Категория не найдена'
        else:
            errors['category'] = 'Укажите категорию'

        subcategory = None
        subcategory_id = payload.get('subcategory_id')
        if subcategory_id:
            try:
                subcategory = Subcategory.objects.get(pk=subcategory_id, user=request.user, status='active')
            except Subcategory.DoesNotExist:
                errors['subcategory'] = 'Подкатегория не найдена'

    if errors:
        return JsonResponse({'success': False, 'errors': errors}, status=400)

    if not expense_link:
        expense_link = ExpenseLink.objects.filter(
            user=request.user,
            project=project,
            category=category,
            subcategory=subcategory,
            status='active'
        ).first()
        if not expense_link:
            expense_link = _ensure_expense_link(request.user, project, category, subcategory)

    transaction.date = date_value
    transaction.amount = amount
    transaction.currency = currency or account.currency
    transaction.account = account
    transaction.expense_link = expense_link
    transaction.comment = (_normalize_string(payload.get('comment')) or None)
    transaction.transaction_type = 'income' if amount >= 0 else 'expense'
    transaction.save()

    return JsonResponse({'success': True, 'row': _format_transaction_row(transaction)})


@login_required
@require_POST
def transaction_delete(request, pk):
    transaction = get_object_or_404(Transaction, pk=pk, account__user=request.user, is_split_parent=False)
    if transaction.link_items.exists():
        return JsonResponse({'success': False, 'error': 'Нельзя удалить транзакцию, которая участвует в связке.'}, status=400)
    transaction.delete()
    return JsonResponse({'success': True})


@login_required
@require_POST
def transaction_split(request, pk):
    source = get_object_or_404(
        Transaction.objects.select_related("account", "expense_link"),
        pk=pk,
        account__user=request.user,
        is_split_parent=False,
    )
    if source.parent_transaction_id:
        return JsonResponse({"success": False, "error": "Нельзя делить часть уже разделенной операции."}, status=400)
    if source.link_items.exists():
        return JsonResponse({"success": False, "error": "Нельзя делить транзакцию, которая участвует в связке."}, status=400)
    if source.split_children.exists():
        return JsonResponse({"success": False, "error": "Транзакция уже была разделена."}, status=400)

    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Не удалось разобрать данные"}, status=400)

    try:
        split_items = _parse_split_items(payload.get("items", []), source.amount)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    link_ids = {item["expense_link_id"] for item in split_items}
    links = {
        link.id: link
        for link in ExpenseLink.objects.filter(
            id__in=link_ids,
            user=request.user,
            status="active",
        )
    }
    if len(links) != len(link_ids):
        return JsonResponse({"success": False, "error": "Часть категорий не найдена или недоступна."}, status=400)

    with db_transaction.atomic():
        source.is_split_parent = True
        source.save(update_fields=["is_split_parent"])
        children = []
        for item in split_items:
            amount = item["amount"]
            children.append(
                Transaction(
                    account=source.account,
                    expense_link=links[item["expense_link_id"]],
                    amount=amount,
                    currency=source.currency,
                    date=source.date,
                    transaction_type="income" if amount > 0 else "expense",
                    comment=item["comment"] or source.comment,
                    related_transaction=source.related_transaction,
                    parent_transaction=source,
                )
            )
        Transaction.objects.bulk_create(children, batch_size=100)

    return JsonResponse({"success": True})
BANK_PRESET_MAPPINGS = {
    'tinkoff': {
        'column_date': 'Дата операции',
        'column_amount': 'Сумма операции',
        'column_currency': 'Валюта операции',
        'column_category': 'Категория',
        'column_comment': 'Описание',
        'column_account': 'Номер карты',
        'default_project_name': 'Тинькофф',
        'default_account_name': 'Карта Тинькофф',
    },
    'alfa': {
        'column_date': 'Дата операции',
        'column_amount': 'Сумма',
        'column_currency': 'Валюта',
        'column_category': 'Категория',
        'column_comment': 'Описание операции',
        'column_account': 'Название счета',
        'default_project_name': 'Альфа-Банк',
        'default_account_name': 'Счёт Альфа',
    },
    't_business': {
        'column_date': 'Дата проведения',
        'column_amount': 'Сумма в валюте счёта',
        'column_type': 'Тип операции (пополнение/списание)',
        'column_comment': 'Назначение платежа',
        'default_currency': 'RUB',
        'income_markers': 'доход,поступление,пополнение,кредит,income,credit',
        'expense_markers': 'расход,списание,дебет,expense,debit',
        'default_project_name': 'Т Бизнес',
        'default_account_name': 'Тинькофф. Бизнес',
    },
}


def _bybit_account_name(event: BybitExternalEvent) -> str:
    if event.stream == "uta_translog":
        return "Bybit (UNIFIED)"
    busi = (
        BaseNormalizer.normalize_string(event.raw_payload.get("showBusiTypeEn"))
        or BaseNormalizer.normalize_string(event.raw_payload.get("showBusiType"))
    ).lower()
    if "card" in busi:
        return "Bybit Card"
    return "Bybit (FUND)"


def _bybit_source_category(event: BybitExternalEvent) -> str:
    if event.stream == "uta_translog":
        return "UNIFIED"
    if event.stream == "funding_history":
        return "FUND"
    if event.stream == "convert_history":
        return "CONVERT"
    return event.stream.upper()


def _is_fiat_currency(code: str) -> bool:
    return code in {
        "RUB", "USD", "EUR", "KZT", "KGS", "GBP", "CHF", "JPY", "CNY",
        "UAH", "BYN", "CAD", "AUD", "NOK", "SEK", "TRY", "AED", "HKD",
        "SGD", "THB", "VND", "INR", "IDR", "MYR", "PHP",
    }


def _make_bybit_row(
    event: BybitExternalEvent,
    *,
    amount: Decimal = None,
    currency: str = "",
    description: str = "",
    source_category: str = "",
    event_id: str = "",
) -> dict:
    occurred = event.occurred_at or timezone.now()
    amount_value = event.amount if amount is None else amount
    return {
        "event_id": event_id or str(event.id),
        "occurred_at": timezone.localtime(occurred).strftime("%Y-%m-%d %H:%M:%S"),
        "amount": str(amount_value or ""),
        "currency": (currency or event.asset or "USDT").upper(),
        "description": description if description else (event.description or ""),
        "source_category": source_category if source_category else _bybit_source_category(event),
        "account_name": _bybit_account_name(event),
        "external_id": event.external_id or "",
    }


def _transform_bybit_events_for_import(events):
    sorted_events = sorted(
        events,
        key=lambda item: (
            item.occurred_at or timezone.now(),
            item.id,
        ),
    )
    by_id = {event.id: event for event in sorted_events}
    consumed = set()
    rows = []

    # 1) Подготовка пар TRADE: расход USDT + покупка токена.
    trade_token_map = {}
    for event in sorted_events:
        if event.stream != "uta_translog":
            continue
        if (event.description or "").upper() != "TRADE":
            continue
        if (event.asset or "").upper() == "USDT":
            continue
        if not event.amount or event.amount <= 0:
            continue
        symbol = BaseNormalizer.normalize_string(event.raw_payload.get("symbol")).upper()
        key = (
            event.occurred_at,
            symbol,
        )
        trade_token_map.setdefault(key, []).append(event)

    # 2) Coin Purchase + Purchase(USDT): split в exchange + fee.
    coin_purchase_events = [
        event
        for event in sorted_events
        if event.stream == "funding_history"
        and (event.asset or "").upper() == "USD"
        and event.amount
        and event.amount > 0
        and (event.description or "").lower() == "coin purchase"
    ]
    usdt_purchase_candidates = [
        event
        for event in sorted_events
        if event.stream == "funding_history"
        and (event.asset or "").upper() == "USDT"
        and event.amount
        and event.amount < 0
        and (event.description or "").lower() == "purchase"
    ]
    used_usdt_purchase_ids = set()
    for coin_event in coin_purchase_events:
        best = None
        best_key = None
        for usdt_event in usdt_purchase_candidates:
            if usdt_event.id in used_usdt_purchase_ids:
                continue
            if not usdt_event.occurred_at or not coin_event.occurred_at:
                continue
            time_gap = abs((coin_event.occurred_at - usdt_event.occurred_at).total_seconds())
            if time_gap > 120:
                continue
            amount_gap = abs(abs(usdt_event.amount) - abs(coin_event.amount))
            rank = (amount_gap, time_gap, usdt_event.id)
            if best_key is None or rank < best_key:
                best_key = rank
                best = usdt_event
        if best is None:
            continue

        used_usdt_purchase_ids.add(best.id)
        consumed.add(best.id)

        exchange_amount = abs(coin_event.amount)
        fee_amount = abs(best.amount) - exchange_amount

        rows.append(
            _make_bybit_row(
                coin_event,
                amount=coin_event.amount,
                currency="USD",
                description="Coin Purchase",
                source_category="EXCHANGE_COIN_PURCHASE",
            )
        )
        rows.append(
            _make_bybit_row(
                best,
                amount=-exchange_amount,
                currency="USDT",
                description="Exchange USDT->USD",
                source_category="EXCHANGE_USDT",
            )
        )
        if fee_amount > 0:
            rows.append(
                _make_bybit_row(
                    best,
                    amount=-fee_amount,
                    currency="USDT",
                    description="Exchange fee",
                    source_category="EXCHANGE_FEE",
                    event_id=f"{best.id}:fee:{coin_event.id}",
                )
            )

    # 3) Базовая выгрузка с учетом фильтра валют и TRADE-описаний.
    for event in sorted_events:
        if event.id in consumed:
            continue
        currency = (event.asset or "").upper()
        if currency != "USDT" and not _is_fiat_currency(currency):
            continue

        description = event.description or ""
        source_category = _bybit_source_category(event)

        if event.stream == "uta_translog" and (event.description or "").upper() == "TRADE" and currency == "USDT":
            symbol = BaseNormalizer.normalize_string(event.raw_payload.get("symbol")).upper()
            key = (event.occurred_at, symbol)
            token_events = trade_token_map.get(key, [])
            if token_events:
                token_event = token_events.pop(0)
                bought_token = (token_event.asset or "").upper()
                bought_amount = token_event.amount
                if bought_token:
                    if bought_amount is not None:
                        description = f"TRADE {symbol} | +{bought_amount} {bought_token}"
                    else:
                        description = f"TRADE {symbol} | {bought_token}"
                source_category = "TRADE_BUY"

        rows.append(
            _make_bybit_row(
                event,
                amount=event.amount,
                currency=currency,
                description=description,
                source_category=source_category,
            )
        )

    return rows


@login_required
def bybit_connections(request):
    selected_connection_id = request.GET.get("connection")
    connection = None
    if selected_connection_id and str(selected_connection_id).isdigit():
        connection = BybitConnection.objects.filter(user=request.user, pk=int(selected_connection_id)).first()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_connection":
            conn_id = (request.POST.get("connection_id") or "").strip()
            instance = None
            if conn_id.isdigit():
                instance = BybitConnection.objects.filter(user=request.user, pk=int(conn_id)).first()
            form = BybitConnectionForm(request.POST, instance=instance)
            if form.is_valid():
                saved = form.save(commit=False)
                saved.user = request.user
                saved.save()
                messages.success(request, "API-подключение Bybit сохранено.", extra_tags="bybit")
                return redirect(f"{reverse('transactions:bybit-connections')}?connection={saved.id}")
            messages.error(
                request,
                "Не удалось сохранить подключение Bybit. Проверьте поля формы.",
                extra_tags="bybit",
            )
        else:
            form = BybitConnectionForm(instance=connection)
    else:
        form = BybitConnectionForm(instance=connection)

    connections = BybitConnection.objects.filter(user=request.user).order_by("-updated_at")
    if connection is None:
        connection = connections.first()

    context = {
        "form": form,
        "selected_connection": connection,
        "connections": connections,
    }
    return render(request, "transactions/bybit-connections.html", context)


@login_required
def bybit_staging(request):
    stream_options = [
        ("uta_translog", "UNIFIED: transaction log"),
        ("funding_history", "FUND: history"),
        ("convert_history", "Convert history"),
    ]
    selected_connection_id = request.GET.get("connection")
    connection = None
    if selected_connection_id and str(selected_connection_id).isdigit():
        connection = BybitConnection.objects.filter(user=request.user, pk=int(selected_connection_id)).first()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "sync_connection":
            conn_id = request.POST.get("connection_id")
            date_from_value = (request.POST.get("date_from") or "").strip()
            date_to_value = (request.POST.get("date_to") or "").strip()
            selected_streams = request.POST.getlist("streams")
            start_dt = None
            end_dt = None
            if not (date_from_value and date_to_value):
                messages.error(
                    request,
                    "Укажите диапазон дат: «Дата от» и «Дата до».",
                    extra_tags="bybit",
                )
                return redirect(f"{reverse('transactions:bybit')}?connection={conn_id}")
            try:
                date_from = datetime.strptime(date_from_value, "%Y-%m-%d").date()
                date_to = datetime.strptime(date_to_value, "%Y-%m-%d").date()
                start_dt = timezone.make_aware(
                    datetime.combine(date_from, time.min),
                    timezone.get_current_timezone(),
                )
                end_dt = timezone.make_aware(
                    datetime.combine(date_to, time.max),
                    timezone.get_current_timezone(),
                )
            except ValueError:
                messages.error(
                    request,
                    "Неверный формат дат. Используйте ГГГГ-ММ-ДД.",
                    extra_tags="bybit",
                )
                return redirect(f"{reverse('transactions:bybit')}?connection={conn_id}")
            target_connection = get_object_or_404(BybitConnection, user=request.user, pk=conn_id)
            sync_run, stats = sync_bybit_transaction_log(
                target_connection,
                days=1,
                streams=selected_streams,
                start_dt=start_dt,
                end_dt=end_dt,
            )
            if sync_run.status == BybitSyncRun.Status.SUCCESS:
                selected_streams_text = ", ".join(sync_run.streams or [])
                summary = (
                    f"Bybit sync завершен [{selected_streams_text}]: "
                    f"fetched={stats['fetched']}, inserted={stats['inserted']}, updated={stats['updated']}."
                )
                if sync_run.message:
                    summary = f"{summary} Предупреждения: {sync_run.message}"
                messages.success(request, summary, extra_tags="bybit")
            else:
                messages.error(request, f"Bybit sync завершился с ошибкой: {sync_run.message}", extra_tags="bybit")
            return redirect(f"{reverse('transactions:bybit')}?connection={target_connection.id}")
        elif action == "create_import_session":
            conn_id = (request.POST.get("connection_id") or "").strip()
            target_connection = get_object_or_404(BybitConnection, user=request.user, pk=conn_id)
            stream_filter = (request.POST.get("stream") or "").strip()
            asset_filter = (request.POST.get("asset") or "").strip().upper()
            direction_filter = (request.POST.get("direction") or "").strip()
            search_filter = (request.POST.get("q") or "").strip()

            events_qs = BybitExternalEvent.objects.filter(
                connection=target_connection,
                imported_transaction__isnull=True,
                amount__isnull=False,
            ).order_by("-occurred_at", "-id")
            if stream_filter:
                events_qs = events_qs.filter(stream=stream_filter)
            if asset_filter:
                events_qs = events_qs.filter(asset=asset_filter)
            if direction_filter:
                events_qs = events_qs.filter(direction=direction_filter)
            if search_filter:
                events_qs = events_qs.filter(
                    Q(external_id__icontains=search_filter)
                    | Q(description__icontains=search_filter)
                    | Q(asset__icontains=search_filter)
                    | Q(stream__icontains=search_filter)
                )

            events = list(events_qs[:5000])
            if not events:
                messages.error(
                    request,
                    "Нет новых событий Bybit для импорта в разметку. Сначала сделайте sync или снимите фильтры.",
                    extra_tags="bybit",
                )
                return redirect(f"{reverse('transactions:bybit')}?connection={target_connection.id}")

            bybit_source, _ = ImportSource.objects.get_or_create(
                code="bybit",
                defaults={"name": "Bybit"},
            )
            rows = _transform_bybit_events_for_import(events)
            if not rows:
                messages.error(
                    request,
                    "После преобразования не осталось операций для импорта (фиат + USDT).",
                    extra_tags="bybit",
                )
                return redirect(f"{reverse('transactions:bybit')}?connection={target_connection.id}")
            columns = [
                "event_id",
                "occurred_at",
                "amount",
                "currency",
                "description",
                "source_category",
                "account_name",
                "external_id",
            ]
            session = TransactionImportSession.objects.create(
                user=request.user,
                source=bybit_source,
                status=TransactionImportSession.Status.UPLOADED,
                total_rows=len(rows),
                original_name=f"Bybit sync ({target_connection.name})",
                columns=columns,
                sample_rows=rows[:10],
                rows=rows,
                metadata={
                    "bank_preset": "bybit",
                    "connection_id": target_connection.id,
                },
            )

            mapping_data = {
                "column_date": "occurred_at",
                "column_amount": "amount",
                "column_currency": "currency",
                "column_comment": "description",
                "column_type": "",
                "column_category": "source_category",
                "column_subcategory": "",
                "column_account": "account_name",
                "default_currency": "USDT",
                "income_markers": "",
                "expense_markers": "",
                "default_account_name": "",
            }
            normalize_result = normalize_rows(request.user, session, mapping_data)
            mapping_snapshot = mapping_data.copy()
            session.metadata["last_mapping"] = mapping_snapshot
            session.save(update_fields=["metadata"])
            categorize_result = categorize_session(request.user, session, mapping_data=mapping_data)

            imported_items = list(
                ImportedTransaction.objects.filter(session=session).only("id", "raw_payload")
            )
            imported_map = {}
            for item in imported_items:
                raw_event_id = BaseNormalizer.normalize_string(item.raw_payload.get("event_id"))
                if raw_event_id:
                    imported_map[raw_event_id] = item.id
            events_to_update = []
            for event in events:
                imported_id = imported_map.get(str(event.id))
                if imported_id:
                    event.imported_transaction_id = imported_id
                    events_to_update.append(event)
            if events_to_update:
                BybitExternalEvent.objects.bulk_update(events_to_update, fields=["imported_transaction"])

            messages.success(
                request,
                (
                    f"Создана сессия разметки Bybit #{session.id}: "
                    f"normalized={normalize_result['normalized']}, "
                    f"duplicates={normalize_result['duplicates']}, "
                    f"needs_review={categorize_result['needs_review']}."
                ),
                extra_tags="bybit",
            )
            return redirect(f"{reverse('transactions:review')}?session={session.id}")
        elif action == "clear_staging":
            conn_id = (request.POST.get("connection_id") or "").strip()
            target_connection = get_object_or_404(BybitConnection, user=request.user, pk=conn_id)
            deleted_count, _ = BybitExternalEvent.objects.filter(connection=target_connection).delete()
            messages.success(
                request,
                f"Staging очищен: удалено событий {deleted_count}.",
                extra_tags="bybit",
            )
            return redirect(f"{reverse('transactions:bybit')}?connection={target_connection.id}")
        else:
            pass

    connections = BybitConnection.objects.filter(user=request.user).order_by("-updated_at")
    if connection is None:
        connection = connections.first()
    if request.method == "POST" and "form" in locals() and connection and form.instance.pk != connection.pk:
        # keep explicit selection in case of invalid form submit on a different instance
        pass

    events = []
    sync_runs = []
    sync_runs_page = None
    stream_choices = []
    asset_choices = []
    direction_choices = BybitExternalEvent.Direction.choices
    event_filters = {
        "stream": request.GET.get("stream", "").strip(),
        "asset": request.GET.get("asset", "").strip().upper(),
        "direction": request.GET.get("direction", "").strip(),
        "q": request.GET.get("q", "").strip(),
        "sort": request.GET.get("sort", "-occurred_at"),
    }
    allowed_sorts = {
        "id": "id",
        "-id": "-id",
        "occurred_at": "occurred_at",
        "-occurred_at": "-occurred_at",
        "amount": "amount",
        "-amount": "-amount",
        "fee_amount": "fee_amount",
        "-fee_amount": "-fee_amount",
        "stream": "stream",
        "-stream": "-stream",
        "asset": "asset",
        "-asset": "-asset",
    }
    sort_value = allowed_sorts.get(event_filters["sort"], "-occurred_at")
    event_filters["sort"] = sort_value

    if connection:
        events_qs = BybitExternalEvent.objects.filter(connection=connection)
        stream_choices = list(events_qs.values_list("stream", flat=True).distinct().order_by("stream"))
        asset_choices = list(events_qs.exclude(asset="").values_list("asset", flat=True).distinct().order_by("asset"))

        if event_filters["stream"]:
            events_qs = events_qs.filter(stream=event_filters["stream"])
        if event_filters["asset"]:
            events_qs = events_qs.filter(asset=event_filters["asset"])
        if event_filters["direction"]:
            events_qs = events_qs.filter(direction=event_filters["direction"])
        if event_filters["q"]:
            events_qs = events_qs.filter(
                Q(external_id__icontains=event_filters["q"])
                | Q(description__icontains=event_filters["q"])
                | Q(asset__icontains=event_filters["q"])
                | Q(stream__icontains=event_filters["q"])
            )
        events = events_qs.order_by(sort_value, "-id")[:300]

        sync_runs_qs = BybitSyncRun.objects.filter(connection=connection).order_by("-id")
        sync_runs_paginator = Paginator(sync_runs_qs, 5)
        sync_runs_page = sync_runs_paginator.get_page(request.GET.get("runs_page"))
        sync_runs = sync_runs_page.object_list

    context = {
        "connections": connections,
        "selected_connection": connection,
        "events": events,
        "sync_runs": sync_runs,
        "sync_runs_page": sync_runs_page,
        "stream_choices": stream_choices,
        "asset_choices": asset_choices,
        "direction_choices": direction_choices,
        "event_filters": event_filters,
        "stream_options": stream_options,
    }
    return render(request, "transactions/bybit.html", context)
