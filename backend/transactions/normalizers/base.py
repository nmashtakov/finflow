import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional

from django.utils import timezone


class NormalizationError(Exception):
    """Raised when a source row cannot be normalized."""


@dataclass
class NormalizationContext:
    column_date: str
    column_amount: str
    column_currency: Optional[str]
    column_comment: Optional[str]
    column_type: Optional[str]
    column_category: Optional[str]
    column_subcategory: Optional[str]
    default_currency: str
    income_markers: set[str]
    expense_markers: set[str]


@dataclass
class NormalizedTx:
    occurred_at: datetime
    amount: Decimal
    amount_abs: Decimal
    currency: str
    direction: str
    merchant_norm: str
    description_norm: str
    source_category_norm: str
    external_id: Optional[str]
    original_description: Optional[str]


class BaseNormalizer:
    code = "generic"
    version = "generic_v1"

    def normalize_row(self, row: Dict[str, Any], context: NormalizationContext) -> NormalizedTx:
        raise NotImplementedError

    @classmethod
    def detect_external_id(cls, row: Dict[str, Any]) -> Optional[str]:
        candidate = cls.normalize_string(
            row.get("id")
            or row.get("operation_id")
            or row.get("transaction_id")
            or row.get("operationId")
        )
        return candidate or None

    @classmethod
    def infer_direction_and_amount(cls, amount: Decimal, raw_type: str, income_markers: set, expense_markers: set):
        normalized_amount = amount
        type_value = cls.normalize_string(raw_type).lower()
        if type_value and type_value in expense_markers:
            normalized_amount = -abs(amount)
        elif type_value and type_value in income_markers:
            normalized_amount = abs(amount)
        direction = "income" if normalized_amount > 0 else "expense"
        return normalized_amount, direction

    @classmethod
    def extract_merchant(cls, text: str) -> str:
        text = cls.normalize_string(text)
        if not text:
            return ""
        parts = [item.strip() for item in text.replace("|", ";").split(";") if item.strip()]
        merchant = parts[0] if parts else text
        return cls.normalize_text(merchant)

    @staticmethod
    def normalize_string(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return " ".join(cls.normalize_string(value).lower().split())

    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        text = cls.normalize_string(value)
        if not text:
            raise NormalizationError("Не указана сумма")
        text = text.replace(" ", "").replace("'", "").replace("\xa0", "").replace(",", ".")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise NormalizationError(f"Не удалось преобразовать сумму '{value}'") from exc

    @classmethod
    def parse_date_value(cls, value: Any) -> datetime:
        text = cls.normalize_string(value)
        if not text:
            raise NormalizationError("Не указана дата")
        patterns = (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        )
        for pattern in patterns:
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise NormalizationError(f"Не удалось распознать дату '{value}'") from exc

        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    @staticmethod
    def make_fingerprint(*parts: Iterable[Any]) -> str:
        normalized_parts = ["" if part is None else str(part).strip() for part in parts]
        joined = "|".join(normalized_parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()
