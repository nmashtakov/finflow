from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import sqrt

from django.db import transaction as db_transaction
from django.db.models import Max, Min
from django.utils import timezone

from core.currencies import fx_currency_code
from core.models import CurrencyRate, Transaction, TransactionLinkGroup, TransactionLinkItem
from .models import MonthlyBudgetLine, MonthlyBudgetPlan


MONTH_SHORT = [
    'янв', 'фев', 'мар', 'апр', 'май', 'июн',
    'июл', 'авг', 'сен', 'окт', 'ноя', 'дек',
]
MONTH_FULL = [
    'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
    'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь',
]

TECHNICAL_LABELS = {
    'долг',
    'долги',
    'перевод',
    'переводы',
    'перевод между счетами',
    'перевод между людьми',
}
REIMBURSEMENT_LABELS = {'возврат', 'возвраты', 'компенсация', 'компенсации'}

FORECAST_WINDOW = 6
BUDGET_WINDOW_CHOICES = (3, 6, 12)
DEFAULT_BUDGET_DISTRIBUTION_WINDOW = 3
REGULAR_MIN_HISTORY_MONTHS = 12
REGULAR_MIN_ACTIVE_SHARE = Decimal('0.60')
REGULAR_MAX_CV = Decimal('1.50')
DEFAULT_GOAL_MONTHS_LEFT = 6
DEFAULT_INTERPRETATION_DELTA_THRESHOLD = Decimal('15')
DEFAULT_INTERPRETATION_AVG_THRESHOLD = Decimal('15')
DEFAULT_INTERPRETATION_RISK_SHARE_THRESHOLD = Decimal('35')
DEFAULT_ANOMALY_THRESHOLD = Decimal('2.0')
MAX_ANOMALY_THRESHOLD = Decimal('4.0')
ANOMALY_HISTORY_WINDOW = 12
ANOMALY_MIN_HISTORY_MONTHS = 6
MIN_ANOMALY_HISTORY_WINDOW = 3
MAX_ANOMALY_HISTORY_WINDOW = 24
MIN_ANOMALY_MIN_HISTORY_MONTHS = 3


def get_available_months(user, project_id=None):
    qs = Transaction.objects.filter(account__user=user, is_split_parent=False)
    if project_id:
        qs = qs.filter(expense_link__project_id=project_id)
    bounds = qs.aggregate(first=Min('date'), last=Max('date'))
    return _month_options_from_bounds(bounds['first'], bounds['last'])


def get_available_months_from_rows(rows):
    dates = [_parse_row_date(row.get('date')) for row in rows]
    dates = [item for item in dates if item]
    if not dates:
        return []
    return [
        {'value': month.strftime('%Y-%m'), 'label': _format_month_full(month)}
        for month in _month_range(_month_start(min(dates)), _month_start(max(dates)))
    ]


def parse_report_date(value):
    return _parse_row_date(value)


def normalize_budget_window(value):
    try:
        window = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_DISTRIBUTION_WINDOW
    return window if window in BUDGET_WINDOW_CHOICES else DEFAULT_BUDGET_DISTRIBUTION_WINDOW


def normalize_regular_history_months(value):
    try:
        months = int(value)
    except (TypeError, ValueError):
        return REGULAR_MIN_HISTORY_MONTHS
    return min(max(months, 1), 60)


def normalize_regular_active_share(value):
    parsed = _to_decimal(value)
    if parsed is None:
        return REGULAR_MIN_ACTIVE_SHARE
    return min(max(parsed / Decimal('100'), Decimal('0')), Decimal('1'))


def normalize_regular_cv(value):
    parsed = _to_decimal(value)
    if parsed is None:
        return REGULAR_MAX_CV
    return min(max(parsed, Decimal('0')), Decimal('10'))


def normalize_goal_months_left(value):
    try:
        months = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GOAL_MONTHS_LEFT
    return min(max(months, 1), 120)


def normalize_percent_threshold(value, default):
    parsed = _to_decimal(value)
    if parsed is None:
        return default
    return min(max(parsed, Decimal('0')), Decimal('100'))


def normalize_anomaly_threshold(value):
    parsed = _to_decimal(value)
    if parsed is None:
        return DEFAULT_ANOMALY_THRESHOLD
    return min(max(parsed, Decimal('0')), MAX_ANOMALY_THRESHOLD)


def normalize_anomaly_history_window(value):
    try:
        window = int(value)
    except (TypeError, ValueError):
        return ANOMALY_HISTORY_WINDOW
    return min(max(window, MIN_ANOMALY_HISTORY_WINDOW), MAX_ANOMALY_HISTORY_WINDOW)


def normalize_anomaly_min_history_months(value, history_window=None):
    normalized_window = normalize_anomaly_history_window(history_window)
    try:
        min_history = int(value)
    except (TypeError, ValueError):
        return min(ANOMALY_MIN_HISTORY_MONTHS, normalized_window)
    return min(max(min_history, MIN_ANOMALY_MIN_HISTORY_MONTHS), normalized_window)


def build_month_report(
    user,
    selected_month=None,
    report_currency='RUB',
    anomaly_threshold=DEFAULT_ANOMALY_THRESHOLD,
    anomaly_history_window=ANOMALY_HISTORY_WINDOW,
    anomaly_min_history_months=ANOMALY_MIN_HISTORY_MONTHS,
    regular_min_history_months=REGULAR_MIN_HISTORY_MONTHS,
    regular_min_active_share=REGULAR_MIN_ACTIVE_SHARE,
    regular_max_cv=REGULAR_MAX_CV,
    interpretation_delta_threshold=DEFAULT_INTERPRETATION_DELTA_THRESHOLD,
    interpretation_avg_threshold=DEFAULT_INTERPRETATION_AVG_THRESHOLD,
    interpretation_risk_share_threshold=DEFAULT_INTERPRETATION_RISK_SHARE_THRESHOLD,
    project_id=None,
    dimension='category',
    budget_distribution_window=DEFAULT_BUDGET_DISTRIBUTION_WINDOW,
):
    selected_month = selected_month or _latest_transaction_month(user, project_id=project_id)
    if not selected_month:
        return {'has_data': False}

    selected_month = _month_start(selected_month)
    rows = _build_system_rows(user, selected_month, report_currency, project_id=project_id)
    return _build_report_from_rows(
        rows=rows,
        selected_month=selected_month,
        report_currency=report_currency,
        anomaly_threshold=anomaly_threshold,
        anomaly_history_window=anomaly_history_window,
        anomaly_min_history_months=anomaly_min_history_months,
        regular_min_history_months=regular_min_history_months,
        regular_min_active_share=regular_min_active_share,
        regular_max_cv=regular_max_cv,
        interpretation_delta_threshold=interpretation_delta_threshold,
        interpretation_avg_threshold=interpretation_avg_threshold,
        interpretation_risk_share_threshold=interpretation_risk_share_threshold,
        source_label='Данные системы',
        dimension=dimension,
        budget_owner={'user': user, 'project_id': project_id},
        budget_distribution_window=budget_distribution_window,
    )


