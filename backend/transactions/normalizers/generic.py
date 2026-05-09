from decimal import Decimal
from typing import Any, Dict

from .base import BaseNormalizer, NormalizationContext, NormalizationError, NormalizedTx


class GenericNormalizer(BaseNormalizer):
    code = "generic"
    version = "generic_v1"

    def normalize_row(self, row: Dict[str, Any], context: NormalizationContext) -> NormalizedTx:
        occurred_at = self.parse_date_value(row.get(context.column_date))
        amount = self.parse_decimal(row.get(context.column_amount))

        raw_type = self.normalize_string(row.get(context.column_type)) if context.column_type else ""
        amount, direction = self.infer_direction_and_amount(
            amount,
            raw_type,
            context.income_markers,
            context.expense_markers,
        )

        amount_abs = abs(amount)
        if amount == Decimal("0"):
            raise NormalizationError("Сумма операции не может быть равна 0")

        currency = self.normalize_string(row.get(context.column_currency)) if context.column_currency else ""
        if not currency:
            currency = context.default_currency or "RUB"
        currency = currency.upper()

        comment_raw = self.normalize_string(row.get(context.column_comment)) if context.column_comment else ""
        category_raw = self.normalize_string(row.get(context.column_category)) if context.column_category else ""

        description_norm = self.normalize_text(comment_raw)
        source_category_norm = self.normalize_text(category_raw)
        merchant_norm = self.extract_merchant(comment_raw)

        external_id = self.detect_external_id(row)

        return NormalizedTx(
            occurred_at=occurred_at,
            amount=amount,
            amount_abs=amount_abs,
            currency=currency,
            direction=direction,
            merchant_norm=merchant_norm,
            description_norm=description_norm,
            source_category_norm=source_category_norm,
            external_id=external_id,
            original_description=comment_raw or None,
        )
