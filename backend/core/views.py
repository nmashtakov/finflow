from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Max, Min, Sum
from django.db.models.functions import TruncDay
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.utils import timezone

from core.currencies import currencies_need_usd_rate, fx_currency_code
from .forms import (
    ProjectForm,
    CategoryForm,
    SubcategoryForm,
    AccountForm,
    AccountBalanceSnapshotForm,
    CurrencyConverterForm,
)
from core.models import (
    Account,
    AccountBalanceSnapshot,
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
from .services.cbr_rates import sync_cbr_rates_full, sync_cbr_rates_incremental
from .services.crypto_rates import CBR_FIAT_CODES, portfolio_crypto_symbols, sync_crypto_rates_historical, sync_crypto_rates_incremental
from transactions.models import ImportedTransaction, TransactionImportSession


def _transliterate_to_ru(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    replacements = [
        ("sch", "щ"),
        ("sh", "ш"),
        ("ch", "ч"),
        ("ya", "я"),
        ("yo", "ё"),
        ("yu", "ю"),
        ("zh", "ж"),
        ("kh", "х"),
        ("ts", "ц"),
        ("ph", "ф"),
        ("th", "т"),
    ]
    for src, dst in replacements:
        value = value.replace(src, dst).replace(src.upper(), dst.upper())
    char_map = {
        "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
        "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
        "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
        "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
    }
    result = []
    for ch in value:
        low = ch.lower()
        if low in char_map:
            ru = char_map[low]
            if ch.isupper() and ru:
                ru = ru[0].upper() + ru[1:]
            result.append(ru)
        else:
            result.append(ch)
    return "".join(result)


def landing_view(request):
    return render(request, 'landing.html')


def _format_amount(value, decimals=2):
    quant = Decimal('1') if decimals == 0 else Decimal('1.' + ('0' * decimals))
    normalized = Decimal(value or 0).quantize(quant)
    text = f"{normalized:,.{decimals}f}".replace(',', ' ').replace('.', ',')
    if decimals > 0:
        suffix = ',' + ('0' * decimals)
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text


def _build_rate_lookup(tx_list, report_currency):
    currencies = {(tx.currency or '').upper() for tx in tx_list if tx.currency}
    currencies.add('RUB')
    if report_currency:
        currencies.add((report_currency or '').upper())
    if currencies_need_usd_rate(currencies):
        currencies.add('USD')
    date_points = [timezone.localtime(tx.date).date() for tx in tx_list]
    if not date_points:
        return {}
    max_date = max(date_points)
    min_date = min(date_points)
    lookup = {'RUB': [(min_date, Decimal('1'))]}
    for currency in currencies:
        if currency == 'RUB':
            continue
        rows = list(
            CurrencyRate.objects.filter(currency=currency, date__lte=max_date)
            .order_by('date')
            .values_list('date', 'amount')
        )
        if rows:
            lookup[currency] = rows
    return lookup


def _rate_on_date(rate_lookup, currency, target_date):
    currency = fx_currency_code(currency) or 'RUB'
    if currency == 'RUB':
        return Decimal('1')
    rows = rate_lookup.get(currency) or []
    if not rows:
        return None
    dates = [row[0] for row in rows]
    index = bisect_right(dates, target_date) - 1
    if index < 0:
        return rows[0][1]
    return rows[index][1]


def _convert_amount(amount, source_currency, target_currency, target_date, rate_lookup):
    source = (source_currency or '').upper()
    target = (target_currency or '').upper()
    if not target or source == target or fx_currency_code(source) == fx_currency_code(target):
        return Decimal(amount or 0)
    source_rate = _rate_on_date(rate_lookup, source, target_date)
    target_rate = _rate_on_date(rate_lookup, target, target_date)
    if source_rate is None or target_rate is None or target_rate == 0:
        return None
    return (Decimal(amount or 0) * source_rate) / target_rate


def _build_checklist_context(user, period_start, period_end, tx_list, accounts):
    linked_tx_ids = set(
        TransactionLinkItem.objects.filter(group__user=user).values_list('transaction_id', flat=True)
    )
    period_unlinked_qs = (
        Transaction.objects.filter(
            account__user=user,
            is_split_parent=False,
            date__range=(period_start, period_end),
        )
        .exclude(id__in=linked_tx_ids)
        .select_related('account', 'expense_link__subcategory')
    )

    def _normalized_subcategory(tx):
        if not tx.expense_link_id or not tx.expense_link.subcategory:
            return ''
        return ' '.join((tx.expense_link.subcategory.name or '').strip().lower().split())

    pending_reimbursement_count = 0
    pending_internal_transfer_count = 0
    pending_people_transfer_count = 0
    for tx in period_unlinked_qs:
        subcategory = _normalized_subcategory(tx)
        if tx.amount > 0 and subcategory == 'возврат':
            pending_reimbursement_count += 1
        elif 'перевод между счет' in subcategory:
            pending_internal_transfer_count += 1
        elif 'перевод между люд' in subcategory:
            pending_people_transfer_count += 1

    review_needed_count = ImportedTransaction.objects.filter(
        user=user,
        session__created_at__range=(period_start, period_end),
        categorization_status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW,
        final_transaction__isnull=True,
    ).count()
    open_import_sessions_count = TransactionImportSession.objects.filter(
        user=user,
        created_at__range=(period_start, period_end),
    ).exclude(status=TransactionImportSession.Status.COMPLETED).count()

    last_tx_by_account = dict(
        Transaction.objects.filter(account__user=user, is_split_parent=False)
        .values('account_id')
        .annotate(last_tx=Max('date'))
        .values_list('account_id', 'last_tx')
    )
    tx_by_account = defaultdict(list)
    for tx in tx_list:
        tx_by_account[tx.account_id].append(tx)

    period_account_activity = []
    for account in accounts:
        account_txs = tx_by_account.get(account.id, [])
        currencies_used = sorted({tx.currency for tx in account_txs if tx.currency})
        last_tx_overall = last_tx_by_account.get(account.id)
        latest_snapshot = (
            AccountBalanceSnapshot.objects
            .filter(account=account)
            .order_by('-snapshot_date', '-id')
            .first()
        )
        has_period_activity = bool(account_txs)
        last_period_tx_date = max((timezone.localtime(tx.date).date() for tx in account_txs), default=None)
        snapshot_target_date = last_period_tx_date or period_end.date()
        has_period_snapshot = bool(latest_snapshot and latest_snapshot.snapshot_date >= snapshot_target_date)
        period_account_activity.append({
            'account': account,
            'currencies_used': currencies_used,
            'is_multicurrency': len(currencies_used) > 1,
            'has_period_activity': has_period_activity,
            'last_tx_overall_at': timezone.localtime(last_tx_overall).strftime('%d.%m.%Y %H:%M') if last_tx_overall else '—',
            'snapshot_target_date': snapshot_target_date,
            'latest_snapshot': latest_snapshot,
            'has_period_snapshot': has_period_snapshot,
        })

    accounts_missing_snapshot = [row for row in period_account_activity if row['has_period_activity'] and not row['has_period_snapshot']]
    checklist_items = [
        {
            'title': 'Импорт и разметка',
            'status': 'ok' if review_needed_count == 0 and open_import_sessions_count == 0 else 'warning',
            'summary': (
                'Все импортированные операции за период разобраны.'
                if review_needed_count == 0 and open_import_sessions_count == 0
                else f'Нужно проверить {review_needed_count} операций, незавершённых импортов: {open_import_sessions_count}.'
            ),
            'action_url': reverse('transactions:import-sessions'),
            'action_label': 'Открыть импорты',
        },
        {
            'title': 'Связки операций',
            'status': 'ok' if (pending_reimbursement_count + pending_internal_transfer_count + pending_people_transfer_count) == 0 else 'warning',
            'summary': (
                'Непривязанных возвратов и переводов за период нет.'
                if (pending_reimbursement_count + pending_internal_transfer_count + pending_people_transfer_count) == 0
                else f'Возвраты: {pending_reimbursement_count}, между своими счетами: {pending_internal_transfer_count}, между людьми: {pending_people_transfer_count}.'
            ),
            'action_url': reverse('transactions:links'),
            'action_label': 'Открыть связки',
        },
        {
            'title': 'Сверка балансов',
            'status': 'ok' if not accounts_missing_snapshot else 'warning',
            'summary': (
                'По всем счетам с движением есть фиксация на конец периода.'
                if not accounts_missing_snapshot
                else 'Нет фиксации баланса на конец периода: ' + ', '.join(row['account'].name for row in accounts_missing_snapshot[:4]) + ('...' if len(accounts_missing_snapshot) > 4 else '')
            ),
            'action_url': reverse('accounts_directory'),
            'action_label': 'Открыть счета',
        },
    ]
    return {
        'checklist_items': checklist_items,
        'period_account_activity': period_account_activity,
    }


def _build_reimbursement_netting(scope_tx_list, converted_by_tx):
    tx_ids = {tx.id for tx in scope_tx_list}
    tx_date_by_id = {tx.id: tx.date for tx in scope_tx_list}
    expense_contrib = {}
    reimbursement_offset_ids = set()
    reimbursement_reduction_by_tx = defaultdict(lambda: Decimal('0'))

    for tx in scope_tx_list:
        converted_amount = converted_by_tx.get(tx.id)
        if converted_amount is None:
            continue
        subcategory_name = (
            (tx.expense_link.subcategory.name if tx.expense_link and tx.expense_link.subcategory else '') or ''
        ).strip().lower()
        if converted_amount < 0 and subcategory_name != 'перевод между счетами':
            expense_contrib[tx.id] = abs(converted_amount)

    reimbursement_groups = (
        TransactionLinkGroup.objects.filter(
            user=scope_tx_list[0].account.user if scope_tx_list else None,
            status=TransactionLinkGroup.Status.ACTIVE,
            link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
            items__transaction_id__in=tx_ids,
        )
        .distinct()
        .prefetch_related('items__transaction')
    ) if scope_tx_list else []

    for group in reimbursement_groups:
        primary_ids = []
        offset_sum = Decimal('0')
        for item in group.items.all():
            if item.transaction_id not in tx_ids:
                continue
            converted_amount = converted_by_tx.get(item.transaction_id)
            if item.role == TransactionLinkItem.Role.PRIMARY and converted_amount is not None and converted_amount < 0:
                primary_ids.append(item.transaction_id)
            elif item.role == TransactionLinkItem.Role.OFFSET and converted_amount is not None and converted_amount > 0:
                offset_sum += converted_amount
                reimbursement_offset_ids.add(item.transaction_id)
        if not primary_ids or offset_sum <= 0:
            continue
        ordered_primary_ids = sorted(
            [tx_id for tx_id in primary_ids if tx_id in expense_contrib],
            key=lambda tx_id: (tx_date_by_id.get(tx_id), tx_id),
        )
        remaining = offset_sum
        for primary_id in ordered_primary_ids:
            if remaining <= 0:
                break
            current_abs = expense_contrib.get(primary_id, Decimal('0'))
            if current_abs <= 0:
                continue
            reduction = min(current_abs, remaining)
            expense_contrib[primary_id] = current_abs - reduction
            reimbursement_reduction_by_tx[primary_id] += reduction
            remaining -= reduction

    return {
        'expense_contrib': expense_contrib,
        'reimbursement_offset_ids': reimbursement_offset_ids,
        'reimbursement_reduction_by_tx': reimbursement_reduction_by_tx,
    }


def _query_values(request, key):
    values = [value.strip() for value in request.GET.getlist(key) if value and value.strip()]
    if values:
        return values
    single = (request.GET.get(key) or '').strip()
    return [single] if single else []


def _apply_transaction_filters(qs, account_ids, project_ids, category_ids, subcategory_ids, currencies, direction):
    if account_ids:
        qs = qs.filter(account_id__in=account_ids)
    if project_ids:
        qs = qs.filter(expense_link__project_id__in=project_ids)
    if category_ids:
        qs = qs.filter(expense_link__category_id__in=category_ids)
    if subcategory_ids:
        qs = qs.filter(expense_link__subcategory_id__in=subcategory_ids)
    if currencies:
        qs = qs.filter(currency__in=currencies)
    if direction == 'income':
        qs = qs.filter(amount__gt=0)
    elif direction == 'expense':
        qs = qs.filter(amount__lt=0)
    return qs

@login_required
def dashboard_view(request):
    user = request.user
    now = timezone.localtime()
    current_month_start = timezone.make_aware(datetime(now.year, now.month, 1), timezone.get_current_timezone())
    start_param = request.GET.get('start')
    end_param = request.GET.get('end')
    account_values = [int(value) for value in _query_values(request, 'account') if value.isdigit()]
    project_values = [int(value) for value in _query_values(request, 'project') if value.isdigit()]
    category_values = [int(value) for value in _query_values(request, 'category') if value.isdigit()]
    subcategory_values = [int(value) for value in _query_values(request, 'subcategory') if value.isdigit()]
    currency_values = [(value or '').strip().upper() for value in _query_values(request, 'currency')]
    direction_param = (request.GET.get('direction') or '').strip().lower()
    report_currency = (request.GET.get('report_currency') or 'RUB').strip().upper()
    analytics_mode = (request.GET.get('analytics_mode') or 'net').strip().lower()
    chart_range = (request.GET.get('chart_range') or '2y').strip().lower()
    focus_month = (request.GET.get('focus_month') or '').strip()

    period_start = current_month_start
    if start_param:
        try:
            parsed = datetime.strptime(start_param, '%Y-%m-%d')
            period_start = timezone.make_aware(datetime.combine(parsed.date(), time.min), timezone.get_current_timezone())
        except ValueError:
            pass
    period_end = timezone.make_aware(datetime.combine(now.date(), time.max), timezone.get_current_timezone())
    if end_param:
        try:
            parsed = datetime.strptime(end_param, '%Y-%m-%d')
            period_end = timezone.make_aware(datetime.combine(parsed.date(), time.max), timezone.get_current_timezone())
        except ValueError:
            pass

    focus_period_start = None
    focus_period_end = None
    focus_month_label = ''
    if focus_month:
        try:
            focus_dt = datetime.strptime(focus_month, '%Y-%m')
            focus_period_start = timezone.make_aware(
                datetime.combine(datetime(focus_dt.year, focus_dt.month, 1).date(), time.min),
                timezone.get_current_timezone(),
            )
            if focus_dt.month == 12:
                next_month_dt = datetime(focus_dt.year + 1, 1, 1)
            else:
                next_month_dt = datetime(focus_dt.year, focus_dt.month + 1, 1)
            focus_period_end = timezone.make_aware(
                datetime.combine(next_month_dt.date(), time.min),
                timezone.get_current_timezone(),
            ) - timedelta(microseconds=1)
            focus_month_label = focus_period_start.strftime('%m.%Y')
        except ValueError:
            focus_month = ''

    effective_period_start = focus_period_start or period_start
    effective_period_end = focus_period_end or period_end

    base_qs = Transaction.objects.filter(account__user=user, is_split_parent=False)
    base_qs = _apply_transaction_filters(
        base_qs,
        account_values,
        project_values,
        category_values,
        subcategory_values,
        currency_values,
        direction_param,
    )
    filtered_qs = base_qs.filter(date__range=(effective_period_start, effective_period_end))

    chart_start = timezone.make_aware(datetime(now.year - 1, 1, 1), timezone.get_current_timezone())
    chart_end = timezone.make_aware(datetime(now.year, 12, 31, 23, 59, 59), timezone.get_current_timezone())
    if chart_range == 'all':
        first_chart_tx = base_qs.order_by('date').values_list('date', flat=True).first()
        if first_chart_tx:
            chart_start = first_chart_tx
            chart_end = period_end
        else:
            chart_start = period_start
            chart_end = period_end
    elif chart_range == 'period':
        chart_start = period_start
        chart_end = period_end

    tx_list = list(
        filtered_qs.select_related(
            'expense_link__project',
            'expense_link__category',
            'expense_link__subcategory',
            'account',
        )
    )
    chart_tx_list = list(
        base_qs.filter(date__range=(chart_start, chart_end)).select_related(
            'expense_link__project',
            'expense_link__category',
            'expense_link__subcategory',
            'account',
        )
    )
    rate_lookup = _build_rate_lookup(tx_list + chart_tx_list, report_currency)
    tx_ids = {tx.id for tx in tx_list}
    tx_date_by_id = {tx.id: tx.date for tx in tx_list}
    converted_by_tx = {}
    missing_rate_tx_ids = set()
    expense_contrib = {}
    expense_category_by_tx = {}
    account_summary_map = {}

    for tx in tx_list:
        tx_local_date = timezone.localtime(tx.date).date()
        converted_amount = _convert_amount(tx.amount, tx.currency, report_currency, tx_local_date, rate_lookup)
        converted_by_tx[tx.id] = converted_amount
        if converted_amount is None:
            missing_rate_tx_ids.add(tx.id)
            continue
        account_entry = account_summary_map.setdefault(
            tx.account.name,
            {'name': tx.account.name, 'balance': Decimal('0'), 'tx_count': 0}
        )
        account_entry['balance'] += converted_amount
        account_entry['tx_count'] += 1
        subcategory_name = ((tx.expense_link.subcategory.name if tx.expense_link.subcategory else '') or '').strip().lower()
        if converted_amount < 0 and subcategory_name != 'перевод между счетами':
            expense_contrib[tx.id] = abs(converted_amount)
            expense_category_by_tx[tx.id] = tx.expense_link.category.name if tx.expense_link_id else '—'

    reimbursement_groups = (
        TransactionLinkGroup.objects.filter(
            user=user,
            status=TransactionLinkGroup.Status.ACTIVE,
            link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
            items__transaction_id__in=tx_ids,
        )
        .distinct()
        .prefetch_related('items__transaction')
    )
    reimbursement_offset_ids = set()
    for group in reimbursement_groups:
        primary_ids = []
        offset_sum = Decimal('0')
        for item in group.items.all():
            converted_amount = converted_by_tx.get(item.transaction_id)
            if item.role == 'primary' and item.transaction_id in tx_ids and converted_amount is not None and converted_amount < 0:
                primary_ids.append(item.transaction_id)
            elif item.role == 'offset' and item.transaction_id in tx_ids and converted_amount is not None and converted_amount > 0:
                offset_sum += converted_amount
                reimbursement_offset_ids.add(item.transaction_id)
        if not primary_ids or offset_sum <= 0:
            continue
        ordered_primary_ids = sorted(
            [tx_id for tx_id in primary_ids if tx_id in expense_contrib],
            key=lambda tx_id: (tx_date_by_id.get(tx_id, effective_period_end), tx_id),
        )
        remaining = offset_sum
        for primary_id in ordered_primary_ids:
            if remaining <= 0:
                break
            current_abs = expense_contrib.get(primary_id, Decimal('0'))
            if current_abs <= 0:
                continue
            reduction = min(current_abs, remaining)
            expense_contrib[primary_id] = current_abs - reduction
            remaining -= reduction

    expense_raw_abs = sum(
        abs(amount) for tx_id, amount in converted_by_tx.items()
        if amount is not None and amount < 0
    )
    if analytics_mode == 'net':
        total_income = sum(
            amount for tx_id, amount in converted_by_tx.items()
            if amount is not None and amount > 0 and tx_id not in reimbursement_offset_ids
        )
    else:
        total_income = sum((amount for amount in converted_by_tx.values() if amount is not None and amount > 0), Decimal('0'))

    total_expense_abs = sum(expense_contrib.values(), Decimal('0')) if analytics_mode == 'net' else expense_raw_abs
    total_expense = -total_expense_abs
    expense_operation_count = sum(
        1
        for tx_id, amount in converted_by_tx.items()
        if amount is not None
        and amount < 0
        and (analytics_mode != 'net' or tx_id in expense_contrib)
    )
    average_expense = (total_expense_abs / expense_operation_count) if expense_operation_count else Decimal('0')
    net_amount = total_income - total_expense_abs
    has_expense = total_expense_abs > 0
    denominator = total_expense_abs if has_expense else Decimal('1')

    category_totals = {}
    category_source = expense_contrib if analytics_mode == 'net' else {
        tx.id: abs(converted_by_tx[tx.id])
        for tx in tx_list
        if converted_by_tx.get(tx.id) is not None
        and converted_by_tx[tx.id] < 0
        and ((tx.expense_link.subcategory.name if tx.expense_link and tx.expense_link.subcategory else '') or '').strip().lower() != 'перевод между счетами'
    }
    for tx_id, amount_abs in category_source.items():
        category_name = expense_category_by_tx.get(tx_id) or '—'
        category_totals[category_name] = category_totals.get(category_name, Decimal('0')) + amount_abs
    top_expense_categories_raw = sorted(category_totals.items(), key=lambda item: item[1], reverse=True)[:8]

    project_summary_map = {}
    pivot_summary_map = {}
    pivot_month_set = set()
    reimbursement_total = sum(
        converted_by_tx.get(tx_id, Decimal('0'))
        for tx_id in reimbursement_offset_ids
        if converted_by_tx.get(tx_id) is not None
    )
    income_operation_count = sum(
        1
        for tx_id, amount in converted_by_tx.items()
        if amount is not None and amount > 0 and (analytics_mode != 'net' or tx_id not in reimbursement_offset_ids)
    )
    for tx in tx_list:
        converted_amount = converted_by_tx.get(tx.id)
        if converted_amount is None or not tx.expense_link_id:
            continue
        subcategory_name = ((tx.expense_link.subcategory.name if tx.expense_link.subcategory else '') or '').strip().lower()
        is_internal_transfer = subcategory_name == 'перевод между счетами'
        if tx.amount > 0:
            if analytics_mode == 'net' and tx.id in reimbursement_offset_ids:
                continue
            income_value = converted_amount
            expense_value = Decimal('0')
            net_value = converted_amount
        else:
            income_value = Decimal('0')
            if is_internal_transfer:
                amount_abs = abs(converted_amount)
            elif analytics_mode == 'net':
                amount_abs = expense_contrib.get(tx.id)
                if amount_abs is None:
                    continue
            else:
                amount_abs = abs(converted_amount)
            expense_value = amount_abs
            net_value = -amount_abs
        month_bucket = timezone.localtime(tx.date).date().replace(day=1)
        pivot_month_set.add(month_bucket)
        project_name = tx.expense_link.project.name
        category_name = tx.expense_link.category.name
        subcategory_label = tx.expense_link.subcategory.name if tx.expense_link.subcategory else 'Без подкатегории'
        project_entry = project_summary_map.setdefault(
            project_name,
            {'income': Decimal('0'), 'expense': Decimal('0'), 'net': Decimal('0'), 'categories': {}}
        )
        project_entry['income'] += income_value
        project_entry['expense'] += expense_value
        project_entry['net'] += net_value
        category_entry = project_entry['categories'].setdefault(
            category_name,
            {'income': Decimal('0'), 'expense': Decimal('0'), 'net': Decimal('0'), 'subcategories': {}}
        )
        category_entry['income'] += income_value
        category_entry['expense'] += expense_value
        category_entry['net'] += net_value
        subcategory_entry = category_entry['subcategories'].setdefault(
            subcategory_label,
            {'income': Decimal('0'), 'expense': Decimal('0'), 'net': Decimal('0')}
        )
        subcategory_entry['income'] += income_value
        subcategory_entry['expense'] += expense_value
        subcategory_entry['net'] += net_value
        pivot_project = pivot_summary_map.setdefault(
            project_name,
            {
                'project_id': tx.expense_link.project_id,
                'months': defaultdict(lambda: Decimal('0')),
                'total': Decimal('0'),
                'categories': {},
            }
        )
        pivot_project['months'][month_bucket] += net_value
        pivot_project['total'] += net_value
        pivot_category = pivot_project['categories'].setdefault(
            category_name,
            {
                'category_id': tx.expense_link.category_id,
                'months': defaultdict(lambda: Decimal('0')),
                'total': Decimal('0'),
                'subcategories': {},
            }
        )
        pivot_category['months'][month_bucket] += net_value
        pivot_category['total'] += net_value
        pivot_category['subcategories'].setdefault(
            subcategory_label,
            {
                'subcategory_id': tx.expense_link.subcategory_id or '',
                'months': defaultdict(lambda: Decimal('0')),
                'total': Decimal('0'),
            }
        )
        pivot_category['subcategories'][subcategory_label]['months'][month_bucket] += net_value
        pivot_category['subcategories'][subcategory_label]['total'] += net_value

    chart_converted = {}
    chart_expense_contrib = {}
    month_income_map = defaultdict(lambda: Decimal('0'))
    month_expense_map = defaultdict(lambda: Decimal('0'))
    flow_map = defaultdict(lambda: Decimal('0'))
    for tx in chart_tx_list:
        tx_local_date = timezone.localtime(tx.date).date()
        converted_amount = _convert_amount(tx.amount, tx.currency, report_currency, tx_local_date, rate_lookup)
        chart_converted[tx.id] = converted_amount
        if converted_amount is None:
            continue
        subcategory_name = ((tx.expense_link.subcategory.name if tx.expense_link and tx.expense_link.subcategory else '') or '').strip().lower()
        if converted_amount < 0 and subcategory_name != 'перевод между счетами':
            chart_expense_contrib[tx.id] = abs(converted_amount)

    chart_reimbursement_groups = (
        TransactionLinkGroup.objects.filter(
            user=user,
            status=TransactionLinkGroup.Status.ACTIVE,
            link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
            items__transaction_id__in=list(chart_converted.keys()),
        )
        .distinct()
        .prefetch_related('items__transaction')
    )
    chart_reimbursement_offset_ids = set()
    chart_tx_date_by_id = {tx.id: tx.date for tx in chart_tx_list}
    for group in chart_reimbursement_groups:
        primary_ids = []
        offset_sum = Decimal('0')
        for item in group.items.all():
            converted_amount = chart_converted.get(item.transaction_id)
            if item.role == 'primary' and converted_amount is not None and converted_amount < 0:
                primary_ids.append(item.transaction_id)
            elif item.role == 'offset' and converted_amount is not None and converted_amount > 0:
                offset_sum += converted_amount
                chart_reimbursement_offset_ids.add(item.transaction_id)
        if not primary_ids or offset_sum <= 0:
            continue
        ordered_primary_ids = sorted(
            [tx_id for tx_id in primary_ids if tx_id in chart_expense_contrib],
            key=lambda tx_id: (chart_tx_date_by_id.get(tx_id, chart_end), tx_id),
        )
        remaining = offset_sum
        for primary_id in ordered_primary_ids:
            if remaining <= 0:
                break
            current_abs = chart_expense_contrib.get(primary_id, Decimal('0'))
            if current_abs <= 0:
                continue
            reduction = min(current_abs, remaining)
            chart_expense_contrib[primary_id] = current_abs - reduction
            remaining -= reduction

    for tx in chart_tx_list:
        converted_amount = chart_converted.get(tx.id)
        if converted_amount is None:
            continue
        bucket = timezone.localtime(tx.date).date().replace(day=1)
        month_key = bucket.strftime('%m.%Y')
        if converted_amount > 0:
            if analytics_mode == 'net' and tx.id in chart_reimbursement_offset_ids:
                continue
            flow_map[bucket] += converted_amount
            month_income_map[month_key] += converted_amount
        elif converted_amount < 0:
            if analytics_mode == 'net':
                expense_value = chart_expense_contrib.get(tx.id)
                if expense_value is None:
                    continue
                flow_map[bucket] -= expense_value
                month_expense_map[month_key] += expense_value
            else:
                flow_map[bucket] += converted_amount
                month_expense_map[month_key] += abs(converted_amount)

    trend_buckets = sorted(flow_map.keys())
    trend_labels = [bucket.strftime('%m.%Y') for bucket in trend_buckets]
    trend_values = [float(flow_map[bucket]) for bucket in trend_buckets]
    cumulative_total = Decimal('0')
    cumulative_values = []
    for value in trend_values:
        cumulative_total += Decimal(str(value))
        cumulative_values.append(float(cumulative_total))

    month_labels = sorted(set(month_income_map.keys()) | set(month_expense_map.keys()))
    month_income_values = [float(month_income_map[label]) for label in month_labels]
    month_expense_values = [float(month_expense_map[label]) for label in month_labels]

    pivot_months = sorted(pivot_month_set, reverse=True)
    pivot_month_labels = [month.strftime('%m.%Y') for month in pivot_months]
    pivot_month_count = len(pivot_months)

    def _pivot_cell(value, row_max_abs, month):
        intensity = 0
        if value and row_max_abs > 0:
            intensity = max(8, min(38, int((abs(value) / row_max_abs) * 38)))
        if value > 0:
            background = f'rgba(22, 163, 74, {intensity / 100:.2f})' if intensity else ''
        elif value < 0:
            background = f'rgba(220, 38, 38, {intensity / 100:.2f})' if intensity else ''
        else:
            background = ''
        return {
            'display': _format_amount(value, decimals=0) if value else '—',
            'raw': str(value),
            'month_label': month.strftime('%m.%Y'),
            'background': background,
        }

    def _pivot_values(month_map):
        row_max_abs = max((abs(month_map.get(month, Decimal('0'))) for month in pivot_months), default=Decimal('0'))
        return [_pivot_cell(month_map.get(month, Decimal('0')), row_max_abs, month) for month in pivot_months]

    pivot_rows = []
    for project_name, project_data in sorted(
        pivot_summary_map.items(),
        key=lambda item: sum(item[1]['months'].values()),
        reverse=True,
    ):
        pivot_rows.append({
            'label': project_name,
            'level': 0,
            'values': _pivot_values(project_data['months']),
            'total': _format_amount(project_data['total'], decimals=0) if project_data['total'] else '—',
            'average': _format_amount(project_data['total'] / pivot_month_count, decimals=0) if pivot_month_count and project_data['total'] else '—',
            'row_key': f"project-{len(pivot_rows)}",
            'parent_key': '',
            'project_id': project_data['project_id'],
            'category_id': '',
            'subcategory_id': '',
        })
        project_row_key = pivot_rows[-1]['row_key']
        for category_name, category_data in sorted(
            project_data['categories'].items(),
            key=lambda item: item[1]['total'],
            reverse=True,
        ):
            pivot_rows.append({
                'label': category_name,
                'level': 1,
                'values': _pivot_values(category_data['months']),
                'total': _format_amount(category_data['total'], decimals=0) if category_data['total'] else '—',
                'average': _format_amount(category_data['total'] / pivot_month_count, decimals=0) if pivot_month_count and category_data['total'] else '—',
                'row_key': f"{project_row_key}-category-{len(pivot_rows)}",
                'parent_key': project_row_key,
                'project_id': project_data['project_id'],
                'category_id': category_data['category_id'],
                'subcategory_id': '',
            })
            category_row_key = pivot_rows[-1]['row_key']
            for subcategory_name, subcategory_months in sorted(
                category_data['subcategories'].items(),
                key=lambda item: item[1]['total'],
                reverse=True,
            ):
                pivot_rows.append({
                    'label': subcategory_name,
                    'level': 2,
                    'values': _pivot_values(subcategory_months['months']),
                    'total': _format_amount(subcategory_months['total'], decimals=0) if subcategory_months['total'] else '—',
                    'average': _format_amount(subcategory_months['total'] / pivot_month_count, decimals=0) if pivot_month_count and subcategory_months['total'] else '—',
                    'row_key': f"{category_row_key}-subcategory-{len(pivot_rows)}",
                    'parent_key': category_row_key,
                    'project_id': project_data['project_id'],
                    'category_id': category_data['category_id'],
                    'subcategory_id': subcategory_months['subcategory_id'],
                })

    recent_transactions = []
    for tx in sorted(tx_list, key=lambda item: item.date, reverse=True)[:12]:
        converted_amount = converted_by_tx.get(tx.id)
        recent_transactions.append({
            'date': timezone.localtime(tx.date).strftime('%d.%m.%Y %H:%M'),
            'account': tx.account.name,
            'project': tx.expense_link.project.name if tx.expense_link_id else '—',
            'category': tx.expense_link.category.name if tx.expense_link_id else '—',
            'amount': f"{'+' if tx.amount >= 0 else '-'}{_format_amount(abs(tx.amount))} {tx.currency}",
            'report_amount': (
                f"{'+' if converted_amount >= 0 else '-'}{_format_amount(abs(converted_amount))} {report_currency}"
                if converted_amount is not None else 'Нет курса'
            ),
            'is_income': tx.amount >= 0,
            'comment': tx.comment or '—',
        })

    accounts = Account.objects.filter(user=user, status='active').order_by('name')
    projects = Project.objects.filter(user=user, status='active').order_by('name')
    categories = Category.objects.filter(user=user, status='active').order_by('name')
    subcategories = Subcategory.objects.filter(user=user, status='active').order_by('name')
    currencies = Currency.objects.filter(status='active').order_by('code')
    checklist_context = _build_checklist_context(user, period_start, period_end, tx_list, accounts)

    context = {
        'total_income': _format_amount(total_income, decimals=0),
        'total_expense': _format_amount(abs(total_expense), decimals=0),
        'net_amount': _format_amount(net_amount, decimals=0),
        'net_positive': net_amount >= 0,
        'operation_count': len(tx_list),
        'expense_operation_count': expense_operation_count,
        'average_expense': _format_amount(average_expense, decimals=0),
        'converted_transaction_count': len(tx_list) - len(missing_rate_tx_ids),
        'missing_rate_count': len(missing_rate_tx_ids),
        'income_operation_count': income_operation_count,
        'reimbursement_total': _format_amount(reimbursement_total, decimals=0),
        'filters': {
            'start': period_start.strftime('%Y-%m-%d'),
            'end': period_end.strftime('%Y-%m-%d'),
            'account': [str(value) for value in account_values],
            'project': [str(value) for value in project_values],
            'category': [str(value) for value in category_values],
            'subcategory': [str(value) for value in subcategory_values],
            'currency': currency_values,
            'direction': direction_param,
            'report_currency': report_currency,
            'analytics_mode': analytics_mode,
            'chart_range': chart_range,
            'focus_month': focus_month,
        },
        'accounts': accounts,
        'projects': projects,
        'categories': categories,
        'subcategories': subcategories,
        'currencies': currencies,
        'accounts_options': [
            {'id': account.id, 'name': account.name, 'currency': account.currency}
            for account in accounts
        ],
        'currency_choices': [
            {'code': currency.code, 'label': f'{currency.code} — {currency.name}'}
            for currency in currencies
        ],
        'project_tree': [
            {
                'id': project.id,
                'name': project.name,
                'categories': [
                    {
                        'id': category.id,
                        'name': category.name,
                        'subcategories': [
                            {
                                'id': subcategory.id,
                                'name': subcategory.name,
                            }
                            for subcategory in Subcategory.objects.filter(
                                user=user,
                                status='active',
                                expenselink__user=user,
                                expenselink__project=project,
                                expenselink__category=category,
                                expenselink__status='active',
                            ).distinct().order_by('name')
                        ],
                    }
                    for category in Category.objects.filter(
                        user=user,
                        status='active',
                        expenselink__user=user,
                        expenselink__project=project,
                        expenselink__status='active',
                    ).distinct().order_by('name')
                ],
            }
            for project in projects
        ],
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
        'report_currency': report_currency,
        'analytics_mode': analytics_mode,
        'top_expense_categories': [
            {
                'name': category_name or '—',
                'total': _format_amount(total_value or 0, decimals=0),
                'percent': round((total_value / denominator) * 100, 1) if has_expense else 0,
            }
            for category_name, total_value in top_expense_categories_raw
        ],
        'trend_labels': trend_labels,
        'trend_values': trend_values,
        'cumulative_values': cumulative_values,
        'month_labels': month_labels,
        'month_income_values': month_income_values,
        'month_expense_values': month_expense_values,
        'pivot_months': pivot_month_labels,
        'pivot_rows': pivot_rows,
        'focus_month_label': focus_month_label,
        'recent_transactions': recent_transactions,
        'project_summaries': [
            {
                'name': project_name,
                'income': _format_amount(project_data['income'], decimals=0),
                'expense': _format_amount(project_data['expense'], decimals=0),
                'net': _format_amount(project_data['net'], decimals=0),
                'is_net_positive': project_data['net'] >= 0,
                'categories': [
                    {
                        'name': category_name,
                        'income': _format_amount(category_data['income'], decimals=0),
                        'expense': _format_amount(category_data['expense'], decimals=0),
                        'net': _format_amount(category_data['net'], decimals=0),
                        'is_net_positive': category_data['net'] >= 0,
                        'subcategories': [
                            {
                                'name': subcategory_name,
                                'income': _format_amount(subcategory_total['income'], decimals=0),
                                'expense': _format_amount(subcategory_total['expense'], decimals=0),
                                'net': _format_amount(subcategory_total['net'], decimals=0),
                                'is_net_positive': subcategory_total['net'] >= 0,
                            }
                            for subcategory_name, subcategory_total in sorted(category_data['subcategories'].items(), key=lambda item: item[1]['expense'], reverse=True)
                        ],
                    }
                    for category_name, category_data in sorted(project_data['categories'].items(), key=lambda item: item[1]['expense'], reverse=True)
                ],
            }
            for project_name, project_data in sorted(project_summary_map.items(), key=lambda item: item[1]['expense'], reverse=True)
        ],
    }
    return render(request, 'dashboard.html', context)


@login_required
def dashboard_checklist_view(request):
    user = request.user
    now = timezone.localtime()
    start_param = request.GET.get('start')
    end_param = request.GET.get('end')
    period_start = None
    if start_param:
        try:
            parsed = datetime.strptime(start_param, '%Y-%m-%d')
            period_start = timezone.make_aware(datetime.combine(parsed.date(), time.min), timezone.get_current_timezone())
        except ValueError:
            pass
    if period_start is None:
        period_start = timezone.make_aware(datetime.combine(datetime(now.year, now.month, 1).date(), time.min), timezone.get_current_timezone())
    period_end = timezone.make_aware(datetime.combine(now.date(), time.max), timezone.get_current_timezone())
    if end_param:
        try:
            parsed = datetime.strptime(end_param, '%Y-%m-%d')
            period_end = timezone.make_aware(datetime.combine(parsed.date(), time.max), timezone.get_current_timezone())
        except ValueError:
            pass
    accounts = Account.objects.filter(user=user, status='active').order_by('name')
    tx_list = list(
        Transaction.objects.filter(account__user=user, is_split_parent=False, date__range=(period_start, period_end))
        .select_related('account', 'expense_link__subcategory')
    )
    context = {
        'filters': {
            'start': period_start.strftime('%Y-%m-%d'),
            'end': period_end.strftime('%Y-%m-%d'),
        },
        **_build_checklist_context(user, period_start, period_end, tx_list, accounts),
    }
    return render(request, 'dashboard-checklist.html', context)


@login_required
def dashboard_cell_transactions_view(request):
    user = request.user
    month_param = (request.GET.get('month') or '').strip()
    report_currency = (request.GET.get('report_currency') or 'RUB').strip().upper()
    analytics_mode = (request.GET.get('analytics_mode') or 'net').strip().lower()
    try:
        month_dt = datetime.strptime(month_param, '%Y-%m')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'invalid_month'}, status=400)

    period_start = timezone.make_aware(
        datetime.combine(datetime(month_dt.year, month_dt.month, 1).date(), time.min),
        timezone.get_current_timezone(),
    )
    if month_dt.month == 12:
        next_month = datetime(month_dt.year + 1, 1, 1)
    else:
        next_month = datetime(month_dt.year, month_dt.month + 1, 1)
    period_end = timezone.make_aware(
        datetime.combine(next_month.date(), time.min),
        timezone.get_current_timezone(),
    ) - timedelta(microseconds=1)

    account_values = [int(value) for value in _query_values(request, 'account') if value.isdigit()]
    currency_values = [(value or '').strip().upper() for value in _query_values(request, 'currency')]
    direction_param = (request.GET.get('direction') or '').strip().lower()

    scope_project_values = [int(value) for value in _query_values(request, 'project') if value.isdigit()]
    scope_category_values = [int(value) for value in _query_values(request, 'category') if value.isdigit()]
    scope_subcategory_values = [int(value) for value in _query_values(request, 'subcategory') if value.isdigit()]

    drill_project_id = request.GET.get('drill_project_id') or request.GET.get('project_id')
    drill_category_id = request.GET.get('drill_category_id') or request.GET.get('category_id')
    drill_subcategory_id = request.GET.get('drill_subcategory_id') or request.GET.get('subcategory_id')
    drill_project_values = [int(drill_project_id)] if drill_project_id and drill_project_id.isdigit() else []
    drill_category_values = [int(drill_category_id)] if drill_category_id and drill_category_id.isdigit() else []
    drill_subcategory_values = [int(drill_subcategory_id)] if drill_subcategory_id and drill_subcategory_id.isdigit() else []

    scope_qs = Transaction.objects.filter(
        account__user=user,
        is_split_parent=False,
        date__range=(period_start, period_end),
    ).select_related(
        'account',
        'expense_link__project',
        'expense_link__category',
        'expense_link__subcategory',
    )
    scope_qs = _apply_transaction_filters(
        scope_qs,
        account_values,
        scope_project_values,
        scope_category_values,
        scope_subcategory_values,
        currency_values,
        direction_param,
    )
    drill_qs = scope_qs
    if drill_project_values or drill_category_values or drill_subcategory_values:
        drill_qs = _apply_transaction_filters(
            drill_qs,
            [],
            drill_project_values,
            drill_category_values,
            drill_subcategory_values,
            [],
            '',
        )

    scope_tx_list = list(scope_qs.order_by('-date', '-id'))
    tx_list = list(drill_qs.order_by('-date', '-id')[:300])
    rate_lookup = _build_rate_lookup(scope_tx_list, report_currency)
    converted_by_tx = {}
    for tx in scope_tx_list:
        converted_by_tx[tx.id] = _convert_amount(
            tx.amount,
            tx.currency,
            report_currency,
            timezone.localtime(tx.date).date(),
            rate_lookup,
        )

    reimbursement_state = _build_reimbursement_netting(scope_tx_list, converted_by_tx)
    expense_contrib = reimbursement_state['expense_contrib']
    reimbursement_offset_ids = reimbursement_state['reimbursement_offset_ids']
    reimbursement_reduction_by_tx = reimbursement_state['reimbursement_reduction_by_tx']

    transactions = []
    total_converted = Decimal('0')
    converted_count = 0
    missing_count = 0
    for tx in tx_list:
        converted_amount = converted_by_tx.get(tx.id)
        is_reimbursement_offset = tx.id in reimbursement_offset_ids
        reimbursement_reduction = reimbursement_reduction_by_tx.get(tx.id, Decimal('0'))

        effective_amount = converted_amount
        if analytics_mode == 'net' and converted_amount is not None:
            subcategory_name = (
                (tx.expense_link.subcategory.name if tx.expense_link_id and tx.expense_link.subcategory else '') or ''
            ).strip().lower()
            if converted_amount > 0 and is_reimbursement_offset:
                effective_amount = Decimal('0')
            elif converted_amount < 0 and subcategory_name == 'перевод между счетами':
                effective_amount = converted_amount
            elif converted_amount < 0:
                amount_abs = expense_contrib.get(tx.id)
                if amount_abs is not None:
                    effective_amount = -amount_abs

        if converted_amount is not None:
            total_converted += effective_amount
            converted_count += 1
        else:
            missing_count += 1

        raw_report_amount = (
            f"{'+' if converted_amount >= 0 else '-'}{_format_amount(abs(converted_amount))} {report_currency}"
            if converted_amount is not None else 'Нет курса'
        )
        effective_report_amount = (
            f"{'+' if effective_amount >= 0 else '-'}{_format_amount(abs(effective_amount))} {report_currency}"
            if effective_amount is not None else 'Нет курса'
        )
        transactions.append({
            'id': tx.id,
            'date': timezone.localtime(tx.date).strftime('%d.%m.%Y %H:%M'),
            'date_iso': timezone.localtime(tx.date).strftime('%Y-%m-%dT%H:%M'),
            'amount': f"{'+' if tx.amount >= 0 else '-'}{_format_amount(abs(tx.amount))} {tx.currency}",
            'amount_raw': str(tx.amount),
            'is_income': tx.amount >= 0,
            'currency_code': tx.currency,
            'account_id': tx.account_id,
            'project_id': tx.expense_link.project_id if tx.expense_link_id else '',
            'category_id': tx.expense_link.category_id if tx.expense_link_id else '',
            'subcategory_id': tx.expense_link.subcategory_id if tx.expense_link_id and tx.expense_link.subcategory_id else '',
            'account': tx.account.name,
            'project': tx.expense_link.project.name if tx.expense_link_id else '—',
            'category': tx.expense_link.category.name if tx.expense_link_id else '—',
            'subcategory': tx.expense_link.subcategory.name if tx.expense_link_id and tx.expense_link.subcategory else '—',
            'comment': tx.comment or '—',
            'comment_raw': tx.comment or '',
            'expense_link_id': tx.expense_link_id or '',
            'report_amount': effective_report_amount,
            'raw_report_amount': raw_report_amount,
            'is_reimbursement_offset': is_reimbursement_offset,
            'is_excluded_in_net': analytics_mode == 'net' and is_reimbursement_offset,
            'reimbursement_applied_amount': _format_amount(reimbursement_reduction, decimals=0) if reimbursement_reduction > 0 else '',
            'has_reimbursement_adjustment': analytics_mode == 'net' and reimbursement_reduction > 0,
            'is_fully_reimbursed': analytics_mode == 'net' and effective_amount == 0 and converted_amount is not None and converted_amount < 0,
        })

    return JsonResponse({
        'ok': True,
        'month': period_start.strftime('%m.%Y'),
        'count': drill_qs.count(),
        'total': _format_amount(total_converted, decimals=0),
        'total_label': 'сумма с учетом возвратов' if analytics_mode == 'net' else 'сумма',
        'report_currency': report_currency,
        'analytics_mode': analytics_mode,
        'converted_count': converted_count,
        'missing_count': missing_count,
        'transactions': transactions,
    })