def build_file_month_report(
    rows,
    selected_month=None,
    report_currency='RUB',
    anomaly_threshold=DEFAULT_ANOMALY_THRESHOLD,
    anomaly_history_window=ANOMALY_HISTORY_WINDOW,
    anomaly_min_history_months=ANOMALY_MIN_HISTORY_MONTHS,
    regular_min_history_months=REGULAR_MIN_HISTORY_MONTHS,
    regular_min_active_share=REGULAR_MIN_ACTIVE_SHARE,
    regular_max_cv=REGULAR_MAX_CV,
    interpretation_delta_threshold=DEFAULT_INTERPRETATION_DELTA_THRESHOLD,
    interpretation_avg_threshold=DEFAULT_INTERPRETATION_AVG_THRESHOLD,
    interpretation_risk_share_threshold=DEFAULT_INTERPRETATION_RISK_SHARE_THRESHOLD,
    dimension='category',
    budget_distribution_window=DEFAULT_BUDGET_DISTRIBUTION_WINDOW,
):
    dates = [_parse_row_date(row.get('date')) for row in rows]
    dates = [item for item in dates if item]
    if not dates:
        return {'has_data': False}

    normalized_rows = []
    for row in rows:
        row_date = _parse_row_date(row.get('date'))
        amount = _to_decimal(row.get('amount'))
        if not row_date or amount is None:
            continue
        amount = _convert_file_amount(amount, row.get('currency') or 'RUB', report_currency, row_date)
        if amount is None:
            continue
        category = (row.get('category') or 'Без категории').strip() or 'Без категории'
        subcategory = (row.get('subcategory') or '').strip()
        if _is_technical_labels(category, subcategory, ''):
            continue
        if amount > 0 and _is_reimbursement_labels(category, subcategory):
            continue
        normalized_rows.append({
            'id': row.get('id') or '',
            'date': row_date,
            'amount': amount,
            'category': category,
            'subcategory': subcategory,
            'comment': (row.get('comment') or '').strip(),
            'account': (row.get('account') or '').strip(),
        })

    selected_month = selected_month or _month_start(max(dates))
    return _build_report_from_rows(
        rows=normalized_rows,
        selected_month=_month_start(selected_month),
        report_currency=report_currency,
        anomaly_threshold=anomaly_threshold,
        anomaly_history_window=anomaly_history_window,
        anomaly_min_history_months=anomaly_min_history_months,
        regular_min_history_months=regular_min_history_months,
        regular_min_active_share=regular_min_active_share,
        regular_max_cv=regular_max_cv,
        interpretation_delta_threshold=interpretation_delta_threshold,
        interpretation_avg_threshold=interpretation_avg_threshold,
        interpretation_risk_share_threshold=interpretation_risk_share_threshold,
        source_label='Загруженный файл',
        dimension=dimension,
        budget_distribution_window=budget_distribution_window,
    )


def _build_system_rows(user, selected_month, report_currency, project_id=None):
    period_end = _month_end_dt(selected_month)
    tx_list = list(
        Transaction.objects.filter(
            account__user=user,
            is_split_parent=False,
            date__lte=period_end,
        )
        .select_related(
            'account',
            'expense_link__project',
            'expense_link__category',
            'expense_link__subcategory',
        )
        .order_by('date', 'id')
    )
    if not tx_list:
        return []

    rate_lookup = _build_rate_lookup(tx_list, report_currency)
    tx_by_id = {tx.id: tx for tx in tx_list}
    converted_by_tx = {}
    expense_rows_by_id = {}

    for tx in tx_list:
        tx_date = timezone.localtime(tx.date).date()
        amount = _convert_amount(tx.amount, tx.currency, report_currency, tx_date, rate_lookup)
        converted_by_tx[tx.id] = amount
        if amount is None:
            continue
        if project_id and tx.expense_link.project_id != project_id:
            continue
        category = _tx_category(tx)
        subcategory = _tx_subcategory(tx)
        if _is_technical_labels(category, subcategory, tx.transaction_type):
            continue
        if amount < 0:
            expense_rows_by_id[tx.id] = {
                'amount_abs': abs(amount),
                'category': category,
                'subcategory': subcategory,
            }

    reimbursement_offset_ids = set()
    if converted_by_tx:
        groups = (
            TransactionLinkGroup.objects.filter(
                user=user,
                status=TransactionLinkGroup.Status.ACTIVE,
                link_type=TransactionLinkGroup.LinkType.REIMBURSEMENT,
                items__transaction_id__in=list(converted_by_tx.keys()),
            )
            .distinct()
            .prefetch_related('items__transaction')
        )
        for group in groups:
            primary_ids = []
            offset_sum = Decimal('0')
            for item in group.items.all():
                converted_amount = converted_by_tx.get(item.transaction_id)
                if item.role == TransactionLinkItem.Role.PRIMARY and item.transaction_id in expense_rows_by_id and converted_amount is not None and converted_amount < 0:
                    primary_ids.append(item.transaction_id)
                elif item.role == TransactionLinkItem.Role.OFFSET and converted_amount is not None and converted_amount > 0:
                    offset_sum += converted_amount
                    reimbursement_offset_ids.add(item.transaction_id)
            remaining = offset_sum
            for primary_id in sorted(primary_ids, key=lambda tx_id: (tx_by_id[tx_id].date, tx_id)):
                if remaining <= 0:
                    break
                current_abs = expense_rows_by_id[primary_id]['amount_abs']
                reduction = min(current_abs, remaining)
                expense_rows_by_id[primary_id]['amount_abs'] = current_abs - reduction
                remaining -= reduction

    rows = []
    for tx in tx_list:
        amount = converted_by_tx.get(tx.id)
        if amount is None:
            continue
        if project_id and tx.expense_link.project_id != project_id:
            continue
        tx_date = timezone.localtime(tx.date).date()
        category = _tx_category(tx)
        subcategory = _tx_subcategory(tx)

        if tx.id in expense_rows_by_id:
            amount_abs = expense_rows_by_id[tx.id]['amount_abs']
            if amount_abs > 0:
                rows.append(_make_report_row(tx, tx_date, -amount_abs, category, subcategory))
        elif amount > 0:
            if tx.id in reimbursement_offset_ids:
                continue
            if _is_technical_labels(category, subcategory, tx.transaction_type):
                continue
            if _is_reimbursement_labels(category, subcategory):
                continue
            rows.append(_make_report_row(tx, tx_date, amount, category, subcategory))
    return rows


