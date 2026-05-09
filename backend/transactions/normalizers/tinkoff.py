from .generic import GenericNormalizer


class TinkoffNormalizer(GenericNormalizer):
    code = "tinkoff"
    version = "tinkoff_v1"

    def normalize_row(self, row, context):
        normalized = super().normalize_row(row, context)
        mapped_category = row.get(context.column_category) if context.column_category else ""
        mapped_description = row.get(context.column_comment) if context.column_comment else ""

        category_raw = self.normalize_string(
            row.get("category")
            or row.get("Категория")
            or mapped_category
        )
        description_raw = self.normalize_string(
            row.get("description")
            or row.get("Описание")
            or mapped_description
        )
        merchant_raw = self.normalize_string(row.get("merchant") or row.get("Мерчант"))

        normalized.source_category_norm = self.normalize_text(category_raw)
        normalized.description_norm = self.normalize_text(description_raw)
        normalized.merchant_norm = self.normalize_text(merchant_raw) if merchant_raw else self.extract_merchant(description_raw)
        normalized.original_description = description_raw or normalized.original_description
        return normalized
