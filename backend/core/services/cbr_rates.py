from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import socket
import time
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from xml.etree import ElementTree as ET

from core.models import CurrencyRate


CBR_CURRENCY_LIST_URL = "https://www.cbr.ru/scripts/XML_valFull.asp"
CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"


def _fetch_xml(url, params=None, *, timeout=10, attempts=2):
    final_url = url
    if params:
        final_url = f"{url}?{urlencode(params)}"

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(final_url, timeout=timeout) as response:
                return ET.fromstring(response.read())
        except (HTTPError, URLError, TimeoutError, socket.timeout, ET.ParseError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(attempt)
    raise RuntimeError(f"ЦБ РФ не ответил после {attempts} попыток: {last_error}")


def _fetch_cbr_code_map():
    root = _fetch_xml(CBR_CURRENCY_LIST_URL)
    code_map = {}
    for item in root.findall("Item"):
        char_code = (item.findtext("ISO_Char_Code") or "").strip().upper()
        item_id = item.attrib.get("ID", "").strip()
        if char_code and item_id:
            code_map[char_code] = item_id
    return code_map


def _daterange(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _sync_rub_rates(start_date, end_date):
    inserted = 0
    updated = 0
    for current_date in _daterange(start_date, end_date):
        _, created = CurrencyRate.objects.update_or_create(
            date=current_date,
            currency="RUB",
            defaults={"amount": Decimal("1")},
        )
        if created:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def _sync_usdt_from_usd(start_date, end_date):
    inserted = 0
    updated = 0
    usd_dates = set(
        CurrencyRate.objects.filter(
            currency="USD",
            date__range=(start_date, end_date),
        ).values_list("date", flat=True)
    )
    CurrencyRate.objects.filter(
        currency="USDT",
        date__range=(start_date, end_date),
    ).exclude(date__in=usd_dates).delete()
    usd_rows = CurrencyRate.objects.filter(
        currency="USD",
        date__range=(start_date, end_date),
    ).values_list("date", "amount")
    for rate_date, amount in usd_rows:
        _, created = CurrencyRate.objects.update_or_create(
            date=rate_date,
            currency="USDT",
            defaults={"amount": amount},
        )
        if created:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def _fill_missing_daily_rates(currency_code, start_date, end_date):
    inserted = 0
    updated = 0
    rows = list(
        CurrencyRate.objects.filter(currency=currency_code, date__lte=end_date)
        .order_by("date")
        .values_list("date", "amount")
    )
    if not rows:
        return inserted, updated

    known_by_date = {rate_date: amount for rate_date, amount in rows}
    current_amount = None
    for rate_date, amount in rows:
        if rate_date < start_date:
            current_amount = amount
            continue
        break

    for current_date in _daterange(start_date, end_date):
        if current_date in known_by_date:
            current_amount = known_by_date[current_date]
            continue
        if current_amount is None:
            continue
        _, created = CurrencyRate.objects.update_or_create(
            date=current_date,
            currency=currency_code,
            defaults={"amount": current_amount},
        )
        if created:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def _sync_cbr_currency_rates(currency_code, cbr_code, start_date, end_date):
    root = _fetch_xml(
        CBR_DYNAMIC_URL,
        params={
            "date_req1": start_date.strftime("%d/%m/%Y"),
            "date_req2": end_date.strftime("%d/%m/%Y"),
            "VAL_NM_RQ": cbr_code,
        },
    )

    inserted = 0
    updated = 0
    for record in root.findall("Record"):
        raw_date = (record.attrib.get("Date") or "").strip()
        nominal = (record.findtext("Nominal") or "").strip()
        value = (record.findtext("Value") or "").strip()
        if not raw_date or not nominal or not value:
            continue
        try:
            rate_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
        except ValueError:
            continue
        try:
            nominal_dec = Decimal(nominal.replace(",", "."))
            value_dec = Decimal(value.replace(",", "."))
            amount = value_dec / nominal_dec
        except (InvalidOperation, ZeroDivisionError):
            continue

        _, created = CurrencyRate.objects.update_or_create(
            date=rate_date,
            currency=currency_code,
            defaults={"amount": amount},
        )
        if created:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def sync_cbr_rates(currency_codes, start_date, end_date):
    result = {
        "inserted": 0,
        "updated": 0,
        "synced": [],
        "skipped": [],
        "failed": [],
    }

    requested_codes = sorted(set(code.upper() for code in currency_codes if code))
    cbr_requested_codes = [code for code in requested_codes if code not in {"RUB", "USDT"}]
    code_map = {}
    if cbr_requested_codes:
        try:
            code_map = _fetch_cbr_code_map()
        except Exception as exc:
            result["failed"].extend(f"{code}: {exc}" for code in cbr_requested_codes)
            cbr_requested_codes = []

    for currency_code in requested_codes:
        if currency_code == "RUB":
            inserted, updated = _sync_rub_rates(start_date, end_date)
            result["inserted"] += inserted
            result["updated"] += updated
            result["synced"].append("RUB")
            continue

        if currency_code == "USDT":
            inserted, updated = _sync_usdt_from_usd(start_date, end_date)
            result["inserted"] += inserted
            result["updated"] += updated
            result["synced"].append("USDT")
            continue

        cbr_code = code_map.get(currency_code)
        if not cbr_code:
            if currency_code not in cbr_requested_codes and any(item.startswith(f"{currency_code}:") for item in result["failed"]):
                continue
            result["skipped"].append(currency_code)
            continue

        try:
            inserted, updated = _sync_cbr_currency_rates(currency_code, cbr_code, start_date, end_date)
            fill_inserted, fill_updated = _fill_missing_daily_rates(currency_code, start_date, end_date)
        except Exception as exc:
            result["failed"].append(f"{currency_code}: {exc}")
            continue
        result["inserted"] += inserted
        result["updated"] += updated
        result["inserted"] += fill_inserted
        result["updated"] += fill_updated
        result["synced"].append(currency_code)

    return result


def sync_cbr_rates_full(currency_codes, start_date, end_date):
    return sync_cbr_rates(currency_codes=currency_codes, start_date=start_date, end_date=end_date)


def sync_cbr_rates_incremental(currency_codes, default_start_date, end_date):
    result = {
        "inserted": 0,
        "updated": 0,
        "synced": [],
        "skipped": [],
        "failed": [],
    }

    requested_codes = sorted(set(code.upper() for code in currency_codes if code))
    cbr_requested_codes = [code for code in requested_codes if code not in {"RUB", "USDT"}]
    code_map = {}
    if cbr_requested_codes:
        try:
            code_map = _fetch_cbr_code_map()
        except Exception as exc:
            result["failed"].extend(f"{code}: {exc}" for code in cbr_requested_codes)
            cbr_requested_codes = []

    for currency_code in requested_codes:
        latest_date = (
            CurrencyRate.objects.filter(currency=currency_code)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        start_date = latest_date + timedelta(days=1) if latest_date else default_start_date

        if currency_code == "RUB":
            if start_date > end_date:
                result["synced"].append(currency_code)
                continue
            inserted, updated = _sync_rub_rates(start_date, end_date)
            result["inserted"] += inserted
            result["updated"] += updated
            result["synced"].append("RUB")
            continue

        if currency_code == "USDT":
            usd_latest_date = (
                CurrencyRate.objects.filter(currency="USD")
                .order_by("-date")
                .values_list("date", flat=True)
                .first()
            )
            sync_end_date = min(end_date, usd_latest_date) if usd_latest_date else end_date
            inserted, updated = _sync_usdt_from_usd(default_start_date, sync_end_date)
            if usd_latest_date:
                CurrencyRate.objects.filter(currency="USDT", date__gt=usd_latest_date).delete()
            result["inserted"] += inserted
            result["updated"] += updated
            result["synced"].append("USDT")
            continue

        if start_date > end_date:
            result["synced"].append(currency_code)
            continue

        cbr_code = code_map.get(currency_code)
        if not cbr_code:
            if currency_code not in cbr_requested_codes and any(item.startswith(f"{currency_code}:") for item in result["failed"]):
                continue
            result["skipped"].append(currency_code)
            continue

        try:
            inserted, updated = _sync_cbr_currency_rates(currency_code, cbr_code, start_date, end_date)
            fill_inserted, fill_updated = _fill_missing_daily_rates(currency_code, start_date, end_date)
        except Exception as exc:
            result["failed"].append(f"{currency_code}: {exc}")
            continue
        result["inserted"] += inserted
        result["updated"] += updated
        result["inserted"] += fill_inserted
        result["updated"] += fill_updated
        result["synced"].append(currency_code)

    return result