def _build_report_from_rows(
    rows,
    selected_month,
    report_currency,
    anomaly_threshold,
    anomaly_history_window,
    anomaly_min_history_months,
    regular_min_history_months,
    regular_min_active_share,
    regular_max_cv,
    interpretation_delta_threshold,
    interpretation_avg_threshold,
    interpretation_risk_share_threshold,
    source_label,
    dimension,
    budget_owner=None,
    budget_distribution_window=DEFAULT_BUDGET_DISTRIBUTION_WINDOW,
):
    dimension = dimension if dimension in {'category', 'subcategory'} else 'category'
    budget_distribution_window = normalize_budget_window(budget_distribution_window)
    anomaly_threshold = normalize_anomaly_threshold(anomaly_threshold)
    anomaly_history_window = normalize_anomaly_history_window(anomaly_history_window)
    anomaly_min_history_months = normalize_anomaly_min_history_months(
        anomaly_min_history_months,
        anomaly_history_window,
    )
    regular_min_history_months = normalize_regular_history_months(regular_min_history_months)
    if isinstance(regular_min_active_share, Decimal) and regular_min_active_share <= 1:
        regular_min_active_share = regular_min_active_share * Decimal('100')
    regular_min_active_share = normalize_regular_active_share(regular_min_active_share)
    regular_max_cv = normalize_regular_cv(regular_max_cv)
    interpretation_delta_threshold = normalize_percent_threshold(
        interpretation_delta_threshold,
        DEFAULT_INTERPRETATION_DELTA_THRESHOLD,
    )
    interpretation_avg_threshold = normalize_percent_threshold(
        interpretation_avg_threshold,
        DEFAULT_INTERPRETATION_AVG_THRESHOLD,
    )
    interpretation_risk_share_threshold = normalize_percent_threshold(
        interpretation_risk_share_threshold,
        DEFAULT_INTERPRETATION_RISK_SHARE_THRESHOLD,
    )
    rows = [row for row in rows if row.get('date') and row.get('amount') is not None]
    if not rows:
        return {'has_data': False}

    selected_month = _month_start(selected_month)
    first_month = _month_start(min(row['date'] for row in rows))
    months = _month_range(first_month, selected_month)
    month_data = {
        month: {
            'income': Decimal('0'),
            'expense': Decimal('0'),
            'categories': defaultdict(lambda: Decimal('0')),
            'transactions': defaultdict(list),
        }
        for month in months
    }
    category_month_data = {
        month: {
            'income': Decimal('0'),
            'expense': Decimal('0'),
            'categories': defaultdict(lambda: Decimal('0')),
            'transactions': defaultdict(list),
        }
        for month in months
    }

    for row in rows:
        month = _month_start(row['date'])
        if month not in month_data:
            continue
        amount = Decimal(row['amount'])
        if amount < 0:
            expense_value = abs(amount)
            group_name = _row_group_label(row, dimension)
            category_name = row.get('category') or 'Без категории'
            month_data[month]['expense'] += expense_value
            month_data[month]['categories'][group_name] += expense_value
            month_data[month]['transactions'][group_name].append({
                'date': row['date'].strftime('%d.%m.%Y'),
                'amount': _money(expense_value),
                'amount_raw': expense_value,
                'category': row.get('category') or 'Без категории',
                'subcategory': row.get('subcategory') or '—',
                'comment': row.get('comment') or '—',
                'account': row.get('account') or '—',
            })
            category_month_data[month]['expense'] += expense_value
            category_month_data[month]['categories'][category_name] += expense_value
            category_month_data[month]['transactions'][category_name].append({
                'date': row['date'].strftime('%d.%m.%Y'),
                'amount': _money(expense_value),
                'amount_raw': expense_value,
                'category': category_name,
                'subcategory': row.get('subcategory') or '—',
                'comment': row.get('comment') or '—',
                'account': row.get('account') or '—',
            })
        elif amount > 0:
            month_data[month]['income'] += amount
            category_month_data[month]['income'] += amount

    expense_series = [month_data[month]['expense'] for month in months]
    income_series = [month_data[month]['income'] for month in months]
    forecast_by_month = _moving_average_forecast(months, expense_series, FORECAST_WINDOW)
    selected_index = months.index(selected_month)
    next_month = _add_months(selected_month, 1)
    next_forecast = _mean_decimal(expense_series[max(0, selected_index + 1 - FORECAST_WINDOW):selected_index + 1])

    categories = sorted({
        category
        for month in months
        for category in month_data[month]['categories']
    })
    profiles = _build_category_profiles(
        categories,
        months,
        month_data,
        selected_month,
        regular_min_history_months,
        regular_min_active_share,
        regular_max_cv,
    )
    regular_categories = {profile['category'] for profile in profiles if profile['expense_type'] == 'regular'}
    regular_series, risk_series = _split_regular_risk_series(months, month_data, regular_categories)
    regular_forecast_by_month = _moving_average_forecast(months, regular_series, FORECAST_WINDOW)
    risk_forecast_by_month = _moving_average_forecast(months, risk_series, FORECAST_WINDOW)
    regular_next = _mean_decimal(regular_series[max(0, selected_index + 1 - FORECAST_WINDOW):selected_index + 1])
    risk_next = max(next_forecast - regular_next, Decimal('0'))

    selected_expense = expense_series[selected_index]
    selected_income = income_series[selected_index]
    previous_expense = expense_series[selected_index - 1] if selected_index > 0 else Decimal('0')
    previous_delta = selected_expense - previous_expense
    previous_delta_pct = _percent(previous_delta, previous_expense)
    previous_month_avg = _mean_decimal(expense_series[max(0, selected_index - FORECAST_WINDOW):selected_index])
    avg_delta = selected_expense - previous_month_avg if previous_month_avg else Decimal('0')
    avg_delta_pct = _percent(avg_delta, previous_month_avg)
    risk_share = _percent(risk_series[selected_index], selected_expense)

    anomalies = _detect_current_anomalies(
        categories,
        months,
        month_data,
        selected_month,
        anomaly_threshold,
        anomaly_history_window,
        anomaly_min_history_months,
    )
    anomaly_candidates = _serialize_anomaly_candidates(
        _detect_current_anomalies(
            categories,
            months,
            month_data,
            selected_month,
            Decimal('0'),
            anomaly_history_window,
            anomaly_min_history_months,
        )
    )
    anomaly_grid = _build_anomaly_grid(
        categories,
        months,
        month_data,
        anomaly_threshold,
        anomaly_history_window,
        anomaly_min_history_months,
    )
    anomaly_transactions = _build_anomaly_transactions(anomaly_grid['raw_months'], categories, month_data)
    metrics = _forecast_metrics(months, expense_series, forecast_by_month)

    chart_start = max(0, len(months) - 18)
    chart_months = months[chart_start:]
    error_points = _forecast_error_points(chart_months, month_data, forecast_by_month)
    top_categories = _top_current_categories(month_data, months, selected_index, selected_month, selected_expense)
    budget = None
    if budget_owner:
        budget_categories = sorted({
            category
            for month in months
            for category in category_month_data[month]['categories']
        })
        budget_profiles = _build_category_profiles(
            budget_categories,
            months,
            category_month_data,
            selected_month,
            regular_min_history_months,
            regular_min_active_share,
            regular_max_cv,
        )
        budget = _build_budget_section(
            user=budget_owner['user'],
            project_id=budget_owner.get('project_id') or None,
            selected_month=selected_month,
            next_month=next_month,
            report_currency=report_currency,
            months=months,
            selected_index=selected_index,
            income_series=income_series,
            total_forecast_next=next_forecast,
            category_month_data=category_month_data,
            category_profiles=budget_profiles,
            distribution_window=budget_distribution_window,
        )
    insights = _build_insights(
        selected_expense=selected_expense,
        previous_delta=previous_delta,
        previous_delta_pct=previous_delta_pct,
        avg_delta=avg_delta,
        avg_delta_pct=avg_delta_pct,
        next_forecast=next_forecast,
        regular_next=regular_next,
        risk_next=risk_next,
        risk_share=risk_share,
        anomalies=anomalies,
        metrics=metrics,
        interpretation_delta_threshold=interpretation_delta_threshold,
        interpretation_avg_threshold=interpretation_avg_threshold,
        interpretation_risk_share_threshold=interpretation_risk_share_threshold,
    )
    chart_months_with_forecast = chart_months + [next_month]

    return {
        'has_data': True,
        'source_label': source_label,
        'dimension': dimension,
        'dimension_label': 'подкатегориям' if dimension == 'subcategory' else 'категориям',
        'selected_month': selected_month,
        'selected_month_value': selected_month.strftime('%Y-%m'),
        'selected_month_label': _format_month_full(selected_month),
        'period_label': f'{_format_month_full(first_month)} - {_format_month_full(selected_month)}',
        'next_month_label': _format_month_full(next_month),
        'report_currency': report_currency,
        'summary': {
            'income': _money(selected_income),
            'expense': _money(selected_expense),
            'balance': _money(selected_income - selected_expense),
            'forecast_next': _money(next_forecast),
            'regular_next': _money(regular_next),
            'risk_next': _money(risk_next),
            'previous_delta': _signed_money(previous_delta),
            'previous_delta_pct': _signed_percent(previous_delta_pct),
            'avg_delta': _signed_money(avg_delta),
            'avg_delta_pct': _signed_percent(avg_delta_pct),
            'risk_share': _plain_percent(risk_share),
            'wape': _plain_percent(metrics['wape']),
        },
        'metrics': metrics,
        'top_categories': top_categories,
        'profiles': profiles,
        'anomalies': anomalies[:10],
        'anomaly_candidates': anomaly_candidates,
        'insights': insights,
        'budget': budget,
        'chart_payload': {
            'labels': [_format_month_short(month) for month in chart_months_with_forecast],
            'expense': [_chart_value(month_data[month]['expense']) for month in chart_months] + [None],
            'forecast': [
                _chart_value(forecast_by_month.get(month)) if forecast_by_month.get(month) is not None else None
                for month in chart_months
            ] + [_chart_value(next_forecast)],
            'regular': [_chart_value(value) for value in regular_series[chart_start:]] + [_chart_value(regular_next)],
            'risk': [_chart_value(value) for value in risk_series[chart_start:]] + [_chart_value(risk_next)],
            'regular_forecast': [
                _chart_value(regular_forecast_by_month.get(month)) if regular_forecast_by_month.get(month) is not None else None
                for month in chart_months
            ] + [_chart_value(regular_next)],
            'risk_forecast': [
                _chart_value(risk_forecast_by_month.get(month)) if risk_forecast_by_month.get(month) is not None else None
                for month in chart_months
            ] + [_chart_value(risk_next)],
            'forecast_index': len(chart_months),
            'top_category_labels': [item['category'] for item in top_categories],
            'top_category_values': [_chart_value(item['amount_raw']) for item in top_categories],
            'top_category_avg_values': [_chart_value(item['avg_raw']) for item in top_categories],
            'top_category_previous_values': [_chart_value(item['previous_raw']) for item in top_categories],
            'top_category_avg_delta_pct_labels': [item['avg_delta_pct'] for item in top_categories],
            'top_category_avg_delta_labels': [item['avg_delta'] for item in top_categories],
            'error_labels': [_format_month_short(item['month']) for item in error_points],
            'error_abs': [_chart_value(item['abs_error']) for item in error_points],
            'error_ape': [_chart_value(item['ape']) for item in error_points],
        },
        'anomaly_grid': anomaly_grid,
        'anomaly_transactions': anomaly_transactions,
        'settings': {
            'forecast_window': FORECAST_WINDOW,
            'regular_min_history_months': regular_min_history_months,
            'regular_min_active_share': _plain_percent(regular_min_active_share * 100),
            'regular_min_active_share_pct': _format_number(regular_min_active_share * 100),
            'regular_max_cv': _format_number(regular_max_cv),
            'anomaly_threshold': _format_number(anomaly_threshold),
            'anomaly_history_window': anomaly_history_window,
            'anomaly_min_history_months': anomaly_min_history_months,
            'interpretation_delta_threshold': _format_number(interpretation_delta_threshold),
            'interpretation_delta_threshold_display': _plain_percent(interpretation_delta_threshold),
            'interpretation_avg_threshold': _format_number(interpretation_avg_threshold),
            'interpretation_avg_threshold_display': _plain_percent(interpretation_avg_threshold),
            'interpretation_risk_share_threshold': _format_number(interpretation_risk_share_threshold),
            'interpretation_risk_share_threshold_display': _plain_percent(interpretation_risk_share_threshold),
            'budget_distribution_window': budget_distribution_window,
            'budget_window_choices': list(BUDGET_WINDOW_CHOICES),
            'history_months': len(months),
        },
    }


