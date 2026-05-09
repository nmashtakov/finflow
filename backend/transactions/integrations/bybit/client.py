import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class BybitApiError(Exception):
    pass


class BybitV5Client:
    def __init__(self, api_key: str, api_secret: str, is_testnet: bool = False, recv_window: int = 5000):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.recv_window = str(recv_window)
        self.base_url = "https://api-testnet.bybit.com" if is_testnet else "https://api.bybit.com"

    def _build_signature(self, timestamp: str, query_string: str) -> str:
        payload = f"{timestamp}{self.api_key}{self.recv_window}{query_string}"
        return hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        query_string = urllib.parse.urlencode(params, doseq=True)
        timestamp = str(int(time.time() * 1000))
        signature = self._build_signature(timestamp, query_string)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        req = urllib.request.Request(url=url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
        except Exception as exc:
            raise BybitApiError(f"HTTP error: {exc}") from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BybitApiError("Bybit returned invalid JSON") from exc

        if payload.get("retCode") != 0:
            raise BybitApiError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")

        return payload

    def _get_with_fallback_paths(self, paths: list[str], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_error = None
        for path in paths:
            try:
                return self._get(path, params=params)
            except BybitApiError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise BybitApiError("No paths configured for request")

    def fetch_transaction_log(self, start_ms: int, end_ms: int, limit: int = 50, cursor: str = "") -> Dict[str, Any]:
        params = {
            "accountType": "UNIFIED",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self._get("/v5/account/transaction-log", params=params)

    def fetch_internal_transfer_records(self, start_ms: int, end_ms: int, limit: int = 50, cursor: str = "") -> Dict[str, Any]:
        params = {
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self._get("/v5/asset/transfer/query-inter-transfer-list", params=params)

    def fetch_funding_history(self, start_sec: int, end_sec: int, limit: int = 50, cursor: str = "") -> Dict[str, Any]:
        params = {
            "createTimeFrom": str(start_sec),
            "createTimeTo": str(end_sec),
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self._get_with_fallback_paths(
            paths=[
                "/v5/asset/funding/history",
                "/v5/asset/fundinghistory",
                "/v5/asset/fund-history",
            ],
            params=params,
        )

    def fetch_convert_history(self, index: int = 1, limit: int = 50) -> Dict[str, Any]:
        params = {
            "index": index,
            "limit": limit,
        }
        return self._get_with_fallback_paths(
            paths=[
                "/v5/asset/exchange/query-convert-history",
                "/v5/asset/exchange/order-record",
            ],
            params=params,
        )

    def fetch_deposit_records(self, start_ms: int, end_ms: int, limit: int = 50, cursor: str = "") -> Dict[str, Any]:
        params = {
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self._get_with_fallback_paths(
            paths=[
                "/v5/asset/deposit/query-record",
                "/v5/asset/deposit/query-records",
            ],
            params=params,
        )

    def fetch_withdraw_records(self, start_ms: int, end_ms: int, limit: int = 50, cursor: str = "") -> Dict[str, Any]:
        params = {
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self._get_with_fallback_paths(
            paths=[
                "/v5/asset/withdraw/query-record",
                "/v5/asset/withdraw/query-records",
            ],
            params=params,
        )
