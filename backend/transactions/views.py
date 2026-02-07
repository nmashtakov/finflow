import io
import json
import re
import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pandas as pd

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Q
from django.views.decorators.http import require_POST

from core.models import Account, Project, Category, Subcategory, ExpenseLink, Transaction, UserPreferences
from .forms import (
    TransactionForm,
    TransactionImportUploadForm,
    TransactionImportMappingForm,
)
from .models import TransactionImportSession


class ImportRowError(Exception):
    """Raised when a row in the import file cannot be processed."""


def _normalize_string(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_decimal(value):
    text = _normalize_string(value)
    if not text:
        raise ImportRowError('Не указана сумма')
    text = text.replace(' ', '').replace("'", '')
    text = text.replace(',', '.').replace('\xa0', '')
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ImportRowError(f"Не удалось преобразовать сумму '{value}'") from exc


def _parse_date_value(value):
    text = _normalize_string(value)
    if not text:
        raise ImportRowError('Не указана дата')
    patterns = [
        '%d.%m.%Y %H:%M:%S',
        '%d.%m.%Y %H:%M',
        '%d.%m.%Y',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
    ]
    for pattern in patterns:
        try:
            dt = datetime.strptime(text, pattern)
            break
        except ValueError:
            continue
    else:
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ImportRowError(f"Не удалось распознать дату '{value}'") from exc
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _split_markers(markers):
    return [item.strip().lower() for item in (markers or '').split(',') if item.strip()]


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
INCOME_VALUE_MARKERS = {'доход', 'поступление', 'пополнение', 'income', 'credit', 'приход'}
EXPENSE_VALUE_MARKERS = {'расход', 'списание', 'перевод', 'expense', 'debit', 'платеж', 'платёж'}


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
    if 'описание операции' in lower_cols and 'название счета' in lower_cols and 'дата проводки' in lower_cols:
        return 'alfa'
    return 'other'


def _resolve_account(user, row_value, cleaned, currency):
    currency = _normalize_string(currency) or _normalize_string(cleaned.get('default_currency')) or 'RUB'
    account = cleaned.get('default_account')
    name = _normalize_string(row_value)
    if not name and account is None:
        fallback_name = _normalize_string(cleaned.get('default_account_name'))
        if fallback_name:
            name = fallback_name
    if account is None and not name:
        raise ImportRowError('Не удалось определить счёт')
    if account is not None:
        return account
    existing = Account.objects.filter(user=user, name__iexact=name).first()
    if existing:
        return existing
    return Account.objects.create(
        user=user,
        name=name,
        currency=currency or 'RUB',
        status='active',
    )


def _resolve_project(user, row_value, cleaned):
    project = cleaned.get('default_project')
    name = _normalize_string(row_value)
    if not name and project is None:
        fallback_name = _normalize_string(cleaned.get('default_project_name'))
        if fallback_name:
            name = fallback_name
    if project is None and not name:
        raise ImportRowError('Не удалось определить проект')
    if project is not None:
        return project
    existing = Project.objects.filter(user=user, name__iexact=name).first()
    if existing:
        return existing
    return Project.objects.create(user=user, name=name, status='active')


def _resolve_category(user, project, row_value):
    name = _normalize_string(row_value)
    if not name:
        raise ImportRowError('Не удалось определить категорию')
    category, _ = Category.objects.get_or_create(user=user, name=name, defaults={'status': 'active'})
    return category


def _resolve_subcategory(user, row_value):
    name = _normalize_string(row_value)
    if not name:
        return None
    subcategory, _ = Subcategory.objects.get_or_create(user=user, name=name, defaults={'status': 'active'})
    return subcategory


def _ensure_expense_link(user, project, category, subcategory):
    link, _ = ExpenseLink.objects.get_or_create(
        user=user,
        project=project,
        category=category,
        subcategory=subcategory,
        defaults={'status': 'active'},
    )
    return link


def _process_rows(user, session, cleaned):
    rows = session.rows
    column_date = cleaned['column_date']
    column_amount = cleaned['column_amount']
    column_currency = cleaned.get('column_currency')
    column_account = cleaned.get('column_account')
    column_project = cleaned.get('column_project')
    column_category = cleaned.get('column_category')
    column_subcategory = cleaned.get('column_subcategory')
    column_comment = cleaned.get('column_comment')
    column_type = cleaned.get('column_type')

    income_markers = set(_split_markers(cleaned.get('income_markers')))
    expense_markers = set(_split_markers(cleaned.get('expense_markers')))

    result = {'created': 0, 'errors': []}

    accounts_cache = {acc.name.lower(): acc for acc in Account.objects.filter(user=user)}
    projects_cache = {proj.name.lower(): proj for proj in Project.objects.filter(user=user)}
    categories_cache = {}
    subcategories_cache = {}
    links_cache = {}
    pending_transactions = []

    for index, row in enumerate(rows, start=1):
        try:
            if not any(value and str(value).strip() for value in row.values()):
                continue

            date_value = _parse_date_value(row.get(column_date))
            amount = _parse_decimal(row.get(column_amount))

            type_value = _normalize_string(row.get(column_type)) if column_type else ''
            type_value_lower = type_value.lower()
            if column_type and income_markers:
                if type_value_lower in income_markers:
                    amount = abs(amount)
                elif type_value_lower in expense_markers:
                    amount = -abs(amount)
            elif column_type and expense_markers:
                if type_value_lower in expense_markers:
                    amount = -abs(amount)

            currency = _normalize_string(row.get(column_currency)) if column_currency else ''
            if not currency:
                currency = _normalize_string(cleaned.get('default_currency')) or 'RUB'

            account_name = _normalize_string(row.get(column_account)) or _normalize_string(cleaned.get('default_account_name'))
            if cleaned.get('default_account') and not account_name:
                account = cleaned['default_account']
            else:
                cache_key = (account_name or '').lower()
                account = accounts_cache.get(cache_key)
                if account is None:
                    account = _resolve_account(user, row.get(column_account), cleaned, currency)
                    accounts_cache[cache_key or account.name.lower()] = account

            project_name = _normalize_string(row.get(column_project)) or _normalize_string(cleaned.get('default_project_name'))
            if cleaned.get('default_project') and not project_name:
                project = cleaned['default_project']
            else:
                proj_key = (project_name or '').lower()
                project = projects_cache.get(proj_key)
                if project is None:
                    project = _resolve_project(user, row.get(column_project), cleaned)
                    projects_cache[proj_key or project.name.lower()] = project

            category_value = row.get(column_category) if column_category else ''
            category_key = (_normalize_string(category_value) or '').lower()
            category = categories_cache.get(category_key)
            if category is None:
                category = _resolve_category(user, project, category_value)
                categories_cache[category_key or category.name.lower()] = category

            sub_value = row.get(column_subcategory) if column_subcategory else ''
            sub_name = _normalize_string(sub_value)
            sub_key = (sub_name or '').lower()
            subcategory = subcategories_cache.get(sub_key)
            if subcategory is None and sub_name:
                subcategory = _resolve_subcategory(user, sub_value)
                subcategories_cache[sub_key] = subcategory
            elif not sub_name:
                subcategory = None

            link_key = (project.id, category.id, subcategory.id if subcategory else None)
            expense_link = links_cache.get(link_key)
            if expense_link is None:
                expense_link = _ensure_expense_link(user, project, category, subcategory)
                links_cache[link_key] = expense_link

            comment_parts = []
            if column_comment:
                comment_parts.append(_normalize_string(row.get(column_comment)))
            if cleaned.get('default_comment'):
                comment_parts.append(_normalize_string(cleaned.get('default_comment')))
            comment = ' '.join(part for part in comment_parts if part)

            transaction_type = 'income' if amount >= 0 else 'expense'

            pending_transactions.append(Transaction(
                account=account,
                expense_link=expense_link,
                amount=amount,
                currency=currency or account.currency,
                date=date_value,
                transaction_type=transaction_type,
                comment=comment or None,
            ))
        except ImportRowError as exc:
            result['errors'].append({'row': index, 'message': str(exc), 'row_data': row})
        except Exception as exc:  # catch-all for unexpected issues
            result['errors'].append({'row': index, 'message': f'Неожиданная ошибка: {exc}', 'row_data': row})

    if pending_transactions:
        Transaction.objects.bulk_create(pending_transactions, batch_size=500)
        result['created'] += len(pending_transactions)

    return result


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
        'comment': transaction.comment or '',
        'comment_raw': transaction.comment or '',
    }


