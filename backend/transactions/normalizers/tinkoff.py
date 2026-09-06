from .generic import GenericNormalizer


class TinkoffNormalizer(GenericNormalizer):
    code = "tinkoff"
    version = "tinkoff_v1"

    def normalize_row(self, row, context):
        normalized = super().normalize_row(row, context)
        mapped_category = row.get(context.column_category) if context.column_category else ""
        mapped_description = row.get(context.column_comment) if context.column_comment else ""

        custom_category = self.normalize_string(row.get("Ваша категория") or row.get("your_category"))
        default_category = self.normalize_string(
            row.get("Категория по-умолчанию")
            or row.get("Категория")
            or row.get("category")
            or mapped_category
        )
        category_raw = custom_category or default_category
        description_raw = self.normalize_string(
            row.get("description")
            or row.get("Описание")
            or mapped_description
        )
        merchant_raw = self.normalize_string(row.get("merchant") or row.get("Мерчант"))
        message_raw = self.normalize_string(row.get("Сообщение") or row.get("message"))

        if description_raw and message_raw:
            original_description = f"{description_raw} — {message_raw}"
        else:
            original_description = description_raw or message_raw or normalized.original_description

        normalized.source_category_norm = self.normalize_text(category_raw)
        normalized.description_norm = self.normalize_text(description_raw)
        normalized.merchant_norm = self.normalize_text(merchant_raw) if merchant_raw else self.extract_merchant(description_raw)
        normalized.original_description = original_description
        return normalized