@login_required
def categories_settings(request):
    user = request.user
    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    if preferences.default_project and preferences.default_project.status != 'active':
        preferences.default_project = None
        preferences.save(update_fields=['default_project'])

    open_project_id = None
    open_category_id = None
    open_param = request.GET.get('open')
    category_param = request.GET.get('category')
    if open_param and open_param.isdigit():
        open_project_id = int(open_param)

    if category_param and category_param.isdigit():
        open_category_id = int(category_param)

    if request.method == "POST" and 'set_default_project' in request.POST:
        project_id = request.POST.get('project_id')
        project = get_object_or_404(Project, pk=project_id, user=user, status='active')
        preferences.default_project = project
        preferences.save()
        target = project.id
        category_param = request.POST.get('category_id')
        url = f"{reverse('categories_settings')}?open={target}"
        if category_param:
            url += f"&category={category_param}"
        return redirect(url)

    # Обработка редактирования проекта
    if request.method == "POST" and 'edit_project' in request.POST:
        project_id = request.POST.get('project_id')
        project = get_object_or_404(Project, pk=project_id, user=user, status='active')
        project.name = (request.POST.get('name') or project.name).strip()
        project.description = (request.POST.get('description') or '').strip()
        if project.name:
            project.save(update_fields=['name', 'description'])
        return redirect(f"{reverse('categories_settings')}?open={project.id}")

    # Обработка удаления проекта
    if request.method == "POST" and 'delete_project' in request.POST:
        project_id = request.POST.get('project_id')
        project = get_object_or_404(Project, pk=project_id, user=user, status='active')
        project.status = 'deleted'
        project.save(update_fields=['status'])
        ExpenseLink.objects.filter(user=user, project=project, status='active').update(status='deleted')
        if preferences.default_project_id == project.id:
            preferences.default_project = None
            preferences.save(update_fields=['default_project'])
        return redirect('categories_settings')

    # Обработка добавления проекта
    if request.method == "POST" and 'add_project' in request.POST:
        project_form = ProjectForm(request.POST)
        if project_form.is_valid():
            new_project = project_form.save(commit=False)
            new_project.user = user
            new_project.status = 'active'
            new_project.save()
            return redirect(f"{reverse('categories_settings')}?open={new_project.id}")
    else:
        project_form = ProjectForm()

    # Обработка добавления категории
    if request.method == "POST" and 'add_category' in request.POST:
        category_form = CategoryForm(request.POST)
        project_id = request.POST.get('project_id')
        if category_form.is_valid() and project_id:
            new_category = category_form.save(commit=False)
            new_category.user = user
            new_category.status = 'active'
            new_category.save()
            # Cоздаем ExpenseLink
            ExpenseLink.objects.create(
                user=user,
                project_id=project_id,
                category=new_category,
                status='active'
            )
            return redirect(f"{reverse('categories_settings')}?open={project_id}&category={new_category.id}")
    else:
        category_form = CategoryForm()

    # Обработка редактирования категории
    if request.method == "POST" and 'edit_category' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        has_category_link = ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category_id=category_id,
            status='active'
        ).exists()
        if not has_category_link:
            get_object_or_404(Category, pk=category_id, user=user, status='active')
            get_object_or_404(Project, pk=project_id, user=user, status='active')
        category = get_object_or_404(Category, pk=category_id, user=user, status='active')
        category.name = (request.POST.get('name') or category.name).strip()
        if category.name:
            category.save(update_fields=['name'])
        return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category.id}")

    # Обработка удаления категории
    if request.method == "POST" and 'delete_category' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        has_category_link = ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category_id=category_id,
            status='active'
        ).exists()
        if not has_category_link:
            get_object_or_404(Category, pk=category_id, user=user, status='active')
            get_object_or_404(Project, pk=project_id, user=user, status='active')
        category = get_object_or_404(Category, pk=category_id, user=user, status='active')

        links_to_delete = ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category=category,
            status='active'
        )
        subcategory_ids = list(
            links_to_delete.exclude(subcategory__isnull=True).values_list('subcategory_id', flat=True).distinct()
        )
        links_to_delete.update(status='deleted')
        has_active_category_links = ExpenseLink.objects.filter(
            user=user, category=category, status='active'
        ).exists()
        if not has_active_category_links:
            category.status = 'deleted'
            category.save(update_fields=['status'])

        for subcategory_id in subcategory_ids:
            has_active_links = ExpenseLink.objects.filter(
                user=user, subcategory_id=subcategory_id, status='active'
            ).exists()
            if not has_active_links:
                Subcategory.objects.filter(pk=subcategory_id, user=user, status='active').update(status='deleted')

        return redirect(f"{reverse('categories_settings')}?open={project_id}")

    # Обработка добавления подкатегории
    if request.method == "POST" and 'add_subcategory' in request.POST:
        subcategory_form = SubcategoryForm(request.POST)
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        if subcategory_form.is_valid() and project_id and category_id:
            new_subcategory = subcategory_form.save(commit=False)
            new_subcategory.user = user
            new_subcategory.status = 'active'
            new_subcategory.save()
            ExpenseLink.objects.create(
                user=user,
                project_id=project_id,
                category_id=category_id,
                subcategory=new_subcategory,
                status='active'
            )
            return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category_id}")
    else:
        subcategory_form = SubcategoryForm()

    # Обработка редактирования подкатегории
    if request.method == "POST" and 'edit_subcategory' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        subcategory_id = request.POST.get('subcategory_id')
        has_subcategory_link = ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category_id=category_id,
            subcategory_id=subcategory_id,
            status='active'
        ).exists()
        if not has_subcategory_link:
            get_object_or_404(Subcategory, pk=subcategory_id, user=user, status='active')
            get_object_or_404(Category, pk=category_id, user=user, status='active')
            get_object_or_404(Project, pk=project_id, user=user, status='active')
        subcategory = get_object_or_404(Subcategory, pk=subcategory_id, user=user, status='active')
        subcategory.name = (request.POST.get('name') or subcategory.name).strip()
        if subcategory.name:
            subcategory.save(update_fields=['name'])
        return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category_id}")

    # Обработка удаления подкатегории
    if request.method == "POST" and 'delete_subcategory' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        subcategory_id = request.POST.get('subcategory_id')
        has_subcategory_link = ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category_id=category_id,
            subcategory_id=subcategory_id,
            status='active'
        ).exists()
        if not has_subcategory_link:
            get_object_or_404(Subcategory, pk=subcategory_id, user=user, status='active')
            get_object_or_404(Category, pk=category_id, user=user, status='active')
            get_object_or_404(Project, pk=project_id, user=user, status='active')
        subcategory = get_object_or_404(Subcategory, pk=subcategory_id, user=user, status='active')
        ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category_id=category_id,
            subcategory=subcategory,
            status='active'
        ).update(status='deleted')

        has_active_links = ExpenseLink.objects.filter(
            user=user, subcategory=subcategory, status='active'
        ).exists()
        if not has_active_links:
            subcategory.status = 'deleted'
            subcategory.save(update_fields=['status'])
        return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category_id}")

    # Cтроим дерево, как раньше:
    projects = Project.objects.filter(user=user, status="active")
    expense_links = ExpenseLink.objects.filter(user=user, status="active")
    tree = []
    project_categories_map = {}
    for project in projects:
        links_in_project = expense_links.filter(project=project)
        categories = Category.objects.filter(
            id__in=links_in_project.values_list('category', flat=True).distinct(),
            status='active'
        )
        cat_list = []
        for category in categories:
            links_in_category = links_in_project.filter(category=category)
            subcat_ids = links_in_category.values_list('subcategory', flat=True).distinct()
            subcategories = Subcategory.objects.filter(
                id__in=[sid for sid in subcat_ids if sid],
                status='active'
            )
            cat_list.append({'category': category, 'subcategories': subcategories})
        project_categories_map[project.id] = [item['category'].id for item in cat_list]
        tree.append({'project': project, 'categories': cat_list})

    if open_project_id is None:
        open_project_id = None

    if open_project_id and not open_category_id:
        category_ids = project_categories_map.get(open_project_id, [])
        if category_ids:
            open_category_id = category_ids[0]

    # validate provided category belongs to project
    if open_project_id and open_category_id:
        valid_ids = set(project_categories_map.get(open_project_id, []))
        if open_category_id not in valid_ids and valid_ids:
            open_category_id = next(iter(valid_ids))

    context = {
        'tree': tree,
        'project_form': project_form,
        'category_form': category_form,
        'subcategory_form': subcategory_form,
        'default_project_id': preferences.default_project_id,
        'open_project_id': open_project_id,
        'open_category_id': open_category_id,
    }
    return render(request, 'categories.html', context)


