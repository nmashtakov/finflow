from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from core.currencies import split_stable_quote_pair
from core.models import CurrencyRate
from core.services.bybit_market import fetch_bybit_spot_prices
from core.services.crypto_rates import sync_crypto_rates_incremental
from crypto.models import (
    DEFAULT_CRYPTO_ASSETS,
    QUOTE_CURRENCIES,
    CryptoAsset,
    CryptoPortfolioSettings,
    CryptoTransaction,
)
from transactions.integrations.bybit.client import BybitV5Client
from transactions.integrations.bybit.sync import sync_bybit_transaction_log
from transactions.models import BybitConnection, BybitExternalEvent


MONEY_Q = Decimal('0.00000001')
USD_EQUIVALENTS = QUOTE_CURRENCIES
BYBIT_COIN_ALIASES = {
    'TON': 'GRAM',
}
TRADE_MATCH_SECONDS = 30

LEGACY_MANUAL_TRANSACTIONS = (
    {
        'external_key': 'manual:eth:2018-05-13-external',
        'symbol': 'ETH',
        'quantity': Decimal('0.02464'),
        'total_quote': Decimal('32.40'),
        'fee_quote': Decimal('0'),
        'occurred_at': '2018-05-13T07:30:00',
        'note': 'Покупка ETH вне Bybit (до биржи)',
    },
)


@dataclass
class AssetSummary:
    asset: CryptoAsset
    symbol: str
    quantity: Decimal
    avg_buy_price: Decimal
    cost_basis: Decimal
    current_price: Decimal
    current_value: Decimal
    change_24h_pct: Decimal | None
    profit_abs: Decimal
    profit_pct: Decimal | None
    allocation_pct: Decimal | None
    ledger_quantity: Decimal | None = None


def get_or_create_settings(user) -> CryptoPortfolioSettings:
    settings_obj, _ = CryptoPortfolioSettings.objects.get_or_create(user=user)
    return settings_obj


def ensure_default_assets(user) -> int:
    created = 0
    for index, (symbol, name) in enumerate(DEFAULT_CRYPTO_ASSETS):
        _, was_created = CryptoAsset.objects.get_or_create(
            user=user,
            symbol=symbol,
            defaults={'name': name, 'sort_order': index, 'is_active': True},
        )
        if was_created:
            created += 1
    return created


def active_asset_symbols(user) -> list[str]:
    return [
        symbol
        for symbol in CryptoAsset.objects.filter(user=user, is_active=True)
        .order_by('sort_order', 'symbol')
        .values_list('symbol', flat=True)
        if symbol.upper() not in USD_EQUIVALENTS
    ]


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_Q)


def _parse_decimal(raw) -> Decimal | None:
    if raw in (None, ''):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _latest_rate(symbol: str, as_of: date) -> Decimal | None:
    symbol = (symbol or '').upper()
    if symbol in USD_EQUIVALENTS:
        usd = (
            CurrencyRate.objects.filter(currency='USD', date__lte=as_of)
            .order_by('-date')
            .values_list('amount', flat=True)
            .first()
        )
        return Decimal(usd) if usd is not None else None
    row = (
        CurrencyRate.objects.filter(currency=symbol.upper(), date__lte=as_of)
        .order_by('-date')
        .values_list('amount', flat=True)
        .first()
    )
    return Decimal(row) if row is not None else None


def _price_usd_from_rub(symbol: str, as_of: date) -> Decimal | None:
    symbol = symbol.upper()
    if symbol in USD_EQUIVALENTS:
        return Decimal('1')
    coin_rub = _latest_rate(symbol, as_of)
    usd_rub = _latest_rate('USD', as_of)
    if coin_rub is None or usd_rub is None or usd_rub == 0:
        return None
    return coin_rub / usd_rub


