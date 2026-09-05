from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from core.models import Currency, CurrencyRate


CBR_FIAT_CODES = {
    'RUB', 'USD', 'EUR', 'KZT', 'KGS', 'GBP', 'CHF', 'JPY', 'CNY',
    'UAH', 'BYN', 'CAD', 'AUD', 'NOK', 'SEK', 'TRY', 'AED', 'HKD',
    'SGD', 'THB', 'VND', 'INR', 'IDR', 'MYR', 'PHP', 'USDT', 'USDC',
    'AMD', 'KRW', 'GEL', 'AZN', 'BRL', 'MXN', 'PLN', 'CZK', 'HUF',
}

CRYPTOCOMPARE_HISTODAY_URL = 'https://min-api.cryptocompare.com/data/v2/histoday'
BINANCE_KLINES_URL = 'https://api.binance.com/api/v3/klines'

CMC_API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
COINGECKO_API_URL = 'https://api.coingecko.com/api/v3/simple/price'
COINGECKO_CHART_RANGE_URL = 'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range'

COINGECKO_IDS = {
    'BTC': 'bitcoin',
    'ETH': 'ethereum',
    'SOL': 'solana',
    'TWT': 'trust-wallet-token',
    'GRAM': 'gram-2',
    'XRP': 'ripple',
    'MNT': 'mantle',
    'DOGE': 'dogecoin',
}

CRYPTO_NAMES = {
    'BTC': 'Bitcoin',
    'ETH': 'Ethereum',
    'SOL': 'Solana',
    'TWT': 'Trust Wallet Token',
    'GRAM': 'Gram',
    'XRP': 'XRP',
    'MNT': 'Mantle',
    'DOGE': 'Dogecoin',
}


def portfolio_crypto_symbols(symbols: list[str] | None = None) -> list[str]:
    """Keep only tickers we know how to price (portfolio / coingecko map)."""
    known = set(COINGECKO_IDS.keys())
    if symbols:
        return sorted({symbol.upper() for symbol in symbols if symbol.upper() in known})
    return sorted(known)


def _cryptocompare_headers() -> dict[str, str]:
    api_key = getattr(settings, 'CRYPTOCOMPARE_API_KEY', '')
    if api_key:
        return {'authorization': f'Apikey {api_key.strip()}'}
    return {}


def _fetch_json(url: str, headers: dict | None = None) -> dict:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {body[:240]}') from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'API request failed: {exc}') from exc


def _fetch_json_array(url: str) -> list:
    request = Request(url)
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {body[:240]}') from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'API request failed: {exc}') from exc
    if isinstance(payload, dict):
        raise RuntimeError(payload.get('msg') or str(payload))
    return payload


def _usd_rub_on_date(rate_date: date) -> Decimal | None:
    usd_row = (
        CurrencyRate.objects.filter(currency='USD', date__lte=rate_date)
        .order_by('-date')
        .values_list('amount', flat=True)
        .first()
    )
    return Decimal(usd_row) if usd_row is not None else None


def ensure_crypto_currencies(symbols: list[str]) -> None:
    for symbol in symbols:
        code = symbol.upper()
        Currency.objects.get_or_create(
            code=code,
            defaults={'name': CRYPTO_NAMES.get(code, code), 'status': 'active'},
        )


def fetch_prices_usd_cmc(symbols: list[str], api_key: str) -> dict[str, dict[str, Decimal]]:
    if not symbols or not api_key.strip():
        return {}
    payload = _fetch_json(
        f'{CMC_API_URL}?{urlencode({"symbol": ",".join(symbols), "convert": "USD"})}',
        headers={
            'Accepts': 'application/json',
            'X-CMC_PRO_API_KEY': api_key.strip(),
        },
    )
    quotes: dict[str, dict[str, Decimal]] = {}
    for symbol, item in (payload.get('data') or {}).items():
        usd = (item.get('quote') or {}).get('USD') or {}
        price = usd.get('price')
        if price in (None, ''):
            continue
        try:
            quotes[symbol.upper()] = {
                'price_usd': Decimal(str(price)),
                'change_24h_pct': Decimal(str(usd.get('percent_change_24h') or '0')),
            }
        except InvalidOperation:
            continue
    return quotes


def fetch_prices_usd_coingecko(symbols: list[str]) -> dict[str, dict[str, Decimal]]:
    ids = []
    id_to_symbol = {}
    for symbol in symbols:
        code = symbol.upper()
        coin_id = COINGECKO_IDS.get(code)
        if not coin_id:
            continue
        ids.append(coin_id)
        id_to_symbol[coin_id] = code
    if not ids:
        return {}

    params = urlencode({
        'ids': ','.join(ids),
        'vs_currencies': 'usd',
        'include_24hr_change': 'true',
    })
    payload = _fetch_json(f'{COINGECKO_API_URL}?{params}')
    quotes: dict[str, dict[str, Decimal]] = {}
    for coin_id, values in payload.items():
        symbol = id_to_symbol.get(coin_id)
        if not symbol:
            continue
        price = values.get('usd')
        if price in (None, ''):
            continue
        try:
            quotes[symbol] = {
                'price_usd': Decimal(str(price)),
                'change_24h_pct': Decimal(str(values.get('usd_24h_change') or '0')),
            }
        except InvalidOperation:
            continue
    return quotes


