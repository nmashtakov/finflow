from __future__ import annotations

from bisect import bisect_right
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Prefetch, Q

from capital.models import CapitalPosition, CapitalSnapshot, SteamAccount, WithdrawalSite
from core.models import AccountBalanceSnapshot, CurrencyRate


KIND_LABELS = dict(CapitalPosition.Kind.choices)
ASSET_CLASS_LABELS = dict(CapitalPosition.AssetClass.choices)


def _normalize_currency(code: str | None) -> str:
    return (code or 'RUB').upper()


def build_rate_lookup(currencies: set[str], max_date: date, min_date: date | None = None) -> dict[str, list[tuple[date, Decimal]]]:
    min_date = min_date or max_date
    lookup: dict[str, list[tuple[date, Decimal]]] = {'RUB': [(min_date, Decimal('1'))]}
    normalized = {_normalize_currency(c) for c in currencies if c}
    normalized.add('RUB')
    if 'USDT' in normalized:
        normalized.add('USD')
    for currency in normalized:
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


def rate_on_date(rate_lookup: dict[str, list[tuple[date, Decimal]]], currency: str, target_date: date) -> Decimal | None:
    currency = _normalize_currency(currency)
    if currency == 'USDT':
        currency = 'USD'
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


def convert_amount(
    amount: Decimal | None,
    source_currency: str,
    target_currency: str,
    target_date: date,
    rate_lookup: dict[str, list[tuple[date, Decimal]]],
) -> Decimal | None:
    source = _normalize_currency(source_currency)
    target = _normalize_currency(target_currency)
    if not target or source == target:
        return Decimal(amount or 0)
    source_rate = rate_on_date(rate_lookup, source, target_date)
    target_rate = rate_on_date(rate_lookup, target, target_date)
    if source_rate is None or target_rate is None or target_rate == 0:
        return None
    return (Decimal(amount or 0) * source_rate) / target_rate


def ensure_steam_positions(user) -> int:
    """Create CapitalPosition rows for each active (steam_account, withdrawal_site) pair."""
    steam_accounts = SteamAccount.objects.filter(user=user, is_active=True)
    sites = WithdrawalSite.objects.filter(user=user, is_active=True)
    created = 0
    for steam_account in steam_accounts:
        for site in sites:
            exists = CapitalPosition.objects.filter(
                user=user,
                kind=CapitalPosition.Kind.STEAM,
                steam_account=steam_account,
                withdrawal_site=site,
            ).exclude(status=CapitalPosition.Status.DELETED).exists()
            if exists:
                continue
            CapitalPosition.objects.create(
                user=user,
                project=steam_account.project,
                name=f'{steam_account.account_code} / {site.name}',
                kind=CapitalPosition.Kind.STEAM,
                currency=site.currency or steam_account.currency,
                steam_account=steam_account,
                withdrawal_site=site,
            )
            created += 1
    return created


def _active_positions_qs(user):
    return (
        CapitalPosition.objects.filter(user=user)
        .exclude(status=CapitalPosition.Status.DELETED)
        .select_related(
            'project',
            'linked_account',
            'steam_account',
            'withdrawal_site',
        )
        .order_by('sort_order', 'name', 'id')
    )


def _position_is_liability(position: CapitalPosition) -> bool:
    if position.is_liability:
        return True
    if position.kind == CapitalPosition.Kind.BANK_LINK and position.linked_account_id:
        account = position.linked_account
        if account.account_type == 'debt' or (account.total_debt or 0) > 0:
            return True
    return False


def _position_included(position: CapitalPosition) -> bool:
    if not position.include_in_total:
        return False
    if position.kind == CapitalPosition.Kind.BANK_LINK and position.linked_account_id:
        if not position.linked_account.include_in_total:
            return False
    return True


def _last_capital_snapshot(position: CapitalPosition, as_of: date) -> CapitalSnapshot | None:
    return (
        position.snapshots.filter(snapshot_date__lte=as_of)
        .order_by('-snapshot_date')
        .first()
    )


def _last_account_snapshot(account_id: int, as_of: date) -> AccountBalanceSnapshot | None:
    return (
        AccountBalanceSnapshot.objects.filter(account_id=account_id, snapshot_date__lte=as_of)
        .order_by('-snapshot_date')
        .first()
    )