def _parse_fee_usd(payload: dict, event: BybitExternalEvent | None = None) -> Decimal:
    payload = payload or {}
    for key in (
        'fee',
        'convertFee',
        'feeAmount',
        'exchangeFee',
        'totalFee',
        'execFee',
        'tradeFee',
        'withdrawFee',
        'txFee',
    ):
        value = _parse_decimal(payload.get(key))
        if value is not None and value > 0:
            fee = abs(value)
            fee_coin = str(payload.get('feeCoin') or payload.get('fee_coin') or payload.get('currency') or '').upper()
            if not fee_coin and event:
                fee_coin = (event.asset or '').upper()
            if fee_coin in USD_EQUIVALENTS or not fee_coin:
                return fee
            trade_price = _parse_decimal(payload.get('tradePrice'))
            if trade_price and trade_price > 0:
                return fee * trade_price
            return fee
    if event and event.fee_amount:
        return _parse_trade_fee_usd(event)
    return Decimal('0')


def _parse_trade_fee_usd(event: BybitExternalEvent) -> Decimal:
    payload = event.raw_payload or {}
    fee = _parse_decimal(payload.get('fee'))
    if (fee is None or fee <= 0) and event.fee_amount:
        fee = abs(event.fee_amount)
    if fee is None or fee <= 0:
        return Decimal('0')
    fee_coin = str(payload.get('feeCoin') or payload.get('currency') or event.asset or '').upper()
    if fee_coin in USD_EQUIVALENTS:
        return abs(fee)
    trade_price = _parse_decimal(payload.get('tradePrice'))
    if trade_price and trade_price > 0:
        return abs(fee) * trade_price
    return abs(fee)


def _build_extended_asset_map(user) -> dict[str, CryptoAsset]:
    asset_map = _build_asset_map(user)
    for alias, target in BYBIT_COIN_ALIASES.items():
        target_asset = asset_map.get(target)
        if target_asset is not None:
            asset_map[alias] = target_asset
    return asset_map


def _resolve_portfolio_asset(asset_map: dict[str, CryptoAsset], coin: str) -> CryptoAsset | None:
    coin = (coin or '').upper()
    if not coin or coin in USD_EQUIVALENTS:
        return None
    if coin in asset_map:
        return asset_map[coin]
    target = BYBIT_COIN_ALIASES.get(coin)
    if target:
        return asset_map.get(target)
    return None


def _wallet_balance_for_symbol(symbol: str, balances: dict[str, Decimal]) -> Decimal | None:
    symbol = symbol.upper()
    qty = balances.get(symbol)
    for alias, target in BYBIT_COIN_ALIASES.items():
        if target != symbol:
            continue
        alias_qty = balances.get(alias)
        if alias_qty is not None:
            qty = (qty or Decimal('0')) + alias_qty
    return qty


def _trade_symbol_from_event(event: BybitExternalEvent) -> str:
    payload = event.raw_payload or {}
    symbol = str(payload.get('symbol') or event.description or '').upper().strip()
    if split_stable_quote_pair(symbol):
        return symbol
    if symbol and symbol not in USD_EQUIVALENTS:
        return f'{symbol}USDT'
    return symbol


def _is_trade_event(event: BybitExternalEvent) -> bool:
    if event.stream != 'uta_translog':
        return False
    payload = event.raw_payload or {}
    desc = (event.description or '').upper()
    tx_type = str(payload.get('type') or payload.get('bizType') or '').upper()
    return desc == 'TRADE' or tx_type == 'TRADE'


def _effective_buy_price(quantity: Decimal, total_quote: Decimal, fee_quote: Decimal) -> Decimal:
    if quantity <= 0:
        return Decimal('0')
    return _quantize((total_quote + fee_quote) / quantity)


def ensure_legacy_manual_transactions(user) -> int:
    created = 0
    for spec in LEGACY_MANUAL_TRANSACTIONS:
        asset = CryptoAsset.objects.filter(user=user, symbol=spec['symbol'], is_active=True).first()
        if asset is None:
            continue
        quantity = spec['quantity']
        total_quote = spec['total_quote']
        fee_quote = spec['fee_quote']
        _, was_created = CryptoTransaction.objects.update_or_create(
            user=user,
            external_key=spec['external_key'],
            defaults={
                'asset': asset,
                'tx_type': CryptoTransaction.TxType.BUY,
                'occurred_at': timezone.datetime.fromisoformat(spec['occurred_at']).replace(tzinfo=timezone.utc),
                'quantity': quantity,
                'price_quote': _effective_buy_price(quantity, total_quote, fee_quote),
                'total_quote': total_quote,
                'fee_quote': fee_quote,
                'source': CryptoTransaction.Source.MANUAL,
                'note': spec['note'],
            },
        )
        if was_created:
            created += 1
    return created


