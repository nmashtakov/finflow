from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import urlopen

BYBIT_PUBLIC_URL = 'https://api.bybit.com'


def fetch_bybit_spot_prices(symbols: list[str]) -> dict[str, dict[str, Decimal]]:
    """Last spot price and 24h change from Bybit public tickers."""
    if not symbols:
        return {}
    normalized = sorted({symbol.upper() for symbol in symbols if symbol})
    pair_by_symbol = {symbol: f'{symbol}USDT' for symbol in normalized}
    quotes: dict[str, dict[str, Decimal]] = {}

    chunk_size = 10
    symbols_list = list(normalized)
    for index in range(0, len(symbols_list), chunk_size):
        chunk = symbols_list[index:index + chunk_size]
        pairs = [pair_by_symbol[symbol] for symbol in chunk]
        query = urlencode({'category': 'spot', 'symbol': ','.join(pairs)})
        with urlopen(f'{BYBIT_PUBLIC_URL}/v5/market/tickers?{query}', timeout=30) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if payload.get('retCode') != 0:
            raise RuntimeError(payload.get('retMsg') or 'Bybit tickers error')
        for item in payload.get('result', {}).get('list', []) or []:
            pair = str(item.get('symbol') or '').upper()
            symbol = next((code for code, pair_code in pair_by_symbol.items() if pair_code == pair), None)
            if not symbol:
                continue
            last_price = item.get('lastPrice')
            if last_price in (None, ''):
                continue
            try:
                quotes[symbol] = {
                    'price_usd': Decimal(str(last_price)),
                    'change_24h_pct': Decimal(str(item.get('price24hPcnt') or '0')) * Decimal('100'),
                }
            except InvalidOperation:
                continue

    missing = [symbol for symbol in normalized if symbol not in quotes]
    if missing:
        fallback_pairs = {}
        for symbol in missing:
            if symbol == 'GRAM':
                fallback_pairs[symbol] = 'TONUSDT'
        if fallback_pairs:
            query = urlencode({'category': 'spot', 'symbol': ','.join(fallback_pairs.values())})
            with urlopen(f'{BYBIT_PUBLIC_URL}/v5/market/tickers?{query}', timeout=30) as response:
                payload = json.loads(response.read().decode('utf-8'))
            for item in payload.get('result', {}).get('list', []) or []:
                pair = str(item.get('symbol') or '').upper()
                symbol = next((code for code, pair_code in fallback_pairs.items() if pair_code == pair), None)
                if not symbol or symbol in quotes:
                    continue
                last_price = item.get('lastPrice')
                if last_price in (None, ''):
                    continue
                try:
                    quotes[symbol] = {
                        'price_usd': Decimal(str(last_price)),
                        'change_24h_pct': Decimal(str(item.get('price24hPcnt') or '0')) * Decimal('100'),
                    }
                except InvalidOperation:
                    continue
    return quotes
