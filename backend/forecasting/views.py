from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

import pandas as pd
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from core.models import Currency, Project

from .services import (
    build_file_month_report,
    build_month_report,
    get_available_months,
    get_available_months_from_rows,
    normalize_budget_window,
    parse_report_date,
    save_budget_plan,
)


DEFAULT_PROJECT_NAME = 'Личные финансы'


@login_required
def month_summary(request):
    if request.GET.get('clear_file') == '1':
        request.session.pop('forecast_upload_rows', None)
        request.session.pop('forecast_upload_preview', None)

    show_file_mapping_modal = False
    file_upload_error = ''
    file_upload_notice = ''
    budget_notice = ''
    budget_error = ''
    action = (request.POST.get('action') or '').strip()
    if request.method == 'POST' and action == 'upload_file' and request.FILES.get('forecast_file'):
        preview = _read_uploaded_forecast_file(request.FILES['forecast_file'])
        if preview['rows'] and preview['columns']:
            request.session['forecast_upload_preview'] = preview
            request.session.modified = True
            show_file_mapping_modal = True
            file_upload_notice = f'Файл прочитан: строк — {len(preview["rows"])}. Сопоставьте столбцы.'
        else:
            show_file_mapping_modal = True
            file_upload_error = 'Не удалось прочитать файл. Загрузите CSV/XLSX и сопоставьте столбцы вручную.'
    elif request.method == 'POST' and action == 'map_file':
        preview = request.session.get('forecast_upload_preview') or {}
        rows = _map_uploaded_rows(preview, request.POST)
        if rows:
            request.session['forecast_upload_rows'] = rows
            request.session.pop('forecast_upload_preview', None)
            request.session.modified = True
            file_upload_notice = f'Файл сопоставлен: операций для анализа — {len(rows)}.'
        else:
            show_file_mapping_modal = True
            file_upload_error = 'Не удалось сопоставить файл. Проверьте столбцы даты и суммы.'

    source = (request.GET.get('source') or request.POST.get('source') or 'system').strip()
    upload_rows = request.session.get('forecast_upload_rows') or []
    file_preview = request.session.get('forecast_upload_preview') or {}
    if source == 'file' and not upload_rows:
        source = 'system'

    month_param = (request.GET.get('month') or request.POST.get('month') or '').strip()
    report_currency = (request.GET.get('report_currency') or request.POST.get('report_currency') or 'RUB').strip().upper()
    threshold_param = (request.GET.get('threshold') or request.POST.get('threshold') or '2.0').replace(',', '.')
    selected_dimension = (request.GET.get('dimension') or request.POST.get('dimension') or 'category').strip()
    budget_window = normalize_budget_window(request.GET.get('budget_window') or request.POST.get('budget_window'))
    if selected_dimension not in {'category', 'subcategory'}:
        selected_dimension = 'category'

    selected_month = None
    if month_param:
        try:
            parsed = datetime.strptime(month_param, '%Y-%m')
            selected_month = parsed.date().replace(day=1)
        except ValueError:
            selected_month = None

    try:
        anomaly_threshold = Decimal(threshold_param)
    except (InvalidOperation, TypeError):
        anomaly_threshold = Decimal('2.0')
    anomaly_threshold = min(max(anomaly_threshold, Decimal('0')), Decimal('4.0'))

    projects = Project.objects.filter(user=request.user, status='active').order_by('name')
    selected_project = _select_project(request.user, request.GET.get('project') or request.POST.get('project'))
    selected_project_id = selected_project.id if selected_project else ''

    system_available_months = get_available_months(request.user, project_id=selected_project_id)
    file_available_months = get_available_months_from_rows(upload_rows)
    available_months = file_available_months if source == 'file' else system_available_months
    currencies = Currency.objects.filter(status='active').order_by('code')
    if source == 'file':
        report = build_file_month_report(
            upload_rows,
            selected_month=selected_month,
            report_currency=report_currency,
            anomaly_threshold=anomaly_threshold,
            dimension=selected_dimension,
            budget_distribution_window=budget_window,
        )
    else:
        report = build_month_report(
            request.user,
            selected_month=selected_month,
            report_currency=report_currency,
            anomaly_threshold=anomaly_threshold,
            project_id=selected_project_id,
            dimension=selected_dimension,
            budget_distribution_window=budget_window,
        )

    if request.method == 'POST' and action == 'save_budget_plan':
        if source != 'system':
            budget_error = 'План бюджета можно сохранять только для данных системы.'
        elif not report.get('has_data') or not report.get('budget'):
            budget_error = 'Недостаточно данных для сохранения бюджета.'
        elif save_budget_plan(request.user, selected_project, report['budget'], request.POST):
            budget_notice = f'План на {report["budget"]["plan_month_label"]} сохранен.'
            report = build_month_report(
                request.user,
                selected_month=selected_month,
                report_currency=report_currency,
                anomaly_threshold=anomaly_threshold,
                project_id=selected_project_id,
                dimension=selected_dimension,
                budget_distribution_window=budget_window,
            )
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                saved_at = report.get('budget', {}).get('saved_at', '')
                return JsonResponse({
                    'ok': True,
                    'message': budget_notice,
                    'saved_at': saved_at,
                })
        else:
            budget_error = 'Не удалось сохранить план. Проверьте суммы по категориям.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'ok': False,
                'message': budget_error or 'Не удалось сохранить план.',
            }, status=400)

    return render(request, 'forecasting/month_summary.html', {
        'available_months': available_months,
        'system_available_months': system_available_months,
        'file_available_months': file_available_months,
        'currencies': currencies,
        'projects': projects,
        'report': report,
        'source': source,
        'has_uploaded_file': bool(upload_rows),
        'file_preview': file_preview,
        'show_file_mapping_modal': show_file_mapping_modal,
        'file_upload_error': file_upload_error,
        'file_upload_notice': file_upload_notice,
        'file_preview_range': _date_range_from_preview(file_preview, _guess_file_columns(file_preview.get('columns') or [])),
        'file_rows_range': _date_range_from_rows(upload_rows),
        'file_column_guesses': _guess_file_columns(file_preview.get('columns') or []),
        'selected_project_id': str(selected_project_id),
        'selected_project_name': selected_project.name if selected_project else '—',
        'selected_month_value': report.get('selected_month_value') or month_param,
        'selected_currency': report_currency,
        'selected_threshold': str(anomaly_threshold),
        'selected_dimension': selected_dimension,
        'selected_budget_window': budget_window,
        'budget_notice': budget_notice,
        'budget_error': budget_error,
    })


