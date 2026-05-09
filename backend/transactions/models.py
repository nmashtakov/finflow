from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core.models import ExpenseLink, Transaction


class ImportSource(models.Model):
    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class TransactionImportSession(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", _("Uploaded")
        NORMALIZED = "normalized", _("Normalized")
        CATEGORIZED = "categorized", _("Categorized")
        REVIEW = "review", _("Review")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='transaction_imports')
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT, related_name="import_sessions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)
    total_rows = models.IntegerField(default=0)
    matched_rows = models.IntegerField(default=0)
    needs_review_rows = models.IntegerField(default=0)
    normalizer_version = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    original_name = models.CharField(max_length=255)
    columns = models.JSONField()
    sample_rows = models.JSONField()
    rows = models.JSONField()
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Import {self.original_name} ({self.created_at:%Y-%m-%d %H:%M})"


class CategorizationRule(models.Model):
    class ActionSign(models.TextChoices):
        ANY = "any", _("Any")
        INCOME = "income", _("Income")
        EXPENSE = "expense", _("Expense")

    RULE_FIELDS = {
        "merchant_norm",
        "description_norm",
        "source_category_norm",
        "direction",
        "currency",
    }
    RULE_OPERATORS = {"equals", "contains", "regex"}

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="categorization_rules",
        null=True,
        blank=True,
    )
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT, related_name="categorization_rules")
    name = models.CharField(max_length=255)
    priority = models.IntegerField(default=0)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    is_active = models.BooleanField(default=True)
    conditions = models.JSONField(default=list)
    action_expense_link = models.ForeignKey(
        ExpenseLink,
        on_delete=models.PROTECT,
        related_name="categorization_rules",
    )
    action_sign = models.CharField(max_length=16, choices=ActionSign.choices, default=ActionSign.ANY)
    created_from_feedback = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "-confidence", "id"]
        indexes = [
            models.Index(fields=["source", "user", "is_active", "priority"], name="rule_src_user_act_pr"),
            models.Index(fields=["source", "is_active"], name="rule_src_active"),
        ]

    def __str__(self):
        owner = "system" if self.user_id is None else f"user:{self.user_id}"
        return f"{self.name} [{owner}]"

    def clean(self):
        super().clean()
        if not isinstance(self.conditions, list):
            raise ValidationError({"conditions": "Conditions must be a list of objects."})
        if not self.conditions:
            raise ValidationError({"conditions": "At least one condition is required."})

        for index, condition in enumerate(self.conditions):
            if not isinstance(condition, dict):
                raise ValidationError({"conditions": f"Condition #{index + 1} must be an object."})

            keys = set(condition.keys())
            if keys != {"field", "operator", "value"}:
                raise ValidationError(
                    {"conditions": f"Condition #{index + 1} must contain exactly field, operator, value."}
                )

            field = condition.get("field")
            operator = condition.get("operator")
            value = condition.get("value")

            if field not in self.RULE_FIELDS:
                raise ValidationError({"conditions": f"Condition #{index + 1}: unsupported field '{field}'."})
            if operator not in self.RULE_OPERATORS:
                raise ValidationError(
                    {"conditions": f"Condition #{index + 1}: unsupported operator '{operator}'."}
                )
            if value is None:
                raise ValidationError({"conditions": f"Condition #{index + 1}: value is required."})
            if not isinstance(value, str):
                raise ValidationError({"conditions": f"Condition #{index + 1}: value must be a string."})
            if not value.strip():
                raise ValidationError({"conditions": f"Condition #{index + 1}: value cannot be empty."})
            if operator == "equals" and value == "":
                raise ValidationError({"conditions": f"Condition #{index + 1}: equals '' is not allowed."})