def create_manual_transaction(
    user,
    *,
    asset: CryptoAsset,
    occurred_at,
    quantity: Decimal,
    price_quote: Decimal | None = None,
    total_quote: Decimal | None = None,
    fee_quote: Decimal = Decimal('0'),
    note: str = '',
) -> CryptoTransaction:
    if quantity <= 0:
        raise ValueError('Количество должно быть больше нуля.')
    fee_quote = fee_quote or Decimal('0')
    if total_quote is None and price_quote is not None:
        total_quote = quantity * price_quote
    if total_quote is None or total_quote <= 0:
        raise ValueError('Укажите цену или сумму покупки.')
    if price_quote is None:
        price_quote = total_quote / quantity
    effective_price = _effective_buy_price(quantity, total_quote, fee_quote)
    external_key = f'manual:{asset.symbol}:{occurred_at.isoformat()}:{quantity}'
    tx, _ = CryptoTransaction.objects.update_or_create(
        user=user,
        external_key=external_key,
        defaults={
            'asset': asset,
            'tx_type': CryptoTransaction.TxType.BUY,
            'occurred_at': occurred_at,
            'quantity': quantity,
            'price_quote': effective_price,
            'total_quote': _quantize(total_quote),
            'fee_quote': _quantize(fee_quote),
            'source': CryptoTransaction.Source.MANUAL,
            'note': note,
        },
    )
    return tx


def _build_asset_map(user) -> dict[str, CryptoAsset]:
    return {
        asset.symbol.upper(): asset
        for asset in CryptoAsset.objects.filter(user=user, is_active=True)
    }


def _tx_fingerprint(asset_symbol: str, occurred_at, quantity: Decimal) -> tuple:
    moment = occurred_at or timezone.now()
    minute = moment.replace(second=0, microsecond=0)
    return asset_symbol.upper(), minute, _quantize(quantity)


def _upsert_sell_tx(
    *,
    user,
    asset: CryptoAsset,
    occurred_at,
    quantity: Decimal,
    total_quote: Decimal,
    fee_quote: Decimal,
    source: str,
    external_key: str,
    bybit_event: BybitExternalEvent | None = None,
) -> tuple[CryptoTransaction, bool]:
    if quantity <= 0:
        return None, False
    net_quote = total_quote - (fee_quote or Decimal('0'))
    price_quote = _quantize(net_quote / quantity) if net_quote > 0 else Decimal('0')
    defaults = {
        'asset': asset,
        'tx_type': CryptoTransaction.TxType.SELL,
        'occurred_at': occurred_at,
        'quantity': quantity,
        'price_quote': price_quote,
        'total_quote': _quantize(total_quote),
        'fee_quote': _quantize(fee_quote or Decimal('0')),
        'source': source,
        'bybit_event': bybit_event,
    }
    tx, created = CryptoTransaction.objects.update_or_create(
        user=user,
        external_key=external_key,
        defaults=defaults,
    )
    return tx, created


def _upsert_buy_tx(
    *,
    user,
    asset: CryptoAsset,
    occurred_at,
    quantity: Decimal,
    total_quote: Decimal,
    fee_quote: Decimal,
    source: str,
    external_key: str,
    bybit_event: BybitExternalEvent | None = None,
) -> tuple[CryptoTransaction, bool]:
    if quantity <= 0 or total_quote <= 0:
        return None, False
    price_quote = _effective_buy_price(quantity, total_quote, fee_quote or Decimal('0'))
    defaults = {
        'asset': asset,
        'tx_type': CryptoTransaction.TxType.BUY,
        'occurred_at': occurred_at,
        'quantity': quantity,
        'price_quote': price_quote,
        'total_quote': _quantize(total_quote),
        'fee_quote': _quantize(fee_quote or Decimal('0')),
        'source': source,
        'bybit_event': bybit_event,
    }
    tx, created = CryptoTransaction.objects.update_or_create(
        user=user,
        external_key=external_key,
        defaults=defaults,
    )
    return tx, created