def _make_report_row(tx, tx_date, amount, category, subcategory):
    return {
        'id': tx.id,
        'date': tx_date,
        'amount': amount,
        'category': category or 'Без категории',
        'subcategory': subcategory or '',
        'comment': (tx.comment or '').strip(),
        'account': tx.account.name if tx.account_id else '',
    }


def _row_group_label(row, dimension):
    category = row.get('category') or 'Без категории'
    subcategory = (row.get('subcategory') or '').strip()
    if dimension == 'subcategory' and subcategory:
        return f'{category} / {subcategory}'
    return category


def _latest_transaction_month(user, project_id=None):
    qs = Transaction.objects.filter(account__user=user, is_split_parent=False)
    if project_id:
        qs = qs.filter(expense_link__project_id=project_id)
    latest = qs.aggregate(last=Max('date'))['last']
    if not latest:
        return None
    return _month_start(timezone.localtime(latest).date())


def _build_rate_lookup(tx_list, report_currency):
    currencies = {((tx.currency or 'RUB').upper()) for tx in tx_list}
    currencies.add('RUB')
    currencies.add((report_currency or 'RUB').upper())
    dates = [timezone.localtime(tx.date).date() for tx in tx_list]
    min_date = min(dates)
    max_date = max(dates)
    lookup = {'RUB': [(min_date, Decimal('1'))]}
    for currency in currencies:
        normalized_currency = fx_currency_code(currency) or currency
        if normalized_currency == 'RUB' or normalized_currency in lookup:
            continue
        rows = list(
            CurrencyRate.objects.filter(currency=normalized_currency, date__lte=max_date)
            .order_by('date')
            .values_list('date', 'amount')
        )
        if rows:
            lookup[normalized_currency] = rows
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
    return rows[max(index, 0)][1]


def _convert_amount(amount, source_currency, target_currency, target_date, rate_lookup):
    source = (source_currency or 'RUB').upper()
    target = (target_currency or 'RUB').upper()
    if source == target or fx_currency_code(source) == fx_currency_code(target):
        return Decimal(amount or 0)
    source_rate = _rate_on_date(rate_lookup, source, target_date)
    target_rate = _rate_on_date(rate_lookup, target, target_date)
    if source_rate is None or target_rate is None or target_rate == 0:
        return None
    return (Decimal(amount or 0) * source_rate) / target_rate


def _convert_file_amount(amount, source_currency, target_currency, target_date):
    source = (source_currency or 'RUB').upper()
    target = (target_currency or 'RUB').upper()
    if source == target or fx_currency_code(source) == fx_currency_code(target):
        return amount
    source_rate = _single_rate_on_date(source, target_date)
    target_rate = _single_rate_on_date(target, target_date)
    if source_rate is None or target_rate is None or target_rate == 0:
        return None
    return (amount * source_rate) / target_rate


def _single_rate_on_date(currency, target_date):
    currency = fx_currency_code(currency) or 'RUB'
    if currency == 'RUB':
        return Decimal('1')
    row = CurrencyRate.objects.filter(currency=currency, date__lte=target_date).order_by('-date').first()
    return row.amount if row else None


def _tx_category(tx):
    if tx.expense_link_id and tx.expense_link.category_id:
        return tx.expense_link.category.name
    return 'Без категории'


def _tx_subcategory(tx):
    if tx.expense_link_id and tx.expense_link.subcategory_id:
        return tx.expense_link.subcategory.name
    return ''


def _is_technical_labels(category, subcategory, transaction_type=''):
    if transaction_type == 'transfer':
        return True
    normalized = {_normalize_label(category), _normalize_label(subcategory)}
    return bool(normalized & TECHNICAL_LABELS)


def _is_reimbursement_labels(category, subcategory):
    normalized = {_normalize_label(category), _normalize_label(subcategory)}
    return bool(normalized & REIMBURSEMENT_LABELS)


def _normalize_label(value):
    return ' '.join((value or '').strip().lower().split())


def _moving_average_forecast(months, values, window):
    result = {}
    for index, month in enumerate(months):
        result[month] = None if index < window else _mean_decimal(values[index - window:index])
    return result


def _build_category_profiles(
    categories,
    months,
    month_data,
    selected_month,
    regular_min_history_months,
    regular_min_active_share,
    regular_max_cv,
):
    total_expense = sum((month_data[month]['expense'] for month in months), Decimal('0'))
    selected_categories = month_data[selected_month]['categories']
    total_months = len(months)
    result = []

    for category in categories:
        values = [month_data[month]['categories'].get(category, Decimal('0')) for month in months]
        total = sum(values, Decimal('0'))
        active_months = sum(1 for value in values if value > 0)
        mean_value = _mean_decimal(values)
        std_value = _std_decimal(values, mean_value)
        cv = (std_value / mean_value) if mean_value else Decimal('0')
        active_share = Decimal(active_months) / Decimal(total_months or 1)
        total_share = (total / total_expense * 100) if total_expense else Decimal('0')
        expense_type = 'regular' if (
            total_months >= regular_min_history_months
            and active_share >= regular_min_active_share
            and cv <= regular_max_cv
        ) else 'risk'

        result.append({
            'category': category,
            'current': _money(selected_categories.get(category, Decimal('0'))),
            'current_raw': selected_categories.get(category, Decimal('0')),
            'total': _money(total),
            'active_months': active_months,
            'active_share': _plain_percent(active_share * 100),
            'coefficient_variation': _format_number(cv),
            'total_share': _plain_percent(total_share),
            'expense_type': expense_type,
            'expense_type_label': 'Регулярная' if expense_type == 'regular' else 'Рисковая',
        })

    return sorted(result, key=lambda item: (item['current_raw'], item['total']), reverse=True)


