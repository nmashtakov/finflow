USD_STABLECOINS = frozenset({"USDT", "USDC", "USDE"})
USD_STABLE_QUOTES = ("USDT", "USDC", "USDE", "USD")


def normalize_currency_code(code) -> str:
    return str(code or "").strip().upper()


def is_usd_stablecoin(code) -> bool:
    return normalize_currency_code(code) in USD_STABLECOINS


def fx_currency_code(code) -> str:
    normalized = normalize_currency_code(code)
    if is_usd_stablecoin(normalized):
        return "USD"
    return normalized


def currencies_need_usd_rate(codes) -> bool:
    return any(is_usd_stablecoin(code) or normalize_currency_code(code) == "USD" for code in (codes or []))


def split_stable_quote_pair(symbol: str):
    text = normalize_currency_code(symbol)
    for quote in USD_STABLE_QUOTES:
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)], quote
    return None