class ImportedTransaction(models.Model):
    class Direction(models.TextChoices):
        INCOME = "income", _("Income")
        EXPENSE = "expense", _("Expense")

    class CategorizationStatus(models.TextChoices):
        AUTO = "auto", _("Auto")
        MANUAL = "manual", _("Manual")
        NEEDS_REVIEW = "needs_review", _("Needs review")

    session = models.ForeignKey(
        TransactionImportSession,
        on_delete=models.CASCADE,
        related_name="imported_transactions",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="imported_transactions")
    raw_payload = models.JSONField(default=dict)
    normalized_payload = models.JSONField(default=dict)

    occurred_at = models.DateTimeField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    amount_abs = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10, blank=True, default="")
    direction = models.CharField(max_length=16, choices=Direction.choices, blank=True, default="")
    merchant_norm = models.CharField(max_length=255, blank=True, default="")
    description_norm = models.CharField(max_length=255, blank=True, default="")
    source_category_norm = models.CharField(max_length=255, blank=True, default="")
    external_id = models.CharField(max_length=255, null=True, blank=True)
    original_description = models.TextField(null=True, blank=True)

    fingerprint = models.CharField(max_length=128)
    categorization_status = models.CharField(
        max_length=16,
        choices=CategorizationStatus.choices,
        default=CategorizationStatus.NEEDS_REVIEW,
    )
    matched_rule = models.ForeignKey(
        CategorizationRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matched_transactions",
    )
    match_confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    resolved_expense_link = models.ForeignKey(
        ExpenseLink,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="imported_transactions",
    )
    locked = models.BooleanField(default=False)
    final_transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="imported_transactions",
    )
    split_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="split_children",
    )
    is_split_parent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "fingerprint"], name="uniq_session_fingerprint"),
        ]
        indexes = [
            models.Index(fields=["user", "categorization_status"], name="itx_user_status"),
            models.Index(fields=["session", "locked"], name="itx_session_locked"),
            models.Index(fields=["fingerprint"], name="itx_fingerprint"),
        ]

    def __str__(self):
        return f"Imported tx {self.id} ({self.categorization_status})"


class CategorizationFeedback(models.Model):
    imported_transaction = models.ForeignKey(
        ImportedTransaction,
        on_delete=models.CASCADE,
        related_name="feedback_items",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="categorization_feedback")
    previous_rule = models.ForeignKey(
        CategorizationRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback_previous",
    )
    new_expense_link = models.ForeignKey(
        ExpenseLink,
        on_delete=models.PROTECT,
        related_name="categorization_feedback",
    )
    created_or_updated_rule = models.ForeignKey(
        CategorizationRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback_created_or_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Feedback tx:{self.imported_transaction_id} by user:{self.user_id}"


class BybitConnection(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bybit_connections")
    name = models.CharField(max_length=120, default="Bybit")
    api_key = models.CharField(max_length=255)
    api_secret = models.CharField(max_length=255)
    is_testnet = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.user_id}: {self.name}"


class BybitSyncRun(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")

    connection = models.ForeignKey(BybitConnection, on_delete=models.CASCADE, related_name="sync_runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUCCESS)
    range_from = models.DateTimeField()
    range_to = models.DateTimeField()
    streams = models.JSONField(default=list, blank=True)
    fetched_count = models.IntegerField(default=0)
    inserted_count = models.IntegerField(default=0)
    updated_count = models.IntegerField(default=0)
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"Bybit sync #{self.id} ({self.status})"


class BybitExternalEvent(models.Model):
    class Direction(models.TextChoices):
        IN = "in", _("In")
        OUT = "out", _("Out")
        UNKNOWN = "unknown", _("Unknown")

    connection = models.ForeignKey(BybitConnection, on_delete=models.CASCADE, related_name="external_events")
    stream = models.CharField(max_length=64)
    external_id = models.CharField(max_length=255, blank=True, default="")
    event_hash = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(null=True, blank=True)
    asset = models.CharField(max_length=32, blank=True, default="")
    amount = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    fee_amount = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    direction = models.CharField(max_length=16, choices=Direction.choices, default=Direction.UNKNOWN)
    description = models.CharField(max_length=512, blank=True, default="")
    raw_payload = models.JSONField(default=dict)
    imported_transaction = models.ForeignKey(
        ImportedTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bybit_events",
    )
    posted_transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bybit_source_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "stream", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_bybit_conn_stream_external_id",
            ),
            models.UniqueConstraint(
                fields=["connection", "stream", "event_hash"],
                name="uniq_bybit_conn_stream_event_hash",
            ),
        ]
        indexes = [
            models.Index(fields=["connection", "stream"], name="bybit_evt_conn_stream"),
            models.Index(fields=["connection", "occurred_at"], name="bybit_evt_conn_occurred"),
        ]

    def __str__(self):
        return f"{self.stream}:{self.external_id or self.event_hash[:8]}"