def _split_regular_risk_series(months, month_data, regular_categories):
    regular = []
    risk = []
    for month in months:
        regular_total = sum(
            amount
            for category, amount in month_data[month]['categories'].items()
            if category in regular_categories
        )
        regular.append(regular_total)
        risk.append(max(month_data[month]['expense'] - regular_total, Decimal('0')))
    return regular, risk


def _detect_current_anomalies(
    categories,
    months,
    month_data,
    selected_month,
    threshold,
    anomaly_history_window,
    anomaly_min_history_months,
):
    selected_index = months.index(selected_month)
    result = []

    for category in categories:
        stats = _rolling_category_stats(
            category,
            selected_index,
            months,
            month_data,
            anomaly_history_window,
            anomaly_min_history_months,
        )
        if not stats:
            continue
        mean_value, std_value = stats
        current_value = month_data[selected_month]['categories'].get(category, Decimal('0'))
        if std_value <= 0:
            continue
        z_score = (current_value - mean_value) / std_value
        if abs(z_score) < threshold:
            continue
        result.append({
            'category': category,
            'key': _cell_key(selected_month, category),
            'amount': _money(current_value),
            'expected': _money(mean_value),
            'amount_raw': current_value,
            'expected_raw': mean_value,
            'z_score': _format_number(z_score),
            'z_score_value': _format_js_number(z_score),
            'z_score_raw': z_score,
            'type': 'growth' if z_score > 0 else 'decrease',
            'type_label': 'Рост' if z_score > 0 else 'Снижение',
        })
    return sorted(result, key=lambda item: abs(item['z_score_raw']), reverse=True)


def _build_anomaly_grid(
    categories,
    months,
    month_data,
    threshold,
    anomaly_history_window,
    anomaly_min_history_months,
):
    display_months = months[-12:]
    display_month_indexes = {month: months.index(month) for month in display_months}
    rows = []
    for category in categories:
        values = [month_data[month]['categories'].get(category, Decimal('0')) for month in months]
        total = sum(values, Decimal('0'))
        if total <= 0:
            continue
        cells = []
        max_abs_z = Decimal('0')
        for month in display_months:
            value = month_data[month]['categories'].get(category, Decimal('0'))
            mean_value = Decimal('0')
            stats = _rolling_category_stats(
                category,
                display_month_indexes[month],
                months,
                month_data,
                anomaly_history_window,
                anomaly_min_history_months,
            )
            if stats:
                mean_value, std_value = stats
                z_score = Decimal('0') if std_value <= 0 else (value - mean_value) / std_value
            else:
                z_score = Decimal('0')
            max_abs_z = max(max_abs_z, abs(z_score))
            cell_type = 'normal'
            if z_score >= threshold:
                cell_type = 'growth'
            elif z_score <= -threshold:
                cell_type = 'decrease'
            cells.append({
                'type': cell_type,
                'value': _money(value),
                'short_value': _compact_money(value),
                'expected': _money(mean_value),
                'z_score': _format_number(z_score),
                'z_score_value': _format_js_number(z_score),
                'title': f'{category}, {_format_month_full(month)}',
                'key': _cell_key(month, category),
                'transaction_count': len(month_data[month]['transactions'].get(category, [])),
            })
        rows.append({
            'category': category,
            'cells': cells,
            'max_abs_z': max_abs_z,
            'total': total,
        })

    return {
        'months': [_format_month_short(month) for month in display_months],
        'raw_months': display_months,
        'rows': sorted(rows, key=lambda item: (item['max_abs_z'], item['total']), reverse=True),
    }


def _rolling_category_stats(category, month_index, months, month_data, anomaly_history_window, anomaly_min_history_months):
    history_start = max(0, month_index - anomaly_history_window)
    history_months = months[history_start:month_index]
    if len(history_months) < anomaly_min_history_months:
        return None
    values = [month_data[month]['categories'].get(category, Decimal('0')) for month in history_months]
    mean_value = _mean_decimal(values)
    return mean_value, _std_decimal(values, mean_value)


def _serialize_anomaly_candidates(anomalies):
    return [
        {
            'category': item['category'],
            'key': item['key'],
            'amount': item['amount'],
            'expected': item['expected'],
            'z_score': item['z_score'],
            'z_score_value': item['z_score_value'],
            'type': item['type'],
            'type_label': item['type_label'],
        }
        for item in anomalies
    ]


def _build_anomaly_transactions(months, categories, month_data):
    payload = {}
    for month in months:
        for category in categories:
            items = sorted(
                month_data[month]['transactions'].get(category, []),
                key=lambda item: item['amount_raw'],
                reverse=True,
            )
            payload[_cell_key(month, category)] = [
                {
                    'date': item['date'],
                    'amount': f'-{item["amount"]}',
                    'amount_raw': -_chart_value(item['amount_raw']),
                    'category': item['category'],
                    'subcategory': item['subcategory'],
                    'comment': item['comment'],
                    'account': item['account'],
                }
                for item in items
            ]
    return payload


def _forecast_error_points(months, month_data, forecast_by_month):
    result = []
    for month in months:
        forecast = forecast_by_month.get(month)
        actual = month_data[month]['expense']
        if forecast is None or actual <= 0:
            continue
        abs_error = abs(actual - forecast)
        result.append({
            'month': month,
            'abs_error': abs_error,
            'ape': abs_error / actual * 100,
        })
    return result


def _forecast_metrics(months, expense_series, forecast_by_month):
    actual_sum = Decimal('0')
    abs_error_sum = Decimal('0')
    ape_values = []
    for month, actual in zip(months, expense_series):
        forecast = forecast_by_month.get(month)
        if forecast is None or actual <= 0:
            continue
        abs_error = abs(actual - forecast)
        abs_error_sum += abs_error
        actual_sum += actual
        ape_values.append(abs_error / actual * 100)

    wape = (abs_error_sum / actual_sum * 100) if actual_sum else Decimal('0')
    return {
        'mae': _money(abs_error_sum / Decimal(len(ape_values))) if ape_values else '0',
        'mape': _plain_percent(_mean_decimal(ape_values)) if ape_values else '0%',
        'wape': wape,
        'wape_display': _plain_percent(wape),
        'points': len(ape_values),
    }


def _top_current_categories(month_data, months, selected_index, selected_month, selected_expense):
    category_values = month_data[selected_month]['categories']
    previous_month = months[selected_index - 1] if selected_index > 0 else None
    history_start = max(0, selected_index - 12)
    history_months = months[history_start:selected_index]
    if not history_months:
        history_months = [selected_month]

    rows = []
    for category, amount in category_values.items():
        previous_value = month_data[previous_month]['categories'].get(category, Decimal('0')) if previous_month else Decimal('0')
        avg_value = _mean_decimal([
            month_data[month]['categories'].get(category, Decimal('0'))
            for month in history_months
        ])
        previous_delta = amount - previous_value
        avg_delta = amount - avg_value
        rows.append({
            'category': category,
            'amount': _money(amount),
            'amount_raw': amount,
            'previous': _money(previous_value),
            'previous_raw': previous_value,
            'previous_delta': _signed_money(previous_delta),
            'previous_delta_pct': _signed_percent(_percent(previous_delta, previous_value)),
            'avg': _money(avg_value),
            'avg_raw': avg_value,
            'avg_delta': _signed_money(avg_delta),
            'avg_delta_pct': _signed_percent(_percent(avg_delta, avg_value)),
            'share': _plain_percent(_percent(amount, selected_expense)),
        })
    return sorted(rows, key=lambda item: item['amount_raw'], reverse=True)