@login_required
def transaction_import(request):
    session_id = request.GET.get('session')
    session = None
    mapping_form = None
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
                            if preset_choice == 'auto':
                                # попробуем определить по первому листу
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
                        session = TransactionImportSession.objects.create(
                            user=request.user,
                            original_name=parsed['original_name'],
                            columns=parsed['columns'],
                            sample_rows=parsed['sample_rows'],
                            rows=parsed['rows'],
                            metadata={'bank_preset': preset_detected},
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
                    session = TransactionImportSession.objects.create(
                        user=request.user,
                        original_name=f"{parsed['original_name']} — {sheet_name}",
                        columns=parsed['columns'],
                        sample_rows=parsed['sample_rows'],
                        rows=parsed['rows'],
                        metadata={'bank_preset': pending.get('bank_preset', 'other'), 'sheet_name': sheet_name},
                    )
                    request.session.pop('import_excel_pending', None)
                    request.session.pop('import_excel_sheets', None)
                    return redirect(f"{reverse('transactions:import')}?session={session.id}")
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
                result = _process_rows(request.user, session, mapping_data)
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
                else:
                    session.delete()
                    session = None
                return render(request, 'transactions/import.html', {
                    'step': 'result',
                    'result': result,
                    'session': session,
                })
        elif step == 'discard' and session:
            session.delete()
            return redirect('transactions:list')
        else:
            upload_form = TransactionImportUploadForm()

    if session and result is None:
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
    if preferences.default_project:
        initial_form_data['project'] = preferences.default_project.pk

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
                'initial_values': {
                    'project': form['project'].value(),
                    'category': form['category'].value(),
                    'subcategory': form['subcategory'].value(),
                },
                'default_account_id': preferences.default_account_id,
                'default_project_id': preferences.default_project_id,
                'error_modal': True,
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
        'initial_values': {
            'project': form['project'].value(),
            'category': form['category'].value(),
            'subcategory': form['subcategory'].value(),
        },
        'default_account_id': preferences.default_account_id,
        'default_project_id': preferences.default_project_id,
    }
    return render(request, 'transactions/transaction-list.html', context)


