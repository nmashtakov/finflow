from __future__ import annotations

from datetime import date, timedelta
import json
from typing import Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from capital.forms import CapitalPositionForm, SteamAccountForm, WithdrawalSiteForm
from capital.models import CapitalPosition, SteamAccount, WithdrawalSite
from capital.services import (
    copy_snapshots_from_previous_date,
    ensure_steam_positions,
    get_capital_at_date,
    get_capital_timeseries,
    get_snapshot_matrix,
    save_snapshot_from_post,
)
from core.models import Currency, Project


def _parse_date(value: Optional[str], default: Optional[date] = None) -> date:
    default = default or timezone.localdate()
    if not value:
        return default
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return default


@login_required
def capital_dashboard(request):
    user = request.user
    as_of = _parse_date(request.GET.get('as_of'))
    report_currency = (request.GET.get('report_currency') or 'RUB').strip().upper()
    granularity = (request.GET.get('granularity') or 'day').strip().lower()
    if granularity not in ('day', 'month'):
        granularity = 'day'

    range_days = int(request.GET.get('range_days') or 90)
    range_days = max(7, min(range_days, 730))
    date_from = as_of - timedelta(days=range_days)

    capital = get_capital_at_date(user, as_of, report_currency)
    prev_day = get_capital_at_date(user, as_of - timedelta(days=1), report_currency)
    prev_week = get_capital_at_date(user, as_of - timedelta(days=7), report_currency)
    prev_month = get_capital_at_date(user, as_of - timedelta(days=30), report_currency)

    def _delta(current, previous):
        if previous is None:
            return None
        return current - previous

    timeseries = get_capital_timeseries(user, date_from, as_of, report_currency, granularity)

    currencies = list(Currency.objects.filter(status='active').order_by('code').values_list('code', flat=True))
    if report_currency not in currencies:
        currencies = [report_currency] + currencies

    projects = Project.objects.filter(user=user, status='active').order_by('name')
    project_filter = request.GET.get('project')
    positions = capital['positions']
    if project_filter:
        positions = [p for p in positions if str(p['project_id']) == project_filter]

    kind_labels = {
        'instrument': 'Инструменты',
        'bank_link': 'Денежные средства',
        'inventory': 'Товарный остаток',
        'steam': 'Steam',
    }

    by_kind_float = {k: float(v) for k, v in capital['by_kind'].items()}

    return render(request, 'capital/dashboard.html', {
        'as_of': as_of,
        'report_currency': report_currency,
        'granularity': granularity,
        'range_days': range_days,
        'capital': capital,
        'positions': positions,
        'timeseries_json': json.dumps(timeseries),
        'by_kind_json': json.dumps(by_kind_float),
        'kind_labels_json': json.dumps(kind_labels),
        'delta_day': _delta(capital['total'], prev_day['total']),
        'delta_week': _delta(capital['total'], prev_week['total']),
        'delta_month': _delta(capital['total'], prev_month['total']),
        'currencies': currencies,
        'projects': projects,
        'project_filter': project_filter or '',
        'kind_labels': kind_labels,
    })


