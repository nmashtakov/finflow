from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def assign_default_source(apps, schema_editor):
    ImportSource = apps.get_model("transactions", "ImportSource")
    TransactionImportSession = apps.get_model("transactions", "TransactionImportSession")

    default_source, _ = ImportSource.objects.get_or_create(
        code="generic",
        defaults={"name": "Generic file", "is_active": True},
    )
    TransactionImportSession.objects.filter(source__isnull=True).update(source=default_source)


def reverse_assign_default_source(apps, schema_editor):
    ImportSource = apps.get_model("transactions", "ImportSource")
    TransactionImportSession = apps.get_model("transactions", "TransactionImportSession")
    TransactionImportSession.objects.filter(source__code="generic").update(source=None)
    ImportSource.objects.filter(code="generic").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_userpreferences"),
        ("transactions", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="transactionimportsession",
            name="matched_rows",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="transactionimportsession",
            name="needs_review_rows",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="transactionimportsession",
            name="normalizer_version",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="transactionimportsession",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="import_sessions",
                to="transactions.importsource",
            ),
        ),
        migrations.AddField(
            model_name="transactionimportsession",
            name="status",
            field=models.CharField(
                choices=[
                    ("uploaded", "Uploaded"),
                    ("normalized", "Normalized"),
                    ("categorized", "Categorized"),
                    ("review", "Review"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                ],
                default="uploaded",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="transactionimportsession",
            name="total_rows",
            field=models.IntegerField(default=0),
        ),
        migrations.RunPython(assign_default_source, reverse_assign_default_source),
        migrations.AlterField(
            model_name="transactionimportsession",
            name="source",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="import_sessions",
                to="transactions.importsource",
            ),
        ),
        migrations.CreateModel(
            name="CategorizationRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("priority", models.IntegerField(default=0)),
                ("confidence", models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ("is_active", models.BooleanField(default=True)),
                ("conditions", models.JSONField(default=list)),
                (
                    "action_sign",
                    models.CharField(
                        choices=[("any", "Any"), ("income", "Income"), ("expense", "Expense")],
                        default="any",
                        max_length=16,
                    ),
                ),
                ("created_from_feedback", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "action_expense_link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="categorization_rules",
                        to="core.expenselink",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="categorization_rules",
                        to="transactions.importsource",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="categorization_rules",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-priority", "-confidence", "id"],
                "indexes": [
                    models.Index(fields=["source", "user", "is_active", "priority"], name="rule_src_user_act_pr"),
                    models.Index(fields=["source", "is_active"], name="rule_src_active"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ImportedTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("raw_payload", models.JSONField(default=dict)),
                ("normalized_payload", models.JSONField(default=dict)),
                ("occurred_at", models.DateTimeField(blank=True, null=True)),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("amount_abs", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("currency", models.CharField(blank=True, default="", max_length=10)),
                (
                    "direction",
                    models.CharField(
                        blank=True,
                        choices=[("income", "Income"), ("expense", "Expense")],
                        default="",
                        max_length=16,
                    ),
                ),
                ("merchant_norm", models.CharField(blank=True, default="", max_length=255)),
                ("description_norm", models.CharField(blank=True, default="", max_length=255)),
                ("source_category_norm", models.CharField(blank=True, default="", max_length=255)),
                ("external_id", models.CharField(blank=True, max_length=255, null=True)),
                ("original_description", models.TextField(blank=True, null=True)),
                ("fingerprint", models.CharField(max_length=128)),
                (
                    "categorization_status",
                    models.CharField(
                        choices=[("auto", "Auto"), ("manual", "Manual"), ("needs_review", "Needs review")],
                        default="needs_review",
                        max_length=16,
                    ),
                ),
                ("match_confidence", models.DecimalField(blank=True, decimal_places=4, max_digits=5, null=True)),
                ("locked", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "final_transaction",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="imported_transactions",
                        to="core.transaction",
                    ),
                ),
                (
                    "matched_rule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="matched_transactions",
                        to="transactions.categorizationrule",
                    ),
                ),
                (
                    "resolved_expense_link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="imported_transactions",
                        to="core.expenselink",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="imported_transactions",
                        to="transactions.transactionimportsession",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="imported_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["user", "categorization_status"], name="itx_user_status"),
                    models.Index(fields=["session", "locked"], name="itx_session_locked"),
                    models.Index(fields=["fingerprint"], name="itx_fingerprint"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("session", "fingerprint"), name="uniq_session_fingerprint"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CategorizationFeedback",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_or_updated_rule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="feedback_created_or_updated",
                        to="transactions.categorizationrule",
                    ),
                ),
                (
                    "imported_transaction",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedback_items",
                        to="transactions.importedtransaction",
                    ),
                ),
                (
                    "new_expense_link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="categorization_feedback",
                        to="core.expenselink",
                    ),
                ),
                (
                    "previous_rule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="feedback_previous",
                        to="transactions.categorizationrule",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="categorization_feedback",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