@login_required
def transaction_data(request):
    user = request.user
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 20))
    search_query = request.GET.get('search_query', '').strip()
    project_filter = request.GET.get('project', '').strip()
    account_filter = request.GET.get('account', '').strip()
    category_filter = request.GET.get('category', '').strip()
    subcategory_filter = request.GET.get('subcategory', '').strip()
    date_start = request.GET.get('date_start')
    date_end = request.GET.get('date_end')

    base_queryset = Transaction.objects.filter(
        account__user=user
    )
    queryset = base_queryset.select_related(
        'account',
        'expense_link__project',
        'expense_link__category',
        'expense_link__subcategory'
    ).order_by('-date', '-id')

    if project_filter:
        queryset = queryset.filter(expense_link__project__name=project_filter)
    if account_filter:
        queryset = queryset.filter(account__name=account_filter)
    if category_filter:
        queryset = queryset.filter(expense_link__category__name=category_filter)
    if subcategory_filter == '__none':
        queryset = queryset.filter(expense_link__subcategory__isnull=True)
    elif subcategory_filter:
        queryset = queryset.filter(expense_link__subcategory__name=subcategory_filter)

    if date_start:
        qs_start = datetime.strptime(date_start, '%Y-%m-%d')
        if timezone.is_naive(qs_start):
            qs_start = timezone.make_aware(qs_start)
        queryset = queryset.filter(date__gte=qs_start)
    if date_end:
        qs_end = datetime.strptime(date_end, '%Y-%m-%d')
        if timezone.is_naive(qs_end):
            qs_end = timezone.make_aware(qs_end)
        qs_end = qs_end.replace(hour=23, minute=59, second=59)
        queryset = queryset.filter(date__lte=qs_end)

    if search_query:
        queryset = queryset.filter(
            Q(account__name__icontains=search_query) |
            Q(expense_link__project__name__icontains=search_query) |
            Q(expense_link__category__name__icontains=search_query) |
            Q(expense_link__subcategory__name__icontains=search_query) |
            Q(comment__icontains=search_query)
        )

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
    })


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
    transaction = get_object_or_404(Transaction, pk=pk, account__user=request.user)
    transaction.delete()
    return JsonResponse({'success': True})
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
}