def _build_insights(
    selected_expense,
    previous_delta,
    previous_delta_pct,
    avg_delta,
    avg_delta_pct,
    next_forecast,
    regular_next,
    risk_next,
    risk_share,
    anomalies,
    metrics,
    interpretation_delta_threshold,
    interpretation_avg_threshold,
    interpretation_risk_share_threshold,
):
    insights = []
    if previous_delta_pct >= interpretation_delta_threshold:
        insights.append({
            'tone': 'bad',
            'text': f'Расходы выросли относительно предыдущего месяца на {_signed_percent(previous_delta_pct)} ({_signed_money(previous_delta)}).',
        })
    elif previous_delta_pct <= -interpretation_delta_threshold:
        insights.append({
            'tone': 'good',
            'text': f'Расходы снизились относительно предыдущего месяца на {_plain_percent(abs(previous_delta_pct))} ({_signed_money(previous_delta)}).',
        })
    else:
        insights.append({
            'tone': 'neutral',
            'text': f'Расходы близки к прошлому месяцу: изменение {_signed_percent(previous_delta_pct)} ({_signed_money(previous_delta)}).',
        })

    if avg_delta_pct >= interpretation_avg_threshold:
        insights.append({
            'tone': 'bad',
            'text': f'Месяц выше среднего уровня последних {FORECAST_WINDOW} месяцев на {_signed_percent(avg_delta_pct)} ({_signed_money(avg_delta)}).',
        })
    elif avg_delta_pct <= -interpretation_avg_threshold:
        insights.append({
            'tone': 'good',
            'text': f'Месяц ниже среднего уровня последних {FORECAST_WINDOW} месяцев на {_plain_percent(abs(avg_delta_pct))} ({_signed_money(avg_delta)}).',
        })

    if risk_share >= interpretation_risk_share_threshold:
        insights.append({
            'tone': 'bad',
            'text': f'Рисковые расходы заняли {_plain_percent(risk_share)} выбранного месяца.',
        })
    else:
        insights.append({
            'tone': 'good',
            'text': f'Рисковая часть удержалась на уровне {_plain_percent(risk_share)} расходов месяца.',
        })

    if anomalies:
        top = anomalies[0]
        if top['type'] == 'growth':
            insights.append({
                'tone': 'bad',
                'text': f'Главное отклонение: рост в «{top["category"]}», z-score {top["z_score"]}, факт {top["amount"]}.',
            })
        else:
            insights.append({
                'tone': 'good',
                'text': f'Главное отклонение: снижение в «{top["category"]}», z-score {top["z_score"]}, факт {top["amount"]}.',
            })

    if next_forecast > 0:
        forecast_delta = next_forecast - selected_expense
        insights.append({
            'tone': 'bad' if forecast_delta > 0 else 'good',
            'text': f'Прогноз следующего месяца: {_money(next_forecast)}, изменение к выбранному месяцу {_signed_percent(_percent(forecast_delta, selected_expense))} ({_signed_money(forecast_delta)}).',
        })
        insights.append({
            'tone': 'neutral',
            'text': f'В прогнозе: регулярная часть {_money(regular_next)}, рисковая часть {_money(risk_next)}, историческая WAPE {_plain_percent(metrics["wape"])}.',
        })

    return insights[:7]


def _build_budget_section(
    user,
    project_id,
    selected_month,
    next_month,
    report_currency,
    months,
    selected_index,
    income_series,
    total_forecast_next,
    category_month_data,
    category_profiles,
    distribution_window,
):
    distribution_window = normalize_budget_window(distribution_window)
    auto_income_forecast_next = _mean_decimal(income_series[max(0, selected_index + 1 - FORECAST_WINDOW):selected_index + 1])
    savings_goal_defaults = _build_savings_goal_defaults()
    regular_categories = {
        profile['category']
        for profile in category_profiles
        if profile['expense_type'] == 'regular'
    }
    regular_series, risk_series = _split_regular_risk_series(months, category_month_data, regular_categories)
    regular_forecast_next = _mean_decimal(regular_series[max(0, selected_index + 1 - FORECAST_WINDOW):selected_index + 1])
    risk_forecast_next = max(total_forecast_next - regular_forecast_next, Decimal('0'))
    saved_plan = _get_saved_budget_plan(user, project_id, next_month, report_currency)
    current_month_plan = _get_saved_budget_plan(user, project_id, selected_month, report_currency)
    saved_lines = {
        line.category_name: line
        for line in (saved_plan.lines.all() if saved_plan else [])
    }
    current_month_lines = {
        line.category_name: line
        for line in (current_month_plan.lines.all() if current_month_plan else [])
    }

    history_months = months[max(0, selected_index + 1 - distribution_window):selected_index + 1]
    if not history_months:
        history_months = [selected_month]

    bucket_avg_totals = {
        MonthlyBudgetLine.ExpenseType.REGULAR: Decimal('0'),
        MonthlyBudgetLine.ExpenseType.RISK: Decimal('0'),
    }
    line_drafts = []

    for sort_order, profile in enumerate(category_profiles):
        category = profile['category']
        current_value = category_month_data[selected_month]['categories'].get(category, Decimal('0'))
        avg_value = _mean_decimal([
            category_month_data[month]['categories'].get(category, Decimal('0'))
            for month in history_months
        ])
        expense_type = profile['expense_type']
        bucket_avg_totals[expense_type] += avg_value
        line_drafts.append({
            'category': category,
            'expense_type': expense_type,
            'expense_type_label': profile['expense_type_label'],
            'current_raw': current_value,
            'avg_raw': avg_value,
            'sort_order': sort_order,
        })

    bucket_forecasts = {
        MonthlyBudgetLine.ExpenseType.REGULAR: regular_forecast_next,
        MonthlyBudgetLine.ExpenseType.RISK: risk_forecast_next,
    }

    lines = []
    suggested_total = Decimal('0')
    planned_total = Decimal('0')
    seen_categories = set()
    display_income_forecast = saved_plan.forecast_income if saved_plan else auto_income_forecast_next
    goal_target_amount = saved_plan.goal_target_amount if saved_plan else savings_goal_defaults['target_amount']
    goal_current_amount = saved_plan.goal_current_amount if saved_plan else savings_goal_defaults['current_amount']
    goal_months_left = saved_plan.goal_months_left if saved_plan else savings_goal_defaults['months_left']

    for draft in line_drafts:
        category = draft['category']
        expense_type = draft['expense_type']
        bucket_total = bucket_avg_totals[expense_type]
        share_pct = _percent(draft['avg_raw'], bucket_total) if bucket_total > 0 else Decimal('0')
        suggested_amount = (
            bucket_forecasts[expense_type] * draft['avg_raw'] / bucket_total
            if bucket_total > 0 else Decimal('0')
        )
        saved_line = saved_lines.get(category)
        planned_amount = saved_line.planned_amount if saved_line else suggested_amount
        suggested_total += suggested_amount
        planned_total += planned_amount
        seen_categories.add(category)
        current_plan_line = current_month_lines.get(category)
        current_plan_amount = current_plan_line.planned_amount if current_plan_line else None
        lines.append(_serialize_budget_line(
            category=category,
            expense_type=expense_type,
            expense_type_label=draft['expense_type_label'],
            current_raw=draft['current_raw'],
            avg_raw=draft['avg_raw'],
            share_pct=share_pct,
            suggested_amount=suggested_amount,
            planned_amount=planned_amount,
            sort_order=draft['sort_order'],
            current_plan_amount=current_plan_amount,
        ))

    for extra_order, saved_line in enumerate(saved_lines.values(), start=len(lines)):
        if saved_line.category_name in seen_categories:
            continue
        planned_total += saved_line.planned_amount
        current_plan_line = current_month_lines.get(saved_line.category_name)
        current_plan_amount = current_plan_line.planned_amount if current_plan_line else None
        lines.append(_serialize_budget_line(
            category=saved_line.category_name,
            expense_type=saved_line.expense_type,
            expense_type_label=saved_line.get_expense_type_display(),
            current_raw=Decimal('0'),
            avg_raw=saved_line.avg_amount,
            share_pct=saved_line.share_pct,
            suggested_amount=saved_line.suggested_amount,
            planned_amount=saved_line.planned_amount,
            sort_order=extra_order,
            current_plan_amount=current_plan_amount,
        ))

    lines = sorted(
        lines,
        key=lambda item: (
            0 if item['expense_type'] == MonthlyBudgetLine.ExpenseType.REGULAR else 1,
            -item['suggested_raw'],
            item['category'].lower(),
        ),
    )
    planned_balance = display_income_forecast - planned_total
    goal_gap_amount = max(goal_target_amount - goal_current_amount, Decimal('0'))
    goal_required_monthly = (
        goal_gap_amount / Decimal(goal_months_left)
        if goal_gap_amount > 0 and goal_months_left > 0 else Decimal('0')
    )
    recommendation_total, recommendation_bucket_totals = _build_goal_recommendation_totals(
        display_income_forecast=display_income_forecast,
        total_forecast_next=total_forecast_next,
        regular_forecast_next=regular_forecast_next,
        risk_forecast_next=risk_forecast_next,
        goal_required_monthly=goal_required_monthly,
    )
    recommendation_by_bucket = {
        MonthlyBudgetLine.ExpenseType.REGULAR: recommendation_bucket_totals['regular'],
        MonthlyBudgetLine.ExpenseType.RISK: recommendation_bucket_totals['risk'],
    }
    planned_balance_after_goal = planned_balance - goal_required_monthly
    recommendation_lines = []
    for line in lines:
        bucket_total = bucket_forecasts[line['expense_type']]
        recommended_amount = (
            recommendation_by_bucket[line['expense_type']] * line['suggested_raw'] / bucket_total
            if bucket_total > 0 else Decimal('0')
        )
        line['recommended'] = _money(recommended_amount)
        line['recommended_raw'] = recommended_amount
        line['recommended_input'] = _input_decimal(recommended_amount)
        recommendation_lines.append(line)
    return {
        'allow_save': True,
        'base_month': selected_month,
        'currency': report_currency,
        'distribution_window': distribution_window,
        'window_choices': list(BUDGET_WINDOW_CHOICES),
        'plan_month': next_month,
        'plan_month_value': next_month.strftime('%Y-%m'),
        'plan_month_label': _format_month_full(next_month),
        'has_saved_plan': bool(saved_plan),
        'saved_at': timezone.localtime(saved_plan.updated_at).strftime('%d.%m.%Y %H:%M') if saved_plan else '',
        'current_month_has_plan': bool(current_month_plan),
        'current_month_plan_label': _format_month_full(selected_month),
        'summary': {
            'forecast_income': _money(display_income_forecast),
            'forecast_income_auto': _money(auto_income_forecast_next),
            'forecast_expense': _money(total_forecast_next),
            'forecast_regular': _money(regular_forecast_next),
            'forecast_risk': _money(risk_forecast_next),
            'suggested_total': _money(suggested_total),
            'recommended_total': _money(recommendation_total),
            'planned_total': _money(planned_total),
            'planned_balance': _money(planned_balance),
            'goal_target_amount': _money(goal_target_amount),
            'goal_current_amount': _money(goal_current_amount),
            'goal_gap_amount': _money(goal_gap_amount),
            'goal_required_monthly': _money(goal_required_monthly),
            'goal_months_left': str(goal_months_left),
            'planned_balance_after_goal': _money(planned_balance_after_goal),
        },
        'summary_raw': {
            'forecast_income': display_income_forecast,
            'forecast_income_auto': auto_income_forecast_next,
            'forecast_expense': total_forecast_next,
            'forecast_regular': regular_forecast_next,
            'forecast_risk': risk_forecast_next,
            'suggested_total': suggested_total,
            'recommended_total': recommendation_total,
            'planned_total': planned_total,
            'planned_balance': planned_balance,
            'goal_target_amount': goal_target_amount,
            'goal_current_amount': goal_current_amount,
            'goal_gap_amount': goal_gap_amount,
            'goal_required_monthly': goal_required_monthly,
            'goal_months_left': goal_months_left,
            'planned_balance_after_goal': planned_balance_after_goal,
        },
        'lines': recommendation_lines,
    }