def _parse_convert_event(event: BybitExternalEvent, asset_map: dict[str, CryptoAsset]):
    payload = event.raw_payload or {}
    from_coin = str(payload.get('fromCoin') or '').upper()
    to_coin = str(payload.get('toCoin') or '').upper()
    if from_coin not in USD_EQUIVALENTS:
        return None
    asset = _resolve_portfolio_asset(asset_map, to_coin)
    if asset is None:
        return None

    to_amount = _parse_decimal(payload.get('toAmount'))
    from_amount = _parse_decimal(payload.get('fromAmount'))
    if to_amount is None or to_amount <= 0:
        return None
    if from_amount is None or from_amount <= 0:
        return None

    fee = _parse_fee_usd(payload, event)
    fee_coin = str(payload.get('feeCoin') or payload.get('fee_coin') or from_coin).upper()
    if fee_coin not in USD_EQUIVALENTS:
        trade_price = from_amount / to_amount if to_amount else None
        if trade_price:
            fee = fee * trade_price
        else:
            fee = Decimal('0')

    occurred_at = event.occurred_at or timezone.now()
    external_key = f'bybit:convert:{event.connection_id}:{event.external_id or event.id}'
    return {
        'asset': asset,
        'occurred_at': occurred_at,
        'quantity': to_amount,
        'total_quote': from_amount,
        'fee_quote': fee,
        'source': CryptoTransaction.Source.BYBIT_CONVERT,
        'external_key': external_key,
        'bybit_event': event,
    }


def _parse_convert_sell_event(event: BybitExternalEvent, asset_map: dict[str, CryptoAsset]):
    payload = event.raw_payload or {}
    from_coin = str(payload.get('fromCoin') or '').upper()
    to_coin = str(payload.get('toCoin') or '').upper()
    if to_coin not in USD_EQUIVALENTS:
        return None
    asset = _resolve_portfolio_asset(asset_map, from_coin)
    if asset is None:
        return None

    from_amount = _parse_decimal(payload.get('fromAmount'))
    to_amount = _parse_decimal(payload.get('toAmount'))
    if from_amount is None or from_amount <= 0:
        return None
    if to_amount is None or to_amount <= 0:
        return None

    fee = _parse_fee_usd(payload, event)
    fee_coin = str(payload.get('feeCoin') or payload.get('fee_coin') or to_coin).upper()
    if fee_coin not in USD_EQUIVALENTS:
        trade_price = to_amount / from_amount if from_amount else None
        if trade_price:
            fee = fee * trade_price
        else:
            fee = Decimal('0')

    occurred_at = event.occurred_at or timezone.now()
    external_key = f'bybit:convert:sell:{event.connection_id}:{event.external_id or event.id}'
    return {
        'asset': asset,
        'occurred_at': occurred_at,
        'quantity': from_amount,
        'total_quote': to_amount,
        'fee_quote': fee,
        'source': CryptoTransaction.Source.BYBIT_CONVERT,
        'external_key': external_key,
        'bybit_event': event,
    }


def _match_trade_token(
    usdt_event: BybitExternalEvent,
    token_events: list[BybitExternalEvent],
    asset_map: dict[str, CryptoAsset],
    *,
    used_tokens: set[int],
    sell: bool,
) -> BybitExternalEvent | None:
    symbol = _trade_symbol_from_event(usdt_event)
    if not symbol or not usdt_event.occurred_at:
        return None

    def is_pair(token_event: BybitExternalEvent) -> bool:
        if token_event.id in used_tokens:
            return False
        if _resolve_portfolio_asset(asset_map, (token_event.asset or '').upper()) is None:
            return False
        token_amount = token_event.amount or Decimal('0')
        usdt_amount = usdt_event.amount or Decimal('0')
        if sell:
            return token_amount < 0 and usdt_amount > 0
        return token_amount > 0 and usdt_amount < 0

    exact_key = (usdt_event.occurred_at, symbol)
    exact_matches = [
        event for event in token_events
        if (event.occurred_at, _trade_symbol_from_event(event)) == exact_key and is_pair(event)
    ]
    if exact_matches:
        return exact_matches[0]

    best = None
    best_gap = None
    for token_event in token_events:
        if not is_pair(token_event):
            continue
        if _trade_symbol_from_event(token_event) != symbol:
            continue
        if not token_event.occurred_at:
            continue
        gap = abs((token_event.occurred_at - usdt_event.occurred_at).total_seconds())
        if gap > TRADE_MATCH_SECONDS:
            continue
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = token_event
    return best