def fetch_crypto_quotes(symbols: list[str], api_key: str | None = None) -> tuple[dict[str, dict[str, Decimal]], str]:
    normalized = sorted({symbol.upper() for symbol in symbols if symbol})
    api_key = (api_key or '').strip() or getattr(settings, 'COINMARKETCAP_API_KEY', '')
    if api_key:
        try:
            quotes = fetch_prices_usd_cmc(normalized, api_key)
            if quotes:
                return quotes, 'coinmarketcap'
        except RuntimeError:
            pass
    quotes = fetch_prices_usd_coingecko(normalized)
    if quotes:
        return quotes, 'coingecko'
    if api_key:
        raise RuntimeError('CoinMarketCap и CoinGecko не вернули курсы. Проверьте API key и список монет.')
    raise RuntimeError('CoinGecko недоступен. Добавьте COINMARKETCAP_API_KEY в .env или в настройках портфеля.')


def sync_crypto_rates(symbols: list[str], rate_date: date | None = None, api_key: str | None = None) -> dict:
    rate_date = rate_date or timezone.localdate()
    result = {
        'inserted': 0,
        'updated': 0,
        'synced': [],
        'skipped': [],
        'failed': [],
        'source': '',
        'quotes': {},
    }

    normalized = sorted({symbol.upper() for symbol in symbols if symbol})
    if not normalized:
        return result

    usd_rub = _usd_rub_on_date(rate_date)
    if usd_rub is None:
        result['failed'].append('USD: нет курса ЦБ для конвертации в RUB')
        return result

    ensure_crypto_currencies(normalized)

    try:
        quotes, source = fetch_crypto_quotes(normalized, api_key=api_key)
    except RuntimeError as exc:
        result['failed'].append(str(exc))
        return result

    result['source'] = source
    result['quotes'] = quotes

    for symbol in normalized:
        quote = quotes.get(symbol)
        if not quote:
            result['skipped'].append(symbol)
            continue
        rub_amount = quote['price_usd'] * usd_rub
        _, created = CurrencyRate.objects.update_or_create(
            date=rate_date,
            currency=symbol,
            defaults={'amount': rub_amount},
        )
        if created:
            result['inserted'] += 1
        else:
            result['updated'] += 1
        result['synced'].append(symbol)

    return result


def sync_crypto_rates_incremental(symbols: list[str], end_date: date | None = None, api_key: str | None = None) -> dict:
    end_date = end_date or timezone.localdate()
    return sync_crypto_rates(symbols=symbols, rate_date=end_date, api_key=api_key)


def _unix_utc(day: date, *, end_of_day: bool = False) -> int:
    moment = dt_time.max if end_of_day else dt_time.min
    return int(datetime.combine(day, moment, tzinfo=timezone.utc).timestamp())