@login_required
def accounts_directory(request):
    user = request.user
    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    if preferences.default_account and preferences.default_account.status != 'active':
        preferences.default_account = None
        preferences.save(update_fields=['default_account'])
    accounts = Account.objects.filter(user=user).exclude(status='deleted').order_by('status', 'name')

    raw_balance_target_date = request.POST.get('balance_target_date') or request.GET.get('balance_target_date')
    today = timezone.localdate()
    if raw_balance_target_date:
        try:
            balance_target_date = datetime.strptime(raw_balance_target_date, '%Y-%m-%d').date()
        except ValueError:
            balance_target_date = today
    else:
        balance_target_date = today
    if balance_target_date > today:
        balance_target_date = today

    def redirect_accounts_directory():
        base_url = reverse('accounts_directory')
        return redirect(f'{base_url}?balance_target_date={balance_target_date.isoformat()}')

    form = AccountForm()
    balance_form = AccountBalanceSnapshotForm(initial={'snapshot_date': balance_target_date})
    edit_forms = {}
    modal_to_open = None
    modal_context = {}

    if request.method == "POST":
        if 'create_account' in request.POST:
            form = AccountForm(request.POST)
            if form.is_valid():
                account = form.save(commit=False)
                account.user = user
                if Account.objects.filter(user=user, name__iexact=account.name).exists():
                    form.add_error('name', 'Счёт с таким названием уже существует')
                else:
                    account.save()
                    return redirect_accounts_directory()
            modal_to_open = 'accountModal'
        elif 'edit_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account.objects.exclude(status='deleted'), pk=account_id, user=user)
            edit_form = AccountForm(request.POST, instance=account)
            if edit_form.is_valid():
                new_name = edit_form.cleaned_data['name']
                if Account.objects.filter(user=user, name__iexact=new_name).exclude(pk=account.id).exists():
                    edit_form.add_error('name', 'Счёт с таким названием уже существует')
                else:
                    edit_form.save()
                    return redirect_accounts_directory()
            edit_forms[account.id] = edit_form
            modal_to_open = f'editAccountModal{account.id}'
        elif 'delete_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account.objects.exclude(status='deleted'), pk=account_id, user=user)
            account.status = 'deleted'
            account.save()
            if preferences.default_account_id == account.id:
                preferences.default_account = None
                preferences.save(update_fields=['default_account'])
            return redirect_accounts_directory()
        elif 'set_default_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id, user=user, status='active')
            preferences.default_account = account
            preferences.save()
            return redirect_accounts_directory()
        elif 'save_balance_snapshot' in request.POST:
            account_id = request.POST.get('account_id')
            snapshot_id = request.POST.get('snapshot_id')
            if snapshot_id:
                snapshot = get_object_or_404(
                    AccountBalanceSnapshot.objects.select_related('account'),
                    pk=snapshot_id,
                    account__user=user,
                    account__status__in=['active', 'archived'],
                )
                account = snapshot.account
            else:
                snapshot = None
                account = get_object_or_404(Account, pk=account_id, user=user, status='active')
            balance_form = AccountBalanceSnapshotForm(request.POST)
            if balance_form.is_valid():
                if snapshot_id:
                    existing_snapshot = AccountBalanceSnapshot.objects.filter(
                        account=account,
                        snapshot_date=balance_form.cleaned_data['snapshot_date'],
                    ).exclude(pk=snapshot.id).first()
                    if existing_snapshot:
                        balance_form.add_error('snapshot_date', 'На эту дату по счёту уже есть фиксация.')
                    else:
                        snapshot.account = account
                        snapshot.snapshot_date = balance_form.cleaned_data['snapshot_date']
                        snapshot.balance = balance_form.cleaned_data['balance']
                        snapshot.note = balance_form.cleaned_data['note']
                        snapshot.save()
                        return redirect_accounts_directory()
                else:
                    AccountBalanceSnapshot.objects.update_or_create(
                        account=account,
                        snapshot_date=balance_form.cleaned_data['snapshot_date'],
                        defaults={
                            'balance': balance_form.cleaned_data['balance'],
                            'note': balance_form.cleaned_data['note'],
                        },
                    )
                    return redirect_accounts_directory()
            modal_to_open = 'balanceSnapshotModal'
            modal_context = {
                'snapshot_modal_account_id': account.id,
                'snapshot_modal_account_name': account.name,
                'snapshot_modal_id': snapshot_id or '',
            }
        elif 'copy_balance_snapshot' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id, user=user, status='active')
            latest_snapshot = account.balance_snapshots.filter(snapshot_date__lt=balance_target_date).first()
            if latest_snapshot:
                AccountBalanceSnapshot.objects.update_or_create(
                    account=account,
                    snapshot_date=balance_target_date,
                    defaults={
                        'balance': latest_snapshot.balance,
                        'note': 'Без изменений',
                    },
                )
            return redirect_accounts_directory()
        elif 'delete_balance_snapshot' in request.POST:
            snapshot_id = request.POST.get('snapshot_id')
            snapshot = get_object_or_404(
                AccountBalanceSnapshot.objects.select_related('account'),
                pk=snapshot_id,
                account__user=user,
                account__status__in=['active', 'archived'],
            )
            snapshot.delete()
            return redirect_accounts_directory()

    accounts = accounts.prefetch_related('balance_snapshots')
    active_accounts = Account.objects.filter(user=user, status='active').prefetch_related('balance_snapshots').order_by('name')
    last_transaction_map = {
        row['account_id']: row['last_transaction_at']
        for row in Transaction.objects
        .filter(
            account__user=user,
            account__status='active',
            is_split_parent=False,
            date__date__lte=balance_target_date,
        )
        .values('account_id')
        .annotate(last_transaction_at=Max('date'))
    }
    accounts_with_forms = []
    balance_summary = []
    for account in accounts:
        edit_form = edit_forms.get(account.id, AccountForm(instance=account))
        accounts_with_forms.append((account, edit_form))

    for account in active_accounts:
        snapshots = list(account.balance_snapshots.all())
        latest_snapshot = snapshots[0] if snapshots else None
        first_snapshot = snapshots[-1] if snapshots else None
        target_snapshot = next((snapshot for snapshot in snapshots if snapshot.snapshot_date == balance_target_date), None)
        previous_snapshot = next((snapshot for snapshot in snapshots if snapshot.snapshot_date < balance_target_date), None)

        if previous_snapshot:
            interval_delta = (
                Transaction.objects
                .filter(
                    account=account,
                    is_split_parent=False,
                    date__date__gt=previous_snapshot.snapshot_date,
                    date__date__lte=balance_target_date,
                )
                .aggregate(total=Sum('amount'))
                .get('total') or Decimal('0')
            )
            expected_on_target = previous_snapshot.balance + interval_delta
        else:
            expected_on_target = None

        if target_snapshot and previous_snapshot and expected_on_target is not None:
            discrepancy = target_snapshot.balance - expected_on_target
        else:
            discrepancy = None

        has_target_snapshot = target_snapshot is not None
        check_amount = discrepancy
        has_check_difference = (
            check_amount is not None
            and check_amount.quantize(Decimal('0.01')) != Decimal('0.00')
        )
        can_copy_snapshot = previous_snapshot is not None and not has_target_snapshot

        balance_summary.append({
            'account': account,
            'latest_snapshot': latest_snapshot,
            'first_snapshot': first_snapshot,
            'target_snapshot': target_snapshot,
            'previous_snapshot': previous_snapshot,
            'last_transaction_at': last_transaction_map.get(account.id),
            'expected_on_target': expected_on_target,
            'discrepancy': discrepancy,
            'check_amount': check_amount,
            'has_check_difference': has_check_difference,
            'has_target_snapshot': has_target_snapshot,
            'can_copy_snapshot': can_copy_snapshot,
        })

    balance_summary.sort(
        key=lambda row: (
            row['has_target_snapshot'],
            row['account'].name.lower(),
        )
    )
    balance_snapshot_done_count = sum(1 for row in balance_summary if row['has_target_snapshot'])
    balance_snapshot_total_count = len(balance_summary)
    balance_snapshot_pending_count = balance_snapshot_total_count - balance_snapshot_done_count

    context = {
        'accounts_with_forms': accounts_with_forms,
        'balance_form': balance_form,
        'balance_summary': balance_summary,
        'balance_target_date': balance_target_date,
        'balance_snapshot_done_count': balance_snapshot_done_count,
        'balance_snapshot_total_count': balance_snapshot_total_count,
        'balance_snapshot_pending_count': balance_snapshot_pending_count,
        'balance_history': AccountBalanceSnapshot.objects.filter(
            account__user=user,
            account__status__in=['active', 'archived'],
        ).select_related('account'),
        'form': form,
        'modal_to_open': modal_to_open,
        'default_account_id': preferences.default_account_id,
        **modal_context,
    }
    return render(request, 'accounts/account-directory.html', context)