def _parse_trade_events(
    events: list[BybitExternalEvent],
    asset_map: dict[str, CryptoAsset],
    convert_fingerprints: set[tuple],
) -> list[dict]:
    trade_events = [event for event in events if _is_trade_event(event)]
    token_events = [
        event for event in trade_events
        if (event.asset or '').upper() not in USD_EQUIVALENTS
        and _resolve_portfolio_asset(asset_map, (event.asset or '').upper()) is not None
    ]
    quote_events = [
        event for event in trade_events
        if (event.asset or '').upper() in USD_EQUIVALENTS and event.amount not in (None, Decimal('0'))
    ]

    parsed = []
    used_tokens: set[int] = set()
    used_quotes: set[int] = set()

    for quote_event in quote_events:
        if quote_event.id in used_quotes:
            continue
        quote_amount = quote_event.amount or Decimal('0')
        sell = quote_amount > 0
        token_event = _match_trade_token(
            quote_event,
            token_events,
            asset_map,
            used_tokens=used_tokens,
            sell=sell,
        )
        if token_event is None:
            continue

        asset = _resolve_portfolio_asset(asset_map, (token_event.asset or '').upper())
        if asset is None:
            continue

        if not sell:
            fp = _tx_fingerprint(asset.symbol, token_event.occurred_at or quote_event.occurred_at, token_event.amount)
            if fp in convert_fingerprints:
                continue

        used_tokens.add(token_event.id)
        used_quotes.add(quote_event.id)
        qty = abs(token_event.amount)
        quote = abs(quote_amount)
        fee = _parse_trade_fee_usd(quote_event) + _parse_trade_fee_usd(token_event)
        occurred_at = token_event.occurred_at or quote_event.occurred_at or timezone.now()

        if sell:
            parsed.append({
                'kind': 'sell',
                'asset': asset,
                'occurred_at': occurred_at,
                'quantity': qty,
                'total_quote': quote,
                'fee_quote': fee,
                'source': CryptoTransaction.Source.BYBIT_TRADE,
                'external_key': f'bybit:sell:{quote_event.connection_id}:{quote_event.external_id}:{token_event.external_id}',
                'bybit_event': token_event,
            })
        else:
            parsed.append({
                'kind': 'buy',
                'asset': asset,
                'occurred_at': occurred_at,
                'quantity': qty,
                'total_quote': quote,
                'fee_quote': fee,
                'source': CryptoTransaction.Source.BYBIT_TRADE,
                'external_key': f'bybit:trade:{quote_event.connection_id}:{quote_event.external_id}:{token_event.external_id}',
                'bybit_event': token_event,
            })
    return parsed


