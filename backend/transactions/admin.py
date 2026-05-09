from django.contrib import admin

from .models import (
    BybitConnection,
    BybitExternalEvent,
    BybitSyncRun,
    CategorizationFeedback,
    CategorizationRule,
    ImportedTransaction,
    ImportSource,
    TransactionImportSession,
)


@admin.register(ImportSource)
class ImportSourceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "updated_at")
    search_fields = ("code", "name")
    list_filter = ("is_active",)


@admin.register(TransactionImportSession)
class TransactionImportSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "source",
        "status",
        "total_rows",
        "matched_rows",
        "needs_review_rows",
        "created_at",
    )
    search_fields = ("original_name", "user__username", "source__code")
    list_filter = ("status", "source")


@admin.register(ImportedTransaction)
class ImportedTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "user",
        "categorization_status",
        "direction",
        "amount",
        "currency",
        "locked",
        "created_at",
    )
    search_fields = ("merchant_norm", "description_norm", "fingerprint", "external_id")
    list_filter = ("categorization_status", "direction", "locked")


@admin.register(CategorizationRule)
class CategorizationRuleAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "source", "user", "priority", "confidence", "is_active", "updated_at")
    search_fields = ("name", "source__code", "user__username")
    list_filter = ("source", "is_active", "action_sign")


@admin.register(CategorizationFeedback)
class CategorizationFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "imported_transaction", "user", "previous_rule", "created_or_updated_rule", "created_at")
    search_fields = ("user__username",)


@admin.register(BybitConnection)
class BybitConnectionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "name", "is_testnet", "is_active", "last_sync_at", "updated_at")
    search_fields = ("user__username", "name", "api_key")
    list_filter = ("is_testnet", "is_active")


@admin.register(BybitSyncRun)
class BybitSyncRunAdmin(admin.ModelAdmin):
    list_display = ("id", "connection", "status", "range_from", "range_to", "fetched_count", "inserted_count", "updated_count", "created_at")
    search_fields = ("connection__name", "connection__user__username", "message")
    list_filter = ("status",)


@admin.register(BybitExternalEvent)
class BybitExternalEventAdmin(admin.ModelAdmin):
    list_display = ("id", "connection", "stream", "external_id", "occurred_at", "asset", "amount", "direction")
    search_fields = ("external_id", "event_hash", "description", "asset")
    list_filter = ("stream", "direction")