def _snapshot_native_value(position: CapitalPosition, snapshot: CapitalSnapshot | None) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Returns (native_value, nominal_steam, adjusted_steam)."""
    if snapshot is None:
        return None, None, None
    if position.kind == CapitalPosition.Kind.STEAM:
        balance = snapshot.site_balance or Decimal('0')
        skins = snapshot.skins_value or Decimal('0')
        nominal = balance + skins
        coeff = snapshot.coefficient
        if coeff is None and position.withdrawal_site_id:
            coeff = position.withdrawal_site.default_coefficient
        if coeff is None:
            coeff = Decimal('1')
        return nominal * coeff, nominal, nominal * coeff
    return snapshot.total_value, None, snapshot.total_value


def _position_value_at_date(position: CapitalPosition, as_of: date) -> dict[str, Any]:
    native_value = None
    nominal = None
    snapshot = None
    snapshot_date = None
    source = 'none'

    if position.kind == CapitalPosition.Kind.BANK_LINK:
        if not position.linked_account_id:
            return _empty_position_row(position, as_of)
        account = position.linked_account
        abs_snap = _last_account_snapshot(account.id, as_of)
        if abs_snap:
            native_value = abs_snap.balance
            snapshot_date = abs_snap.snapshot_date
            source = 'account_snapshot'
        currency = account.currency
    else:
        snapshot = _last_capital_snapshot(position, as_of)
        currency = position.currency
        if snapshot:
            native_value, nominal, _ = _snapshot_native_value(position, snapshot)
            snapshot_date = snapshot.snapshot_date
            source = 'capital_snapshot'

    included = _position_included(position)
    is_liability = _position_is_liability(position)

    return {
        'position': position,
        'position_id': position.id,
        'name': position.name,
        'kind': position.kind,
        'kind_label': KIND_LABELS.get(position.kind, position.kind),
        'asset_class': position.asset_class,
        'asset_class_label': ASSET_CLASS_LABELS.get(position.asset_class, ''),
        'project_id': position.project_id,
        'project_name': position.project.name if position.project_id else '—',
        'currency': currency,
        'native_value': native_value,
        'nominal_value': nominal,
        'snapshot_date': snapshot_date,
        'source': source,
        'included': included,
        'is_liability': is_liability,
        'snapshot': snapshot,
        'steam_account_code': position.steam_account.account_code if position.steam_account_id else '',
        'withdrawal_site_name': position.withdrawal_site.name if position.withdrawal_site_id else '',
    }


def _empty_position_row(position: CapitalPosition, as_of: date) -> dict[str, Any]:
    return {
        'position': position,
        'position_id': position.id,
        'name': position.name,
        'kind': position.kind,
        'kind_label': KIND_LABELS.get(position.kind, position.kind),
        'asset_class': position.asset_class,
        'asset_class_label': ASSET_CLASS_LABELS.get(position.asset_class, ''),
        'project_id': position.project_id,
        'project_name': position.project.name if position.project_id else '—',
        'currency': position.currency,
        'native_value': None,
        'nominal_value': None,
        'snapshot_date': None,
        'source': 'none',
        'included': _position_included(position),
        'is_liability': _position_is_liability(position),
        'snapshot': None,
        'steam_account_code': '',
        'withdrawal_site_name': '',
    }


def get_capital_at_date(user, as_of: date, report_currency: str = 'RUB') -> dict[str, Any]:
    report_currency = _normalize_currency(report_currency)
    positions = list(_active_positions_qs(user))
    rows = [_position_value_at_date(p, as_of) for p in positions]

    currencies = {report_currency}
    for row in rows:
        if row['native_value'] is not None:
            currencies.add(_normalize_currency(row['currency']))

    rate_lookup = build_rate_lookup(currencies, as_of)

    total = Decimal('0')
    steam_nominal = Decimal('0')
    steam_adjusted = Decimal('0')
    by_kind: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    by_project: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    by_asset_class: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    position_rows = []

    for row in rows:
        converted = None
        if row['included'] and row['native_value'] is not None:
            converted = convert_amount(row['native_value'], row['currency'], report_currency, as_of, rate_lookup)
            if converted is not None:
                signed = -converted if row['is_liability'] else converted
                total += signed
                by_kind[row['kind']] += signed
                by_project[row['project_name']] += signed
                if row['asset_class']:
                    by_asset_class[row['asset_class']] += signed
                if row['kind'] == CapitalPosition.Kind.STEAM and row['nominal_value'] is not None:
                    nom_conv = convert_amount(row['nominal_value'], row['currency'], report_currency, as_of, rate_lookup)
                    if nom_conv is not None:
                        steam_nominal += nom_conv
                        steam_adjusted += converted

        position_rows.append({
            **row,
            'converted_value': converted,
            'report_currency': report_currency,
        })

    return {
        'as_of': as_of,
        'report_currency': report_currency,
        'total': total,
        'steam_nominal': steam_nominal,
        'steam_adjusted': steam_adjusted,
        'by_kind': dict(by_kind),
        'by_project': dict(by_project),
        'by_asset_class': dict(by_asset_class),
        'positions': position_rows,
    }


def _iter_days(date_from: date, date_to: date):
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


def _month_end_dates(date_from: date, date_to: date):
    current = date(date_from.year, date_from.month, 1)
    while current <= date_to:
        last_day = monthrange(current.year, current.month)[1]
        end = date(current.year, current.month, last_day)
        if end >= date_from:
            yield min(end, date_to)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def get_capital_timeseries(
    user,
    date_from: date,
    date_to: date,
    report_currency: str = 'RUB',
    granularity: str = 'day',
) -> list[dict[str, Any]]:
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    if granularity == 'month':
        points = list(_month_end_dates(date_from, date_to))
    else:
        points = list(_iter_days(date_from, date_to))

    series = []
    for point in points:
        snapshot = get_capital_at_date(user, point, report_currency)
        series.append({
            'date': point.isoformat(),
            'total': float(snapshot['total']),
            'by_kind': {k: float(v) for k, v in snapshot['by_kind'].items()},
            'by_project': {k: float(v) for k, v in snapshot['by_project'].items()},
        })
    return series


def get_snapshot_matrix(user, snapshot_date: date) -> dict[str, Any]:
    ensure_steam_positions(user)
    positions = list(_active_positions_qs(user))

    bank_rows = []
    instrument_rows = []
    inventory_rows = []
    steam_rows = []

    for position in positions:
        row = _position_value_at_date(position, snapshot_date)
        exact_snapshot = CapitalSnapshot.objects.filter(position=position, snapshot_date=snapshot_date).first()
        row['exact_snapshot'] = exact_snapshot
        row['has_exact_snapshot'] = exact_snapshot is not None

        if position.kind == CapitalPosition.Kind.BANK_LINK:
            bank_rows.append(row)
        elif position.kind == CapitalPosition.Kind.INSTRUMENT:
            instrument_rows.append(row)
        elif position.kind == CapitalPosition.Kind.INVENTORY:
            inventory_rows.append(row)
        elif position.kind == CapitalPosition.Kind.STEAM:
            steam_rows.append(row)

    return {
        'snapshot_date': snapshot_date,
        'bank_rows': bank_rows,
        'instrument_rows': instrument_rows,
        'inventory_rows': inventory_rows,
        'steam_rows': steam_rows,
    }


def save_snapshot_from_post(position: CapitalPosition, snapshot_date: date, data: dict[str, str]) -> CapitalSnapshot | None:
    if position.kind == CapitalPosition.Kind.BANK_LINK:
        return None

    def _dec(key: str) -> Decimal | None:
        raw = (data.get(key) or '').strip().replace(',', '.')
        if not raw:
            return None
        return Decimal(raw)

    if position.kind == CapitalPosition.Kind.INSTRUMENT:
        qty = _dec('quantity')
        price = _dec('unit_price')
        if qty is None and price is None and not (data.get('total_value') or '').strip():
            return None
        total = qty * price if qty is not None and price is not None else _dec('total_value')
        if total is None:
            return None
        snap, _ = CapitalSnapshot.objects.update_or_create(
            position=position,
            snapshot_date=snapshot_date,
            defaults={
                'quantity': qty,
                'unit_price': price,
                'total_value': total,
                'note': (data.get('note') or '')[:255],
            },
        )
        return snap

    if position.kind == CapitalPosition.Kind.INVENTORY:
        total = _dec('total_value')
        if total is None:
            return None
        snap, _ = CapitalSnapshot.objects.update_or_create(
            position=position,
            snapshot_date=snapshot_date,
            defaults={'total_value': total, 'note': (data.get('note') or '')[:255]},
        )
        return snap

    if position.kind == CapitalPosition.Kind.STEAM:
        balance = _dec('site_balance') or Decimal('0')
        skins = _dec('skins_value') or Decimal('0')
        coeff_raw = _dec('coefficient')
        snap, _ = CapitalSnapshot.objects.update_or_create(
            position=position,
            snapshot_date=snapshot_date,
            defaults={
                'site_balance': balance,
                'skins_value': skins,
                'coefficient': coeff_raw,
                'total_value': Decimal('0'),
                'note': (data.get('note') or '')[:255],
            },
        )
        snap.total_value = snap.compute_total_value()
        snap.save(update_fields=['total_value'])
        return snap

    return None


def copy_snapshots_from_previous_date(user, target_date: date) -> int:
    positions = _active_positions_qs(user).exclude(kind=CapitalPosition.Kind.BANK_LINK)
    copied = 0
    for position in positions:
        if CapitalSnapshot.objects.filter(position=position, snapshot_date=target_date).exists():
            continue
        prev = (
            position.snapshots.filter(snapshot_date__lt=target_date)
            .order_by('-snapshot_date')
            .first()
        )
        if not prev:
            continue
        CapitalSnapshot.objects.create(
            position=position,
            snapshot_date=target_date,
            quantity=prev.quantity,
            unit_price=prev.unit_price,
            site_balance=prev.site_balance,
            skins_value=prev.skins_value,
            coefficient=prev.coefficient,
            total_value=prev.total_value,
            note='Скопировано с ' + prev.snapshot_date.isoformat(),
        )
        copied += 1
    return copied