def sync_wallet_balances_from_bybit(user, connection: BybitConnection | None = None) -> dict:
    settings_obj = get_or_create_settings(user)
    connection = connection or settings_obj.bybit_connection
    if connection is None:
        connection = BybitConnection.objects.filter(user=user, is_active=True).order_by('-updated_at').first()
    if connection is None:
        return {'updated': 0, 'failed': ['Нет активного подключения Bybit']}

    client = BybitV5Client(
        api_key=connection.api_key,
        api_secret=connection.api_secret,
        is_testnet=connection.is_testnet,
    )
    try:
        payload = client.fetch_wallet_balance('UNIFIED')
    except Exception as exc:
        return {'updated': 0, 'failed': [str(exc)]}

    balances: dict[str, Decimal] = {}
    for account in payload.get('result', {}).get('list', []) or []:
        for coin in account.get('coin', []) or []:
            symbol = str(coin.get('coin') or '').upper()
            raw_qty = coin.get('walletBalance') or coin.get('equity') or coin.get('availableToWithdraw')
            if not symbol or raw_qty in (None, ''):
                continue
            try:
                qty = Decimal(str(raw_qty))
            except InvalidOperation:
                continue
            if qty > 0:
                balances[symbol] = qty

    asset_map = _build_extended_asset_map(user)
    now = timezone.now()
    updated = 0
    for symbol, asset in asset_map.items():
        if symbol in BYBIT_COIN_ALIASES or symbol in USD_EQUIVALENTS:
            continue
        qty = _wallet_balance_for_symbol(symbol, balances)
        if qty is None:
            asset.wallet_quantity = Decimal('0')
        else:
            asset.wallet_quantity = qty
        asset.wallet_updated_at = now
        asset.save(update_fields=['wallet_quantity', 'wallet_updated_at', 'updated_at'])
        updated += 1

    settings_obj.bybit_connection = connection
    settings_obj.save(update_fields=['bybit_connection', 'updated_at'])
    return {'updated': updated, 'balances': {k: str(v) for k, v in balances.items()}, 'failed': []}


def sync_transactions_from_bybit(user, connection: BybitConnection | None = None, *, resync_days: int = 3650) -> dict:
    settings_obj = get_or_create_settings(user)
    connection = connection or settings_obj.bybit_connection
    if connection is None:
        connection = BybitConnection.objects.filter(user=user, is_active=True).order_by('-updated_at').first()
    if connection is None:
        return {'inserted': 0, 'updated': 0, 'skipped': 0, 'failed': ['Нет активного подключения Bybit']}

    end_dt = timezone.now()
    start_dt = end_dt - timedelta(days=max(1, resync_days))
    sync_result = sync_bybit_transaction_log(
        connection=connection,
        start_dt=start_dt,
        end_dt=end_dt,
        streams=['convert_history', 'uta_translog'],
    )
    _run, _stats = sync_result

    asset_map = _build_extended_asset_map(user)
    if not asset_map:
        ensure_default_assets(user)
        asset_map = _build_extended_asset_map(user)

    events = BybitExternalEvent.objects.filter(
        connection=connection,
        stream__in=['convert_history', 'uta_translog'],
    ).order_by('occurred_at', 'id')

    convert_events = [event for event in events if event.stream == 'convert_history']
    trade_events = [event for event in events if event.stream == 'uta_translog']

    result = {'inserted': 0, 'updated': 0, 'skipped': 0, 'failed': []}
    convert_fingerprints: set[tuple] = set()
    convert_parsed: list[dict] = []
    convert_sells: list[dict] = []

    with db_transaction.atomic():
        for event in convert_events:
            parsed = _parse_convert_event(event, asset_map)
            if parsed is not None:
                convert_parsed.append(parsed)
                convert_fingerprints.add(
                    _tx_fingerprint(parsed['asset'].symbol, parsed['occurred_at'], parsed['quantity'])
                )
                continue
            sell_parsed = _parse_convert_sell_event(event, asset_map)
            if sell_parsed is not None:
                convert_sells.append(sell_parsed)
            else:
                result['skipped'] += 1

        for parsed in convert_parsed:
            tx, created = _upsert_buy_tx(user=user, **parsed)
            if tx is None:
                result['skipped'] += 1
                continue
            if created:
                result['inserted'] += 1
            else:
                result['updated'] += 1

        for parsed in convert_sells:
            tx, created = _upsert_sell_tx(user=user, **parsed)
            if tx is None:
                result['skipped'] += 1
                continue
            if created:
                result['inserted'] += 1
            else:
                result['updated'] += 1

        for parsed in _parse_trade_events(trade_events, asset_map, convert_fingerprints):
            upsert = _upsert_sell_tx if parsed.pop('kind') == 'sell' else _upsert_buy_tx
            tx, created = upsert(user=user, **parsed)
            if tx is None:
                result['skipped'] += 1
                continue
            if created:
                result['inserted'] += 1
            else:
                result['updated'] += 1

        settings_obj.bybit_connection = connection
        settings_obj.last_tx_sync_at = timezone.now()
        settings_obj.save(update_fields=['bybit_connection', 'last_tx_sync_at', 'updated_at'])

    sync_wallet_balances_from_bybit(user, connection)
    return result