def _select_project(user, raw_project_id):
    projects = Project.objects.filter(user=user, status='active')
    if raw_project_id and str(raw_project_id).isdigit():
        project = projects.filter(id=int(raw_project_id)).first()
        if project:
            return project
    return (
        projects.filter(name__iexact=DEFAULT_PROJECT_NAME).first()
        or projects.order_by('name').first()
    )


def _read_uploaded_forecast_file(uploaded_file):
    name = (uploaded_file.name or '').lower()
    content = uploaded_file.read()
    try:
        if name.endswith(('.xlsx', '.xls')):
            frame = pd.read_excel(BytesIO(content))
        else:
            frame = pd.read_csv(BytesIO(content), sep=None, engine='python')
    except Exception:
        return {'columns': [], 'rows': []}

    if frame.empty:
        return {'columns': [], 'rows': []}

    frame = frame.fillna('')
    columns = [str(column).replace('\ufeff', '').strip().strip('"').strip("'") for column in frame.columns]
    frame.columns = columns
    rows = []
    for index, item in frame.iterrows():
        rows.append({column: _stringify_value(item.get(column)) for column in columns})
    return {'columns': columns, 'rows': rows}


def _map_uploaded_rows(preview, post_data):
    rows = preview.get('rows') or []
    column_list = preview.get('columns') or []
    columns = set(column_list)
    guesses = _guess_file_columns(column_list)

    date_column = _resolve_column(column_list, post_data.get('date_column') or guesses.get('date'))
    amount_column = _resolve_column(column_list, post_data.get('amount_column') or guesses.get('amount'))
    if not date_column or not amount_column:
        return []

    currency_column = _resolve_column(column_list, post_data.get('currency_column') or guesses.get('currency'))
    type_column = _resolve_column(column_list, post_data.get('type_column') or guesses.get('type'))
    status_column = _resolve_column(column_list, post_data.get('status_column') or guesses.get('status'))
    category_column = _resolve_column(column_list, post_data.get('category_column') or guesses.get('category'))
    comment_column = _resolve_column(column_list, post_data.get('comment_column') or guesses.get('comment'))

    mapped = []
    for index, row in enumerate(rows):
        status_value = row.get(status_column) if status_column in columns else ''
        if _is_failed_operation_status(status_value):
            continue
        date_value = row.get(date_column)
        amount_value = row.get(amount_column)
        if not date_value or not amount_value:
            continue
        operation_type = row.get(type_column) if type_column in columns else ''
        amount_value = _apply_operation_type_to_amount(amount_value, operation_type)
        if parse_report_date(date_value) is None or _parse_decimalish(amount_value) is None:
            continue
        category_value = row.get(category_column) if category_column in columns else 'Без категории'
        category, subcategory = _split_category_value(category_value)
        mapped.append({
            'id': str(index + 1),
            'date': date_value,
            'amount': amount_value,
            'currency': row.get(currency_column) if currency_column in columns else 'RUB',
            'category': category,
            'subcategory': subcategory,
            'comment': row.get(comment_column) if comment_column in columns else '',
            'account': '',
        })
    return mapped


