from decimal import Decimal

from .base import BaseNormalizer, NormalizationContext, NormalizedTx


class BybitNormalizer(BaseNormalizer):
    code = "bybit"
    version = "bybit_v1"

    def normalize_row(self, row, context: NormalizationContext) -> NormalizedTx:
        occurred_at = self.parse_date_value(row.get(context.column_date))
        amount = self.parse_decimal(row.get(context.column_amount))
        currency = self.normalize_string(row.get(context.column_currency) or context.default_currency).upper()
        description_raw = self.normalize_string(row.get(context.column_comment))
        source_category = self.normalize_string(row.get(context.column_category))
        external_id = self.normalize_string(row.get("external_id")) or None

        direction = "income" if amount > 0 else "expense"
        merchant_norm = self.extract_merchant(description_raw)
        description_norm = self.normalize_text(description_raw)
        source_category_norm = self.normalize_text(source_category)

        return NormalizedTx(
            occurred_at=occurred_at,
            amount=amount,
            amount_abs=abs(Decimal(amount)),
            currency=currency,
            direction=direction,
            merchant_norm=merchant_norm,
            description_norm=description_norm,
            source_category_norm=source_category_norm,
            external_id=external_id,
            original_description=description_raw or None,
        )