def save_budget_plan(user, project, budget_data, post_data):
    if not budget_data:
        return False

    forecast_income = _to_decimal(post_data.get('budget_forecast_income'))
    if forecast_income is None:
        return False
    forecast_income = max(forecast_income, Decimal('0'))
    goal_target_amount = _to_decimal(post_data.get('budget_goal_target_amount'))
    goal_current_amount = _to_decimal(post_data.get('budget_goal_current_amount'))
    goal_months_left = normalize_goal_months_left(post_data.get('budget_goal_months_left'))
    if goal_target_amount is None or goal_current_amount is None:
        return False
    goal_target_amount = max(goal_target_amount, Decimal('0'))
    goal_current_amount = max(goal_current_amount, Decimal('0'))
    goal_gap_amount = max(goal_target_amount - goal_current_amount, Decimal('0'))
    goal_required_monthly = (
        goal_gap_amount / Decimal(goal_months_left)
        if goal_gap_amount > 0 else Decimal('0')
    )

    categories = post_data.getlist('budget_category')
    expense_types = post_data.getlist('budget_expense_type')
    avg_amounts = post_data.getlist('budget_avg_amount')
    share_pcts = post_data.getlist('budget_share_pct')
    suggested_amounts = post_data.getlist('budget_suggested_amount')
    planned_amounts = post_data.getlist('budget_planned_amount')

    row_count = len(categories)
    if not row_count or not all(
        len(items) == row_count
        for items in (expense_types, avg_amounts, share_pcts, suggested_amounts, planned_amounts)
    ):
        return False

    plan = (
        MonthlyBudgetPlan.objects
        .filter(
            user=user,
            project=project,
            plan_month=budget_data['plan_month'],
            currency=budget_data['currency'],
        )
        .order_by('-updated_at', '-id')
        .first()
    )
    if not plan:
        plan = MonthlyBudgetPlan(user=user, project=project)

    line_payloads = []
    planned_total = Decimal('0')
    for sort_order, category in enumerate(categories):
        planned_amount = _to_decimal(planned_amounts[sort_order])
        avg_amount = _to_decimal(avg_amounts[sort_order])
        share_pct = _to_decimal(share_pcts[sort_order])
        suggested_amount = _to_decimal(suggested_amounts[sort_order])
        if planned_amount is None or avg_amount is None or share_pct is None or suggested_amount is None:
            return False
        planned_amount = max(planned_amount, Decimal('0'))
        planned_total += planned_amount
        line_payloads.append({
            'category_name': (category or 'Без категории').strip() or 'Без категории',
            'expense_type': expense_types[sort_order] if expense_types[sort_order] in dict(MonthlyBudgetLine.ExpenseType.choices) else MonthlyBudgetLine.ExpenseType.RISK,
            'avg_amount': avg_amount,
            'share_pct': share_pct,
            'suggested_amount': suggested_amount,
            'planned_amount': planned_amount,
            'sort_order': sort_order,
        })

    with db_transaction.atomic():
        plan.base_month = budget_data['base_month']
        plan.plan_month = budget_data['plan_month']
        plan.currency = budget_data['currency']
        plan.distribution_window = budget_data['distribution_window']
        plan.forecast_income = _round_decimal(forecast_income, Decimal('0.01'))
        plan.forecast_expense = _round_decimal(budget_data['summary_raw']['forecast_expense'], Decimal('0.01'))
        plan.forecast_regular = _round_decimal(budget_data['summary_raw']['forecast_regular'], Decimal('0.01'))
        plan.forecast_risk = _round_decimal(budget_data['summary_raw']['forecast_risk'], Decimal('0.01'))
        plan.goal_target_amount = _round_decimal(goal_target_amount, Decimal('0.01'))
        plan.goal_current_amount = _round_decimal(goal_current_amount, Decimal('0.01'))
        plan.goal_months_left = goal_months_left
        plan.goal_required_monthly = _round_decimal(goal_required_monthly, Decimal('0.01'))
        plan.planned_total = _round_decimal(planned_total, Decimal('0.01'))
        plan.planned_balance = _round_decimal(plan.forecast_income - planned_total, Decimal('0.01'))
        plan.goal_balance_after_saving = _round_decimal(plan.planned_balance - goal_required_monthly, Decimal('0.01'))
        plan.save()

        plan.lines.all().delete()
        MonthlyBudgetLine.objects.bulk_create([
            MonthlyBudgetLine(plan=plan, **payload)
            for payload in line_payloads
        ])
    return True