def sync_crypto_rates(user, rate_date: date | None = None) -> dict:
    symbols = active_asset_symbols(user)
    settings_obj = get_or_create_settings(user)
    now = timezone.now()
    result = {'inserted': 0, 'updated': 0, 'synced': [], 'skipped': [], 'failed': [], 'source': 'bybit', 'quotes': {}}

    try:
        bybit_quotes = fetch_bybit_spot_prices(symbols)
        result['quotes'] = bybit_quotes
        for asset in CryptoAsset.objects.filter(user=user, is_active=True):
            if asset.symbol.upper() in USD_EQUIVALENTS:
                continue
            quote = bybit_quotes.get(asset.symbol.upper())
            if not quote:
                result['skipped'].append(asset.symbol)
                continue
            asset.last_price_usd = quote['price_usd']
            asset.change_24h_pct = quote.get('change_24h_pct')
            asset.price_updated_at = now
            asset.save(update_fields=['last_price_usd', 'change_24h_pct', 'price_updated_at', 'updated_at'])
            result['synced'].append(asset.symbol)

            rate_date = rate_date or timezone.localdate()
            usd_rub = _latest_rate('USD', rate_date)
            if usd_rub:
                _, created = CurrencyRate.objects.update_or_create(
                    date=rate_date,
                    currency=asset.symbol.upper(),
                    defaults={'amount': quote['price_usd'] * usd_rub},
                )
                if created:
                    result['inserted'] += 1
                else:
                    result['updated'] += 1
    except RuntimeError as exc:
        result['failed'].append(str(exc))
        fallback = sync_crypto_rates_incremental(
            symbols,
            end_date=rate_date,
            api_key=settings_obj.cmc_api_key,
        )
        result.update(fallback)
        result['source'] = fallback.get('source') or 'fallback'
        for asset in CryptoAsset.objects.filter(user=user, symbol__in=fallback.get('synced', [])):
            quote = (fallback.get('quotes') or {}).get(asset.symbol.upper())
            if not quote:
                continue
            asset.last_price_usd = quote['price_usd']
            asset.change_24h_pct = quote.get('change_24h_pct')
            asset.price_updated_at = now
            asset.save(update_fields=['last_price_usd', 'change_24h_pct', 'price_updated_at', 'updated_at'])

    settings_obj.last_rates_sync_at = now
    settings_obj.save(update_fields=['last_rates_sync_at', 'updated_at'])
    return result


def _compute_asset_summary(asset: CryptoAsset, txs: list[CryptoTransaction], as_of: date, total_value: Decimal) -> AssetSummary | None:
    remaining_qty = Decimal('0')
    remaining_cost = Decimal('0')
    total_bought = Decimal('0')

    for tx in sorted(txs, key=lambda row: (row.occurred_at, row.id)):
        if tx.tx_type == CryptoTransaction.TxType.BUY:
            buy_cost = tx.total_quote + tx.fee_quote
            remaining_cost += buy_cost
            remaining_qty += tx.quantity
            total_bought += tx.quantity
        elif tx.tx_type == CryptoTransaction.TxType.SELL and remaining_qty > 0:
            avg_cost = remaining_cost / remaining_qty
            sold_cost = avg_cost * tx.quantity
            remaining_cost -= sold_cost
            remaining_qty -= tx.quantity

    ledger_quantity = remaining_qty
    quantity = asset.wallet_quantity if asset.wallet_quantity is not None else ledger_quantity

    if quantity is None or quantity <= 0:
        return None

    if remaining_qty > 0:
        avg_buy = remaining_cost / remaining_qty
    elif total_bought > 0:
        avg_buy = sum(
            (tx.total_quote + tx.fee_quote for tx in txs if tx.tx_type == CryptoTransaction.TxType.BUY),
            Decimal('0'),
        ) / total_bought
    else:
        avg_buy = Decimal('0')

    cost_basis = avg_buy * quantity
    current_price = asset.last_price_usd or _price_usd_from_rub(asset.symbol, as_of) or Decimal('0')
    current_value = quantity * current_price
    profit_abs = current_value - cost_basis
    profit_pct = (profit_abs / cost_basis * Decimal('100')) if cost_basis else None
    allocation_pct = (current_value / total_value * Decimal('100')) if total_value else None

    return AssetSummary(
        asset=asset,
        symbol=asset.symbol,
        quantity=quantity,
        avg_buy_price=_quantize(avg_buy),
        cost_basis=_quantize(cost_basis),
        current_price=current_price,
        current_value=_quantize(current_value),
        change_24h_pct=asset.change_24h_pct,
        profit_abs=_quantize(profit_abs),
        profit_pct=profit_pct,
        allocation_pct=allocation_pct,
        ledger_quantity=ledger_quantity if ledger_quantity else None,
    )