def fetch_daily_prices_usd_coingecko(coin_id: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    """CoinGecko free tier: max ~365 days per request — use chunked windows."""
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    by_date: dict[date, Decimal] = {}
    chunk_end = end_date
    while chunk_end >= start_date:
        chunk_start = max(start_date, chunk_end - timedelta(days=364))
        url = COINGECKO_CHART_RANGE_URL.format(coin_id=coin_id)
        params = urlencode({
            'vs_currency': 'usd',
            'from': str(_unix_utc(chunk_start)),
            'to': str(_unix_utc(chunk_end, end_of_day=True)),
        })
        payload = _fetch_json(f'{url}?{params}')
        for point in payload.get('prices') or []:
            if not point or len(point) < 2:
                continue
            try:
                rate_date = datetime.fromtimestamp(point[0] / 1000, tz=timezone.utc).date()
                by_date[rate_date] = Decimal(str(point[1]))
            except (InvalidOperation, ValueError, TypeError, OverflowError):
                continue
        if chunk_start <= start_date:
            break
        chunk_end = chunk_start - timedelta(days=1)
        time.sleep(1.2)
    return by_date


def fetch_daily_prices_usd_binance(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    """Daily close from Binance spot klines (free, deep history)."""
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    pair = f'{symbol.upper()}USDT'
    by_date: dict[date, Decimal] = {}
    start_ms = _unix_utc(start_date) * 1000
    end_ms = _unix_utc(end_date, end_of_day=True) * 1000
    current_start = start_ms

    while current_start <= end_ms:
        params = urlencode({
            'symbol': pair,
            'interval': '1d',
            'startTime': str(current_start),
            'endTime': str(end_ms),
            'limit': '1000',
        })
        rows = _fetch_json_array(f'{BINANCE_KLINES_URL}?{params}')
        if not rows:
            break
        for row in rows:
            if not row or len(row) < 5:
                continue
            try:
                open_time_ms = int(row[0])
                rate_date = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date()
                if rate_date < start_date or rate_date > end_date:
                    continue
                by_date[rate_date] = Decimal(str(row[4]))
            except (InvalidOperation, ValueError, TypeError, OverflowError):
                continue
        last_open_time = int(rows[-1][0])
        next_start = last_open_time + 86_400_000
        if next_start <= current_start:
            break
        current_start = next_start
        time.sleep(0.15)

    return by_date


def fetch_daily_prices_usd_cryptocompare(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    """Daily USD close prices; paginates in 2000-day chunks (free tier friendly)."""
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    by_date: dict[date, Decimal] = {}
    start_ts = _unix_utc(start_date)
    to_ts = _unix_utc(end_date, end_of_day=True)
    headers = _cryptocompare_headers()

    while to_ts >= start_ts:
        params = urlencode({
            'fsym': symbol.upper(),
            'tsym': 'USD',
            'limit': 2000,
            'toTs': str(to_ts),
        })
        payload = _fetch_json(f'{CRYPTOCOMPARE_HISTODAY_URL}?{params}', headers)
        if payload.get('Response') != 'Success':
            message = payload.get('Message') or str(payload.get('Data') or 'CryptoCompare error')
            raise RuntimeError(message)

        rows = (payload.get('Data') or {}).get('Data') or []
        if not rows:
            break

        oldest_ts = None
        for row in rows:
            ts = row.get('time')
            close = row.get('close')
            if ts is None or close in (None, ''):
                continue
            rate_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
            if rate_date < start_date or rate_date > end_date:
                continue
            by_date[rate_date] = Decimal(str(close))
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = int(ts)

        if oldest_ts is None or oldest_ts <= start_ts:
            break
        to_ts = oldest_ts - 1
        time.sleep(0.35)

    return by_date


def sync_crypto_rates_historical(
    symbols: list[str],
    start_date: date,
    end_date: date | None = None,
    *,
    request_delay_sec: float = 0.5,
) -> dict:
    """
    Backfill daily crypto prices into CurrencyRate (RUB per coin).
    Uses Binance daily klines (free, history since listing).
    CryptoCompare — если задан CRYPTOCOMPARE_API_KEY.
    Requires USD/RUB from CBR for each date in the range.
    """
    end_date = end_date or timezone.localdate()
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    result = {
        'inserted': 0,
        'updated': 0,
        'synced': [],
        'skipped': [],
        'failed': [],
        'source': 'binance-historical',
        'days_written': 0,
        'days_skipped_no_usd': 0,
    }

    normalized = portfolio_crypto_symbols(symbols)
    if not normalized:
        result['failed'].append('Нет известных криптотикеров для загрузки (BTC, ETH, …).')
        return result

    ensure_crypto_currencies(normalized)
    use_cryptocompare = bool(getattr(settings, 'CRYPTOCOMPARE_API_KEY', ''))

    usd_available = CurrencyRate.objects.filter(
        currency='USD',
        date__gte=start_date,
        date__lte=end_date,
    ).exists()
    if not usd_available:
        result['failed'].append(
            f'USD: нет курсов ЦБ с {start_date.isoformat()}. Сначала загрузите USD через «Первичная загрузка» на этой странице.'
        )
        return result

    for index, symbol in enumerate(normalized):
        if index > 0 and request_delay_sec:
            time.sleep(request_delay_sec)
        fetchers = []
        if use_cryptocompare:
            fetchers.append(('cryptocompare', fetch_daily_prices_usd_cryptocompare))
        fetchers.append(('binance', fetch_daily_prices_usd_binance))

        daily_prices: dict[date, Decimal] = {}
        errors = []
        for source_name, fetcher in fetchers:
            try:
                daily_prices = fetcher(symbol, start_date, end_date)
            except RuntimeError as exc:
                errors.append(f'{source_name}: {exc}')
                continue
            if daily_prices:
                if source_name == 'cryptocompare':
                    result['source'] = 'cryptocompare-historical'
                break

        if not daily_prices:
            result['failed'].append(f'{symbol}: {"; ".join(errors) or "нет данных"}')
            continue

        symbol_days = _write_daily_prices(symbol, daily_prices, start_date, end_date, result)
        result['days_written'] += symbol_days
        result['synced'].append(f'{symbol} ({symbol_days} дн.)')

    return result


def _write_daily_prices(
    symbol: str,
    daily_prices: dict[date, Decimal],
    start_date: date,
    end_date: date,
    result: dict,
) -> int:
    symbol_days = 0
    for rate_date, price_usd in sorted(daily_prices.items()):
        if rate_date < start_date or rate_date > end_date:
            continue
        usd_rub = _usd_rub_on_date(rate_date)
        if usd_rub is None:
            result['days_skipped_no_usd'] += 1
            continue
        _, created = CurrencyRate.objects.update_or_create(
            date=rate_date,
            currency=symbol,
            defaults={'amount': price_usd * usd_rub},
        )
        if created:
            result['inserted'] += 1
        else:
            result['updated'] += 1
        symbol_days += 1
    return symbol_days