@login_required
def capital_settings(request):
    user = request.user
    tab = (request.GET.get('tab') or 'sites').strip()

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'create_site':
            form = WithdrawalSiteForm(request.POST)
            if form.is_valid():
                site = form.save(commit=False)
                site.user = user
                site.save()
                ensure_steam_positions(user)
                messages.success(request, f'Сайт «{site.name}» добавлен.')
                return redirect(reverse('capital:settings') + '?tab=sites')
            site_form = form
        elif action == 'edit_site':
            site = get_object_or_404(WithdrawalSite, pk=request.POST.get('site_id'), user=user)
            form = WithdrawalSiteForm(request.POST, instance=site)
            if form.is_valid():
                form.save()
                messages.success(request, 'Сайт обновлён.')
                return redirect(reverse('capital:settings') + '?tab=sites')
            site_form = form
        elif action == 'delete_site':
            site = get_object_or_404(WithdrawalSite, pk=request.POST.get('site_id'), user=user)
            site.is_active = False
            site.save(update_fields=['is_active'])
            messages.success(request, 'Сайт деактивирован.')
            return redirect(reverse('capital:settings') + '?tab=sites')

        elif action == 'create_steam':
            form = SteamAccountForm(request.POST, user=user)
            if form.is_valid():
                account = form.save(commit=False)
                account.user = user
                account.save()
                ensure_steam_positions(user)
                messages.success(request, f'Steam-аккаунт «{account.account_code}» добавлен.')
                return redirect(reverse('capital:settings') + '?tab=steam')
            steam_form = form
        elif action == 'edit_steam':
            account = get_object_or_404(SteamAccount, pk=request.POST.get('steam_id'), user=user)
            form = SteamAccountForm(request.POST, instance=account, user=user)
            if form.is_valid():
                form.save()
                ensure_steam_positions(user)
                messages.success(request, 'Steam-аккаунт обновлён.')
                return redirect(reverse('capital:settings') + '?tab=steam')
            steam_form = form
        elif action == 'delete_steam':
            account = get_object_or_404(SteamAccount, pk=request.POST.get('steam_id'), user=user)
            account.is_active = False
            account.save(update_fields=['is_active'])
            messages.success(request, 'Steam-аккаунт деактивирован.')
            return redirect(reverse('capital:settings') + '?tab=steam')

        elif action == 'create_position':
            form = CapitalPositionForm(request.POST, user=user)
            if form.is_valid():
                position = form.save(commit=False)
                position.user = user
                if position.kind == CapitalPosition.Kind.BANK_LINK and position.linked_account_id:
                    position.currency = position.linked_account.currency
                    position.name = position.name or position.linked_account.name
                position.save()
                messages.success(request, f'Позиция «{position.name}» добавлена.')
                return redirect(reverse('capital:settings') + '?tab=positions')
            position_form = form
        elif action == 'edit_position':
            position = get_object_or_404(CapitalPosition, pk=request.POST.get('position_id'), user=user)
            form = CapitalPositionForm(request.POST, instance=position, user=user)
            if form.is_valid():
                form.save()
                messages.success(request, 'Позиция обновлена.')
                return redirect(reverse('capital:settings') + '?tab=positions')
            position_form = form
        elif action == 'delete_position':
            position = get_object_or_404(CapitalPosition, pk=request.POST.get('position_id'), user=user)
            position.status = CapitalPosition.Status.DELETED
            position.save(update_fields=['status'])
            messages.success(request, 'Позиция удалена.')
            return redirect(reverse('capital:settings') + '?tab=positions')

    sites = WithdrawalSite.objects.filter(user=user).order_by('sort_order', 'name')
    steam_accounts = SteamAccount.objects.filter(user=user).select_related('project').order_by('account_code')
    positions = (
        CapitalPosition.objects.filter(user=user)
        .exclude(status=CapitalPosition.Status.DELETED)
        .select_related('project', 'linked_account', 'steam_account', 'withdrawal_site')
        .order_by('sort_order', 'name')
    )

    return render(request, 'capital/settings.html', {
        'tab': tab,
        'sites': sites,
        'steam_accounts': steam_accounts,
        'positions': positions,
        'site_form': locals().get('site_form', WithdrawalSiteForm()),
        'steam_form': locals().get('steam_form', SteamAccountForm(user=user)),
        'position_form': locals().get('position_form', CapitalPositionForm(user=user)),
        'purpose_choices': SteamAccount.Purpose.choices,
        'kind_choices': CapitalPosition.Kind.choices,
        'asset_class_choices': CapitalPosition.AssetClass.choices,
    })


@login_required
def capital_snapshots(request):
    user = request.user
    snapshot_date = _parse_date(request.GET.get('date') or request.POST.get('snapshot_date'))

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'copy_previous':
            copied = copy_snapshots_from_previous_date(user, snapshot_date)
            messages.success(request, f'Скопировано позиций: {copied}.')
            return redirect(reverse('capital:snapshots') + f'?date={snapshot_date.isoformat()}')
        if action == 'save_all':
            saved = 0
            position_ids = request.POST.getlist('position_id')
            for pid in position_ids:
                position = get_object_or_404(
                    CapitalPosition,
                    pk=pid,
                    user=user,
                )
                if position.status == CapitalPosition.Status.DELETED:
                    continue
                prefix = f'pos_{pid}_'
                data = {
                    key[len(prefix):]: value
                    for key, value in request.POST.items()
                    if key.startswith(prefix)
                }
                if save_snapshot_from_post(position, snapshot_date, data):
                    saved += 1
            messages.success(request, f'Сохранено снимков: {saved}.')
            return redirect(reverse('capital:snapshots') + f'?date={snapshot_date.isoformat()}')

    matrix = get_snapshot_matrix(user, snapshot_date)
    return render(request, 'capital/snapshots.html', {
        'snapshot_date': snapshot_date,
        **matrix,
        'asset_class_labels': dict(CapitalPosition.AssetClass.choices),
    })