def get_portfolio_summary(user, as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or timezone.localdate()
    assets = [
        asset
        for asset in CryptoAsset.objects.filter(user=user, is_active=True).order_by('sort_order', 'symbol')
        if asset.symbol.upper() not in USD_EQUIVALENTS
    ]
    txs_by_asset: dict[int, list[CryptoTransaction]] = {}
    for tx in CryptoTransaction.objects.filter(user=user, asset__in=assets).select_related('asset'):
        txs_by_asset.setdefault(tx.asset_id, []).append(tx)

    summaries: list[AssetSummary] = []
    for asset in assets:
        summary = _compute_asset_summary(asset, txs_by_asset.get(asset.id, []), as_of, Decimal('0'))
        if summary:
            summaries.append(summary)

    total_value = sum((item.current_value for item in summaries), Decimal('0'))
    total_cost = sum((item.cost_basis for item in summaries), Decimal('0'))
    total_profit = total_value - total_cost
    total_profit_pct = (total_profit / total_cost * Decimal('100')) if total_cost else None

    for item in summaries:
        item.allocation_pct = (item.current_value / total_value * Decimal('100')) if total_value else Decimal('0')

    prev_as_of = as_of - timedelta(days=1)
    change_24h = Decimal('0')
    has_change = False
    for item in summaries:
        if item.change_24h_pct is not None:
            has_change = True
            change_24h += item.current_value * item.change_24h_pct / Decimal('100')
    if not has_change:
        prev_value = Decimal('0')
        for item in summaries:
            prev_price = _price_usd_from_rub(item.symbol, prev_as_of) or item.current_price
            prev_value += item.quantity * prev_price
        change_24h = total_value - prev_value
        change_24h_pct = (change_24h / prev_value * Decimal('100')) if prev_value else None
    else:
        change_24h_pct = (change_24h / total_value * Decimal('100')) if total_value else None

    best = max(summaries, key=lambda row: row.profit_pct or Decimal('-999999'), default=None)
    worst = min(summaries, key=lambda row: row.profit_pct or Decimal('999999'), default=None)

    return {
        'as_of': as_of,
        'total_value': total_value,
        'total_cost': total_cost,
        'total_profit': total_profit,
        'total_profit_pct': total_profit_pct,
        'change_24h': change_24h,
        'change_24h_pct': change_24h_pct,
        'assets': summaries,
        'best': best,
        'worst': worst,
    }


def get_asset_detail(user, symbol: str, as_of: date | None = None) -> dict[str, Any] | None:
    as_of = as_of or timezone.localdate()
    asset = CryptoAsset.objects.filter(user=user, symbol__iexact=symbol, is_active=True).first()
    if asset is None:
        return None
    txs = list(
        CryptoTransaction.objects.filter(user=user, asset=asset)
        .order_by('-occurred_at', '-id')
    )
    portfolio = get_portfolio_summary(user, as_of)
    asset_summary = next((row for row in portfolio['assets'] if row.asset.id == asset.id), None)
    return {
        'asset': asset,
        'summary': asset_summary,
        'transactions': txs,
        'portfolio_total': portfolio['total_value'],
    }