def _resolve_column(columns, value):
    if not value:
        return None
    if value in columns:
        return value
    column_map = {_normalize_column(column): column for column in columns}
    return column_map.get(_normalize_column(value))


def _normalize_column(value):
    return ''.join(str(value or '').replace('\ufeff', '').strip().strip('"').strip("'").lower().replace('ё', 'е').split())


def _first_existing(column_map, candidates):
    for candidate in candidates:
        key = _normalize_column(candidate)
        if key in column_map:
            return column_map[key]
    return None


def _guess_file_columns(columns):
    column_map = {_normalize_column(column): column for column in columns}
    return {
        'date': _first_existing(column_map, ['date', 'дата', 'датаоперации', 'operationdate', 'datetime']) or '',
        'amount': _first_existing(column_map, ['amount', 'сумма', 'суммаоперации', 'sum', 'value']) or '',
        'type': _first_existing(column_map, ['type', 'тип', 'типоперации', 'transactiontype']) or '',
        'status': _first_existing(column_map, ['status', 'статус', 'статусоперации', 'operationstatus']) or '',
        'currency': _first_existing(column_map, ['currency', 'валюта']) or '',
        'category': _first_existing(column_map, ['category', 'категория', 'насчет/накатегорию', 'насчетнаккатегорию', 'насчетнакaтегорию', 'насчет/накатегорию', 'насчёт/накатегорию']) or '',
        'comment': _first_existing(column_map, ['comment', 'комментарий', 'description', 'описание', 'заметки']) or '',
    }


def _stringify_value(value):
    if pd.isna(value):
        return ''
    if hasattr(value, 'to_pydatetime'):
        return value.to_pydatetime().date().isoformat()
    return str(value).strip()


def _apply_operation_type_to_amount(amount_value, operation_type):
    text = str(amount_value or '').strip()
    type_text = str(operation_type or '').strip().lower()
    numeric = _parse_decimalish(text)
    if numeric is None:
        return text
    numeric = abs(numeric)
    if 'расход' in type_text or 'expense' in type_text or 'debit' in type_text:
        return str(-numeric)
    if 'доход' in type_text or 'income' in type_text or 'credit' in type_text:
        return str(numeric)
    return text


def _is_failed_operation_status(value):
    text = str(value or '').strip().lower()
    if not text:
        return False
    failed_markers = ('failed', 'declined', 'rejected', 'cancelled', 'canceled', 'отмен', 'отклон', 'ошиб', 'неусп')
    return any(marker in text for marker in failed_markers)


def _parse_decimalish(value):
    text = str(value or '').strip().replace('\u2212', '-')
    if not text:
        return None
    normalized = ''.join(char for char in text if char.isdigit() or char in '-,.')
    if not normalized:
        return None
    if normalized.count(',') == 1 and normalized.count('.') > 1:
        normalized = normalized.replace('.', '').replace(',', '.')
    elif normalized.count('.') == 1 and normalized.count(',') > 1:
        normalized = normalized.replace(',', '')
    elif ',' in normalized and '.' in normalized:
        if normalized.rfind(',') > normalized.rfind('.'):
            normalized = normalized.replace('.', '').replace(',', '.')
        else:
            normalized = normalized.replace(',', '')
    elif normalized.count('.') > 1:
        normalized = normalized.replace('.', '')
    else:
        normalized = normalized.replace(',', '.')
    try:
        return Decimal(normalized)
    except Exception:
        return None


def _split_category_value(value):
    text = str(value or '').strip()
    if not text:
        return 'Без категории', ''
    if '(' in text and text.endswith(')'):
        category, rest = text.rsplit('(', 1)
        return category.strip() or 'Без категории', rest[:-1].strip()
    return text, ''


def _date_range_from_preview(preview, guesses):
    rows = preview.get('rows') or []
    date_column = guesses.get('date') if guesses else ''
    if not rows or not date_column:
        return ''
    return _format_date_range(row.get(date_column) for row in rows)


def _date_range_from_rows(rows):
    return _format_date_range(row.get('date') for row in rows)


def _format_date_range(values):
    dates = [parse_report_date(value) for value in values]
    dates = [item for item in dates if item]
    if not dates:
        return ''
    return f'{min(dates).strftime("%d.%m.%Y")} - {max(dates).strftime("%d.%m.%Y")}'
