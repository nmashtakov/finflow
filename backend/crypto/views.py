from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from crypto.forms import CryptoAssetForm, CryptoPortfolioSettingsForm, CryptoTransactionForm
from crypto.models import CryptoAsset, CryptoTransaction
from crypto.services import (
    create_manual_transaction,
    ensure_default_assets,
    ensure_legacy_manual_transactions,
    get_asset_detail,
    get_or_create_settings,
    get_portfolio_summary,
    sync_crypto_rates,
    sync_transactions_from_bybit,
    sync_wallet_balances_from_bybit,
)


def _parse_date(value: Optional[str], default: Optional[date] = None) -> date:
    default = default or timezone.localdate()
    if not value:
        return default
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return default


@login_required
def crypto_dashboard(request):
    user = request.user
    ensure_default_assets(user)
    ensure_legacy_manual_transactions(user)
    settings_obj = get_or_create_settings(user)
    as_of = _parse_date(request.GET.get('as_of'))

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'sync_rates':
            result = sync_crypto_rates(user, as_of)
            source = result.get('source') or '—'
            summary = (
                f"Курсы ({source}): inserted={result['inserted']}, updated={result['updated']}, "
                f"synced={', '.join(result['synced']) or '—'}"
            )
            if result['skipped']:
                summary += f". Пропущено: {', '.join(result['skipped'])}."
            if result['failed']:
                messages.warning(request, summary + f" Ошибки: {'; '.join(result['failed'])}.")
            else:
                messages.success(request, summary)
        elif action == 'sync_transactions':
            result = sync_transactions_from_bybit(user)
            summary = (
                f"Покупки Bybit: inserted={result['inserted']}, updated={result['updated']}, "
                f"skipped={result['skipped']}"
            )
            if result.get('failed'):
                messages.warning(request, summary + f". {'; '.join(result['failed'])}.")
            else:
                messages.success(request, summary + '. Балансы Bybit обновлены.')
        elif action == 'sync_balances':
            result = sync_wallet_balances_from_bybit(user)
            if result.get('failed'):
                messages.warning(request, f"Балансы: {'; '.join(result['failed'])}.")
            else:
                messages.success(request, f"Балансы Bybit обновлены: {result.get('updated', 0)} монет.")
        return redirect(reverse('crypto:dashboard') + f'?as_of={as_of.isoformat()}')

    portfolio = get_portfolio_summary(user, as_of)
    transactions = (
        CryptoTransaction.objects.filter(user=user)
        .select_related('asset')
        .order_by('-occurred_at', '-id')[:200]
    )

    allocation = [
        {
            'symbol': row.symbol,
            'name': row.asset.name,
            'value': float(row.current_value),
            'pct': float(row.allocation_pct or 0),
        }
        for row in portfolio['assets']
    ]

    return render(request, 'crypto/dashboard.html', {
        'as_of': as_of,
        'settings': settings_obj,
        'portfolio': portfolio,
        'transactions': transactions,
        'allocation_json': json.dumps(allocation),
    })


@login_required
def crypto_asset_detail(request, symbol: str):
    user = request.user
    as_of = _parse_date(request.GET.get('as_of'))
    detail = get_asset_detail(user, symbol, as_of)
    if detail is None:
        messages.error(request, f'Актив {symbol.upper()} не найден.')
        return redirect('crypto:dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')
        tx_id = request.POST.get('transaction_id')
        if action == 'add_transaction':
            form = CryptoTransactionForm(request.POST)
            if form.is_valid():
                try:
                    create_manual_transaction(
                        user,
                        asset=detail['asset'],
                        occurred_at=form.cleaned_data['occurred_at'],
                        quantity=form.cleaned_data['quantity'],
                        price_quote=form.cleaned_data.get('price_quote'),
                        total_quote=form.cleaned_data.get('total_quote'),
                        fee_quote=form.cleaned_data.get('fee_quote') or Decimal('0'),
                        note=form.cleaned_data.get('note') or '',
                    )
                    messages.success(request, 'Транзакция добавлена.')
                except ValueError as exc:
                    messages.error(request, str(exc))
            else:
                messages.error(request, 'Проверьте поля формы.')
        elif action == 'delete' and tx_id:
            tx = get_object_or_404(CryptoTransaction, pk=tx_id, user=user, asset=detail['asset'])
            if tx.source == CryptoTransaction.Source.MANUAL:
                tx.delete()
                messages.success(request, 'Транзакция удалена.')
            else:
                messages.error(request, 'Транзакции из Bybit можно только скрыть через деактивацию актива.')
        return redirect(reverse('crypto:asset', kwargs={'symbol': symbol.upper()}))

    return render(request, 'crypto/asset_detail.html', {
        'as_of': as_of,
        'detail': detail,
        'summary': detail['summary'],
        'asset': detail['asset'],
        'transactions': detail['transactions'],
        'tx_form': CryptoTransactionForm(initial={'tx_type': CryptoTransaction.TxType.BUY}),
    })


@login_required
def crypto_settings(request):
    user = request.user
    ensure_default_assets(user)
    settings_obj = get_or_create_settings(user)
    tab = (request.GET.get('tab') or 'assets').strip()

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'save_settings':
            form = CryptoPortfolioSettingsForm(request.POST, instance=settings_obj, user=user)
            if form.is_valid():
                saved = form.save(commit=False)
                saved.user = user
                saved.save()
                messages.success(request, 'Настройки сохранены.')
                return redirect(reverse('crypto:settings') + '?tab=connection')
            settings_form = form
        elif action == 'create_asset':
            form = CryptoAssetForm(request.POST)
            if form.is_valid():
                asset = form.save(commit=False)
                asset.user = user
                asset.save()
                messages.success(request, f'Актив {asset.symbol} добавлен.')
                return redirect(reverse('crypto:settings') + '?tab=assets')
            asset_form = form
        elif action == 'edit_asset':
            asset = get_object_or_404(CryptoAsset, pk=request.POST.get('asset_id'), user=user)
            form = CryptoAssetForm(request.POST, instance=asset)
            if form.is_valid():
                form.save()
                messages.success(request, 'Актив обновлён.')
                return redirect(reverse('crypto:settings') + '?tab=assets')
            asset_form = form
        elif action == 'seed_defaults':
            created = ensure_default_assets(user)
            messages.success(request, f'Добавлено активов: {created}.')
            return redirect(reverse('crypto:settings') + '?tab=assets')

    assets = CryptoAsset.objects.filter(user=user).order_by('sort_order', 'symbol')
    return render(request, 'crypto/settings.html', {
        'tab': tab,
        'assets': assets,
        'settings': settings_obj,
        'settings_form': locals().get('settings_form', CryptoPortfolioSettingsForm(instance=settings_obj, user=user)),
        'asset_form': locals().get('asset_form', CryptoAssetForm()),
    })