def _get_saved_budget_plan(user, project_id, plan_month, currency):
    return (
        MonthlyBudgetPlan.objects
        .filter(
            user=user,
            project_id=project_id,
            plan_month=plan_month,
            currency=currency,
        )
        .prefetch_related('lines')
        .order_by('-updated_at', '-id')
        .first()
    )


def _serialize_budget_line(
    category,
    expense_type,
    expense_type_label,
    current_raw,
    avg_raw,
    share_pct,
    suggested_amount,
    planned_amount,
    sort_order,
    current_plan_amount=None,
):
    current_plan_delta = current_raw - current_plan_amount if current_plan_amount is not None else None
    if current_plan_amount is not None:
        ratio_base = current_plan_amount if current_plan_amount > 0 else (current_raw if current_raw > 0 else Decimal('1'))
        current_plan_delta_ratio = min(_percent(abs(current_plan_delta), ratio_base), Decimal('100'))
        current_plan_delta_tone = 'good' if current_plan_delta <= 0 else 'bad'
    else:
        current_plan_delta_ratio = Decimal('0')
        current_plan_delta_tone = 'neutral'
    return {
        'category': category,
        'expense_type': expense_type,
        'expense_type_label': expense_type_label,
        'current': _money(current_raw),
        'current_raw': current_raw,
        'avg': _money(avg_raw),
        'avg_raw': avg_raw,
        'share': _plain_percent(share_pct),
        'share_raw': share_pct,
        'share_input': _input_decimal(share_pct),
        'suggested': _money(suggested_amount),
        'suggested_raw': suggested_amount,
        'suggested_input': _input_decimal(suggested_amount),
        'planned': _money(planned_amount),
        'planned_raw': planned_amount,
        'planned_input': _input_integer(planned_amount),
        'planned_display': _money(planned_amount),
        'avg_input': _input_decimal(avg_raw),
        'current_plan': _money(current_plan_amount) if current_plan_amount is not None else '—',
        'current_plan_raw': current_plan_amount,
        'current_plan_delta': _signed_money(current_plan_delta) if current_plan_delta is not None else '—',
        'current_plan_delta_raw': current_plan_delta,
        'current_plan_delta_ratio': _format_js_number(current_plan_delta_ratio),
        'current_plan_delta_tone': current_plan_delta_tone,
        'sort_order': sort_order,
    }


def _build_savings_goal_defaults():
    return {
        'target_amount': Decimal('0'),
        'current_amount': Decimal('0'),
        'months_left': DEFAULT_GOAL_MONTHS_LEFT,
    }


def _build_goal_recommendation_totals(
    display_income_forecast,
    total_forecast_next,
    regular_forecast_next,
    risk_forecast_next,
    goal_required_monthly,
):
    available_after_goal = max(display_income_forecast - goal_required_monthly, Decimal('0'))
    recommendation_total = min(total_forecast_next, available_after_goal)
    reduction_needed = max(total_forecast_next - recommendation_total, Decimal('0'))
    recommended_risk = max(risk_forecast_next - reduction_needed, Decimal('0'))
    remaining_reduction = max(reduction_needed - risk_forecast_next, Decimal('0'))
    recommended_regular = max(regular_forecast_next - remaining_reduction, Decimal('0'))
    return recommendation_total, {
        'regular': recommended_regular,
        'risk': recommended_risk,
    }


def _month_options_from_bounds(first, last):
    if not first or not last:
        return []
    first_date = timezone.localtime(first).date() if isinstance(first, datetime) else first
    last_date = timezone.localtime(last).date() if isinstance(last, datetime) else last
    return [
        {'value': month.strftime('%Y-%m'), 'label': _format_month_full(month)}
        for month in _month_range(_month_start(first_date), _month_start(last_date))
    ]


def _parse_row_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    excel_date = _parse_excel_serial_date(text)
    if excel_date:
        return excel_date
    for fmt in (
        '%Y-%m-%d',
        '%d.%m.%Y',
        '%d/%m/%Y',
        '%Y-%m-%d %H:%M:%S',
        '%d.%m.%Y %H:%M:%S',
        '%d/%m/%Y %H:%M:%S',
        '%d.%m.%Y %H:%M',
        '%d/%m/%Y %H:%M',
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_excel_serial_date(value):
    normalized = str(value or '').strip().replace(',', '.')
    try:
        numeric = Decimal(normalized)
    except Exception:
        return None
    if numeric < Decimal('20000') or numeric > Decimal('60000'):
        return None
    return date(1899, 12, 30) + timedelta(days=int(numeric))


def _to_decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace('\u2212', '-')
    if not text:
        return None
    text = ''.join(char for char in text if char.isdigit() or char in '-,.')
    if not text:
        return None
    if text.count(',') == 1 and text.count('.') > 1:
        text = text.replace('.', '').replace(',', '.')
    elif text.count('.') == 1 and text.count(',') > 1:
        text = text.replace(',', '')
    elif ',' in text and '.' in text:
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif text.count('.') > 1:
        text = text.replace('.', '')
    else:
        text = text.replace(',', '.')
    try:
        return Decimal(text)
    except Exception:
        return None


def _month_start(value):
    return date(value.year, value.month, 1)


def _month_end_dt(month):
    next_month = _add_months(month, 1)
    end_date = datetime.combine(next_month, time.min) - timedelta(microseconds=1)
    return timezone.make_aware(end_date, timezone.get_current_timezone())


def _month_range(start_month, end_month):
    months = []
    current = start_month
    while current <= end_month:
        months.append(current)
        current = _add_months(current, 1)
    return months


def _add_months(month, delta):
    month_index = month.month - 1 + delta
    year = month.year + month_index // 12
    month_number = month_index % 12 + 1
    return date(year, month_number, 1)


def _cell_key(month, category):
    return f'{month.strftime("%Y-%m")}||{category}'


def _mean_decimal(values):
    values = list(values)
    if not values:
        return Decimal('0')
    return sum(values, Decimal('0')) / Decimal(len(values))


def _std_decimal(values, mean_value):
    values = list(values)
    if not values:
        return Decimal('0')
    variance = sum((float(value - mean_value) ** 2 for value in values), 0.0) / len(values)
    return Decimal(str(sqrt(variance)))


def _percent(numerator, denominator):
    if not denominator:
        return Decimal('0')
    return Decimal(numerator) / Decimal(denominator) * 100


def _money(value):
    value = _round_decimal(value, Decimal('1'))
    return f'{value:,.0f}'.replace(',', ' ')


def _signed_money(value):
    prefix = '+' if value > 0 else ''
    return prefix + _money(value)


def _plain_percent(value):
    return f'{_round_decimal(value, Decimal("0.1"))}%'


def _signed_percent(value):
    prefix = '+' if value > 0 else ''
    return prefix + _plain_percent(value)


def _format_number(value):
    return str(_round_decimal(value, Decimal('0.01'))).replace('.', ',')


def _format_js_number(value):
    return str(_round_decimal(value, Decimal('0.0001')))


def _input_decimal(value):
    return str(_round_decimal(value, Decimal('0.01')))


def _input_integer(value):
    return str(int(_round_decimal(value, Decimal('1'))))


def _round_decimal(value, quant):
    return Decimal(value or 0).quantize(quant, rounding=ROUND_HALF_UP)


def _chart_value(value):
    return float(_round_decimal(value, Decimal('0.01')))


def _compact_money(value):
    value = abs(Decimal(value or 0))
    if value >= Decimal('1000000'):
        return f'{_round_decimal(value / Decimal("1000000"), Decimal("0.1"))}m'.replace('.0m', 'm')
    if value >= Decimal('1000'):
        return f'{_round_decimal(value / Decimal("1000"), Decimal("0.1"))}k'.replace('.0k', 'k')
    return _money(value)


def _format_month_short(month):
    return f'{MONTH_SHORT[month.month - 1]} {month.year}'


def _format_month_full(month):
    return f'{MONTH_FULL[month.month - 1]} {month.year}'.capitalize()