@login_required
def currencies_directory(request):
    ru_names = {
        "RUB": "Российский рубль",
        "KZT": "Казахстанский тенге",
        "USD": "Доллар США",
        "EUR": "Евро",
        "JPY": "Японская иена",
        "USDT": "Тезер (USDT)",
        "USDC": "USD Coin (USDC)",
        "USDE": "Ethena USDe (USDE)",
        "TRY": "Турецкая лира",
        "CNY": "Китайский юань",
        "GBP": "Фунт стерлингов",
        "CHF": "Швейцарский франк",
        "AED": "Дирхам ОАЭ",
        "UAH": "Украинская гривна",
        "BYN": "Белорусский рубль",
        "GEL": "Грузинский лари",
        "AMD": "Армянский драм",
        "UZS": "Узбекский сум",
        "THB": "Тайский бат",
        "INR": "Индийская рупия",
        "KRW": "Южнокорейская вона",
        "HKD": "Гонконгский доллар",
        "SGD": "Сингапурский доллар",
    }

    if request.method == "POST":
        if request.POST.get("save_currencies") == "1":
            active_codes = set(request.POST.getlist("active_codes"))
            Currency.objects.exclude(code__in=active_codes).update(status="archived")
            if active_codes:
                Currency.objects.filter(code__in=active_codes).update(status="active")
            return redirect("currencies_directory")

    currencies = Currency.objects.all().order_by("code")
    currencies_view = [
        {
            "code": c.code,
            "name": c.name,
            "status": c.status,
            "russian_name": ru_names.get(c.code) or _transliterate_to_ru(c.name),
        }
        for c in currencies
    ]
    context = {
        "currencies": currencies_view,
    }
    return render(request, "core/currencies.html", context)


