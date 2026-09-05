import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Tuple

from django.utils import timezone

from transactions.integrations.bybit.client import BybitApiError, BybitV5Client
from transactions.models import BybitConnection, BybitExternalEvent, BybitSyncRun


STREAM_FETCHERS = {
    "uta_translog": "fetch_transaction_log",
    "funding_history": "fetch_funding_history",
    "convert_history": "fetch_convert_history",
}


def _extract_event_id(item: Dict) -> str:
    for key in ("id", "transactionId", "tradeId", "orderId", "execId", "bizId", "transferId", "txID", "withdrawId"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_occurred_at(item: Dict):
    for key in ("transactionTime", "execTime", "updatedTime", "createdTime", "createTime", "successAt", "time", "timestamp"):
        value = item.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        try:
            ms = int(float(text))
            if ms > 10_000_000_000:
                return timezone.datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            return timezone.datetime.fromtimestamp(ms, tz=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_amount(item: Dict):
    for key in ("change", "cashFlow", "qty", "size", "amount", "fundingAmount", "txnAmt", "fromAmount", "toAmount"):
        value = item.get(key)
        if value in (None, ""):
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None


def _parse_fee_amount(item: Dict):
    for key in (
        "fee",
        "execFee",
        "convertFee",
        "exchangeFee",
        "feeAmount",
        "withdrawFee",
        "depositFee",
        "tax",
        "txFee",
        "networkFee",
        "serviceCharge",
    ):
        value = item.get(key)
        if value in (None, ""):
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None


def _normalize_direction(amount: Optional[Decimal]) -> str:
    if amount is None:
        return BybitExternalEvent.Direction.UNKNOWN
    if amount > 0:
        return BybitExternalEvent.Direction.IN
    if amount < 0:
        return BybitExternalEvent.Direction.OUT
    return BybitExternalEvent.Direction.UNKNOWN


def _event_hash(stream: str, item: Dict) -> str:
    raw = json.dumps({"stream": stream, "payload": item}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _needs_update(existing: BybitExternalEvent, defaults: Dict) -> bool:
    tracked_fields = ("occurred_at", "asset", "amount", "fee_amount", "direction", "description", "raw_payload")
    for field in tracked_fields:
        if getattr(existing, field) != defaults.get(field):
            return True
    return False


def _upsert_events(connection: BybitConnection, stream: str, items: list, stats: Dict[str, int]) -> None:
    for item in items:
        external_id = _extract_event_id(item)
        event_hash = _event_hash(stream, item)
        occurred_at = _parse_occurred_at(item)
        amount = _parse_amount(item)
        fee_amount = _parse_fee_amount(item)
        if stream == "funding_history" and amount is not None:
            io_direction_raw = str(item.get("ioDirection") or "").strip()
            io_direction = io_direction_raw.lower()
            flow_hint = str(item.get("showBusiTypeEn") or item.get("descriptionEn") or "").lower()
            # По доке Bybit: ioDirection = I (In), O (Out). Дополнительно поддерживаем 0/1 из legacy-ответов.
            if io_direction in {"o", "out", "debit", "0"} or "out" in flow_hint or "withdraw" in flow_hint:
                amount = -abs(amount)
            elif io_direction in {"i", "in", "credit", "1"} or "in" in flow_hint or "deposit" in flow_hint:
                amount = abs(amount)
        elif stream == "convert_history":
            from_amount_raw = item.get("fromAmount")
            to_amount_raw = item.get("toAmount")
            try:
                from_amount = Decimal(str(from_amount_raw)) if from_amount_raw not in (None, "") else None
            except (InvalidOperation, ValueError):
                from_amount = None
            try:
                to_amount = Decimal(str(to_amount_raw)) if to_amount_raw not in (None, "") else None
            except (InvalidOperation, ValueError):
                to_amount = None
            # Для аналитики фиксируем исходящее плечо конверта как расход.
            if from_amount is not None:
                amount = -abs(from_amount)
            elif to_amount is not None:
                amount = abs(to_amount)
        direction = _normalize_direction(amount)
        asset = str(item.get("currency") or item.get("coin") or "").upper()
        description = str(item.get("type") or item.get("bizType") or item.get("symbol") or "").strip()
        if stream == "funding_history":
            description = str(
                item.get("descriptionEn")
                or item.get("showBusiTypeEn")
                or item.get("description")
                or item.get("showBusiType")
                or item.get("status")
                or "funding"
            ).strip()
            asset = str(item.get("currency") or item.get("coin") or asset).upper()
        elif stream == "convert_history":
            from_coin = str(item.get("fromCoin") or "").upper()
            to_coin = str(item.get("toCoin") or "").upper()
            asset = from_coin or asset or to_coin
            description = str(
                item.get("status")
                or f"convert {from_coin}->{to_coin}"
            ).strip()

        defaults = {
            "occurred_at": occurred_at,
            "asset": asset,
            "amount": amount,
            "fee_amount": fee_amount,
            "direction": direction,
            "description": description,
            "raw_payload": item,
        }

        if external_id:
            existing = BybitExternalEvent.objects.filter(
                connection=connection,
                stream=stream,
                external_id=external_id,
            ).first()
            if existing is None:
                BybitExternalEvent.objects.create(
                    connection=connection,
                    stream=stream,
                    external_id=external_id,
                    event_hash=event_hash,
                    **defaults,
                )
                created = True
                updated = False
            else:
                created = False
                updated = _needs_update(existing, defaults) or existing.event_hash != event_hash
                if updated:
                    existing.event_hash = event_hash
                    for key, value in defaults.items():
                        setattr(existing, key, value)
                    existing.save(
                        update_fields=[
                            "event_hash",
                            "occurred_at",
                            "asset",
                            "amount",
                            "fee_amount",
                            "direction",
                            "description",
                            "raw_payload",
                            "updated_at",
                        ]
                    )
        else:
            existing = BybitExternalEvent.objects.filter(
                connection=connection,
                stream=stream,
                event_hash=event_hash,
            ).first()
            if existing is None:
                BybitExternalEvent.objects.create(
                    connection=connection,
                    stream=stream,
                    external_id="",
                    event_hash=event_hash,
                    **defaults,
                )
                created = True
                updated = False
            else:
                created = False
                updated = _needs_update(existing, defaults)
                if updated:
                    for key, value in defaults.items():
                        setattr(existing, key, value)
                    existing.save(
                        update_fields=[
                            "occurred_at",
                            "asset",
                            "amount",
                            "fee_amount",
                            "direction",
                            "description",
                            "raw_payload",
                            "updated_at",
                        ]
                    )

        if created:
            stats["inserted"] += 1
        elif updated:
            stats["updated"] += 1


def sync_bybit_transaction_log(
    connection: BybitConnection,
    days: int = 3,
    streams: Optional[list[str]] = None,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> Tuple[BybitSyncRun, Dict[str, int]]:
    now = timezone.now()
    if end_dt is None:
        end_dt = now
    if start_dt is None:
        start_dt = end_dt - timedelta(days=max(1, days))
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    if timezone.is_naive(start_dt):
        start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
    if timezone.is_naive(end_dt):
        end_dt = timezone.make_aware(end_dt, timezone.get_current_timezone())

    selected_streams = [s for s in (streams or list(STREAM_FETCHERS.keys())) if s in STREAM_FETCHERS]
    if not selected_streams:
        selected_streams = list(STREAM_FETCHERS.keys())

    sync_run = BybitSyncRun.objects.create(
        connection=connection,
        status=BybitSyncRun.Status.SUCCESS,
        range_from=start_dt,
        range_to=end_dt,
        streams=selected_streams,
    )

    stats = {
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
    }
    stream_errors = []

    try:
        client = BybitV5Client(
            api_key=connection.api_key,
            api_secret=connection.api_secret,
            is_testnet=connection.is_testnet,
        )
        max_window = timedelta(days=7)
        window_start = start_dt
        while window_start < end_dt:
            window_end = min(window_start + max_window, end_dt)
            start_ms = int(window_start.timestamp() * 1000)
            end_ms = int(window_end.timestamp() * 1000)
            start_sec = int(window_start.timestamp())
            end_sec = int(window_end.timestamp())
            for stream_name in selected_streams:
                if stream_name == "convert_history":
                    page_index = 1
                    while True:
                        try:
                            payload = client.fetch_convert_history(index=page_index, limit=50)
                        except BybitApiError as exc:
                            stream_errors.append(f"{stream_name}: {exc}")
                            break
                        result = payload.get("result") or {}
                        items = result.get("list") or result.get("rows") or []
                        stats["fetched"] += len(items)
                        _upsert_events(connection=connection, stream=stream_name, items=items, stats=stats)
                        if len(items) < 50:
                            break
                        page_index += 1
                    continue

                cursor = ""
                while True:
                    try:
                        if stream_name == "funding_history":
                            payload = client.fetch_funding_history(
                                start_sec=start_sec,
                                end_sec=end_sec,
                                limit=50,
                                cursor=cursor,
                            )
                        else:
                            stream_fetcher = getattr(client, STREAM_FETCHERS[stream_name])
                            payload = stream_fetcher(start_ms=start_ms, end_ms=end_ms, limit=50, cursor=cursor)
                    except BybitApiError as exc:
                        stream_errors.append(f"{stream_name}: {exc}")
                        break
                    result = payload.get("result") or {}
                    items = result.get("list") or result.get("rows") or []
                    stats["fetched"] += len(items)
                    _upsert_events(connection=connection, stream=stream_name, items=items, stats=stats)
                    cursor = result.get("nextPageCursor") or ""
                    if not cursor:
                        break
            window_start = window_end

        connection.last_sync_at = timezone.now()
        connection.save(update_fields=["last_sync_at", "updated_at"])
        if stream_errors:
            sync_run.message = " | ".join(stream_errors)

    except BybitApiError as exc:
        sync_run.status = BybitSyncRun.Status.FAILED
        sync_run.message = str(exc)
    except Exception as exc:  # pragma: no cover
        sync_run.status = BybitSyncRun.Status.FAILED
        sync_run.message = f"Unexpected error: {exc}"

    sync_run.fetched_count = stats["fetched"]
    sync_run.inserted_count = stats["inserted"]
    sync_run.updated_count = stats["updated"]
    sync_run.save(update_fields=["status", "message", "fetched_count", "inserted_count", "updated_count"])
    return sync_run, stats
