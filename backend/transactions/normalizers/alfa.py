from .generic import GenericNormalizer


class AlfaNormalizer(GenericNormalizer):
    code = "alfa"
    version = "alfa_v1"

    def normalize_row(self, row, context):
        normalized = super().normalize_row(row, context)
        if normalized.currency == "RUR":
            normalized.currency = "RUB"
        mapped_category = row.get(context.column_category) if context.column_category else ""
        mapped_description = row.get(context.column_comment) if context.column_comment else ""

        category_raw = self.normalize_string(
            row.get("category")
            or row.get("Категория")
            or mapped_category
        )
        operation_description = self.normalize_string(
            row.get("operation_description")
            or row.get("Описание операции")
            or mapped_description
        )
        user_comment = self.normalize_string(
            row.get("comment")
            or row.get("Комментарий")
            or row.get("Комментарий операции")
            or row.get("Назначение платежа")
        )
        description_raw = f"{operation_description} {user_comment}".strip() if user_comment else operation_description

        normalized.source_category_norm = self.normalize_text(category_raw)
        normalized.description_norm = self.normalize_text(description_raw)
        normalized.merchant_norm = self.extract_merchant(operation_description)
        normalized.original_description = description_raw or normalized.original_description
        return normalized