@login_required
def currency_rates_view(request):
    full_sync_default_start = datetime(2020, 1, 1).date()
    today = timezone.localdate()
    active_currencies = list(Currency.objects.filter(status="active").order_by("code"))
    target_selection = (request.POST.get("target_selection") or "ACTIVE").strip().upper() if request.method == "POST" else "ACTIVE"
    start_date_value = request.POST.get("start_date") if request.method == "POST" else ""

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "sync_full":
                if start_date_value:
                    start_date = datetime.strptime(start_date_value, "%Y-%m-%d").date()
                else:
                    start_date = full_sync_default_start
                if start_date > today:
                    raise ValueError("Дата начала не может быть позже сегодняшней.")

                if target_selection == "ACTIVE":
                    currency_codes = [currency.code for currency in active_currencies]
                    target_label = "все активные валюты"
                else:
                    currency_codes = [target_selection]
                    target_label = target_selection

                sync_result = sync_cbr_rates_full(
                    currency_codes=currency_codes,
                    start_date=start_date,
                    end_date=today,
                )
                summary = (
                    f"Первичная загрузка ({target_label}) завершена: inserted={sync_result['inserted']}, "
                    f"updated={sync_result['updated']}, synced={', '.join(sync_result['synced']) or '—'}."
                )
                if sync_result["skipped"]:
                    summary += f" Пропущено: {', '.join(sync_result['skipped'])}."
                if sync_result.get("failed"):
                    summary += f" Ошибки: {'; '.join(sync_result['failed'])}."
                    messages.warning(request, summary)
                else:
                    messages.success(request, summary)
                return redirect("currency_rates")
            if action == "sync_crypto_historical":
                start_date_value = (request.POST.get("crypto_start_date") or "2020-01-01").strip()
                start_date = datetime.strptime(start_date_value, "%Y-%m-%d").date()
                if start_date > today:
                    raise ValueError("Дата начала не может быть позже сегодняшней.")
                crypto_codes = portfolio_crypto_symbols(
                    [currency.code for currency in active_currencies]
                )
                sync_result = sync_crypto_rates_historical(crypto_codes, start_date, today)
                summary = (
                    f"История крипты ({sync_result.get('source')}): "
                    f"inserted={sync_result['inserted']}, updated={sync_result['updated']}, "
                    f"дней={sync_result['days_written']}."
                )
                if sync_result["synced"]:
                    summary += f" Монеты: {', '.join(sync_result['synced'])}."
                if sync_result["skipped"]:
                    summary += f" Пропущено: {', '.join(sync_result['skipped'])}."
                if sync_result["days_skipped_no_usd"]:
                    summary += f" Без USD ЦБ: {sync_result['days_skipped_no_usd']} дн."
                if sync_result.get("failed"):
                    summary += f" Ошибки: {'; '.join(sync_result['failed'])}."
                    messages.warning(request, summary)
                else:
                    messages.success(request, summary)
                return redirect("currency_rates")
            if action == "sync_crypto":
                crypto_codes = portfolio_crypto_symbols(
                    [currency.code for currency in active_currencies]
                )
                sync_result = sync_crypto_rates_incremental(crypto_codes, end_date=today)
                summary = (
                    f"Курсы ({sync_result.get('source') or '—'}): "
                    f"inserted={sync_result['inserted']}, updated={sync_result['updated']}."
                )
                if sync_result["skipped"]:
                    summary += f" Пропущено: {', '.join(sync_result['skipped'])}."
                if sync_result.get("failed"):
                    summary += f" Ошибки: {'; '.join(sync_result['failed'])}."
                    messages.warning(request, summary)
                else:
                    messages.success(request, summary)
                return redirect("currency_rates")
            if action == "sync_incremental":
                currency_codes = [currency.code for currency in active_currencies]
                sync_result = sync_cbr_rates_incremental(
                    currency_codes=currency_codes,
                    default_start_date=full_sync_default_start,
                    end_date=today,
                )
                summary = (
                    f"Догрузка новых дат (все активные валюты) завершена: inserted={sync_result['inserted']}, "
                    f"updated={sync_result['updated']}, synced={', '.join(sync_result['synced']) or '—'}."
                )
                if sync_result["skipped"]:
                    summary += f" Пропущено: {', '.join(sync_result['skipped'])}."
                if sync_result.get("failed"):
                    summary += f" Ошибки: {'; '.join(sync_result['failed'])}."
                    messages.warning(request, summary)
                else:
                    messages.success(request, summary)
                return redirect("currency_rates")
        except Exception as exc:
            messages.error(request, f"Не удалось загрузить курсы ЦБ: {exc}")

    rate_coverage = (
        CurrencyRate.objects.values("currency")
        .annotate(first_date=Min("date"), latest_date=Max("date"))
        .order_by("currency")
    )
    rate_dates_map = {
        row["currency"]: {"first_date": row["first_date"], "latest_date": row["latest_date"]}
        for row in rate_coverage
    }
    currency_rows = [
        {
            "code": currency.code,
            "name": currency.name,
            "status": currency.status,
            "first_date": rate_dates_map.get(currency.code, {}).get("first_date"),
            "latest_date": rate_dates_map.get(currency.code, {}).get("latest_date"),
        }
        for currency in active_currencies
    ]
    recent_rates_qs = CurrencyRate.objects.order_by("-date", "currency")
    recent_rates_paginator = Paginator(recent_rates_qs, 25)
    recent_rates_page = recent_rates_paginator.get_page(request.GET.get("rates_page"))
    context = {
        "currency_rows": currency_rows,
        "currency_choices": active_currencies,
        "recent_rates": recent_rates_page,
        "full_sync_default_start": full_sync_default_start,
        "today": today,
        "target_selection": target_selection,
        "start_date_value": start_date_value or full_sync_default_start.strftime("%Y-%m-%d"),
    }
    return render(request, "core/currency-rates.html", context)


