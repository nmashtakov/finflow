from .generic import GenericNormalizer


class TBusinessNormalizer(GenericNormalizer):
    code = "t_business"
    version = "t_business_v1"

    def normalize_row(self, row, context):
        normalized = super().normalize_row(row, context)

        payment_number = self.normalize_string(
            row.get("Номер платежа")
            or row.get("payment_number")
            or row.get("payment_id")
        )
        counterparty = self.normalize_string(
            row.get("Наименование контрагента")
            or row.get("Наименование плательщика")
            or row.get("Наименование получателя")
        )
        description = self.normalize_string(
            row.get("Назначение платежа")
            or row.get("Описание операции")
            or normalized.original_description
        )

        if normalized.currency == "643":
            normalized.currency = "RUB"
        normalized.external_id = payment_number or normalized.external_id
        normalized.source_category_norm = self.normalize_text(row.get(context.column_type)) if context.column_type else ""
        normalized.description_norm = self.normalize_text(description)
        normalized.merchant_norm = self.normalize_text(counterparty) if counterparty else self.extract_merchant(description)
        normalized.original_description = description or normalized.original_description
        return normalized