@login_required
def currency_converter_view(request):
    ru_names = {
        "RUB": "Российский рубль",
        "KZT": "Казахстанский тенге",
        "USD": "Доллар США",
        "EUR": "Евро",
        "JPY": "Японская иена",
        "USDT": "Тезер (USDT)",
        "USDC": "USD Coin (USDC)",
        "USDE": "Ethena USDe (USDE)",
        "TRY": "Турецкая лира",
        "CNY": "Китайский юань",
        "GBP": "Фунт стерлингов",
        "CHF": "Швейцарский франк",
        "AED": "Дирхам ОАЭ",
        "UAH": "Украинская гривна",
        "BYN": "Белорусский рубль",
        "GEL": "Грузинский лари",
        "AMD": "Армянский драм",
        "UZS": "Узбекский сум",
        "THB": "Тайский бат",
        "INR": "Индийская рупия",
        "KRW": "Южнокорейская вона",
        "HKD": "Гонконгский доллар",
        "SGD": "Сингапурский доллар",
    }
    currency_choices = [
        (
            currency.code,
            f"{currency.code} — {ru_names.get(currency.code) or _transliterate_to_ru(currency.name)} / {currency.name}"
        )
        for currency in Currency.objects.order_by("code")
    ]
    form = CurrencyConverterForm(request.GET or None, currency_choices=currency_choices)
    conversion = None

    if form.is_valid():
        amount = form.cleaned_data["amount"]
        from_currency = form.cleaned_data["from_currency"]
        to_currency = form.cleaned_data["to_currency"]
        rate_date = form.cleaned_data["rate_date"]

        from_rate = CurrencyRate.objects.filter(currency=from_currency, date=rate_date).first()
        to_rate = CurrencyRate.objects.filter(currency=to_currency, date=rate_date).first()

        if not from_rate:
            messages.error(request, f"Нет курса для {from_currency} на {rate_date:%d.%m.%Y}.")
        elif not to_rate:
            messages.error(request, f"Нет курса для {to_currency} на {rate_date:%d.%m.%Y}.")
        else:
            rub_value = amount * from_rate.amount
            converted_amount = rub_value / to_rate.amount if to_rate.amount else Decimal("0")
            conversion = {
                "amount": amount,
                "from_currency": from_currency,
                "to_currency": to_currency,
                "rate_date": rate_date,
                "from_rate": from_rate.amount,
                "to_rate": to_rate.amount,
                "converted_amount": converted_amount,
            }

    return render(
        request,
        "core/currency-converter.html",
        {
            "form": form,
            "conversion": conversion,
            "currency_choices": currency_choices,
        },
    )
