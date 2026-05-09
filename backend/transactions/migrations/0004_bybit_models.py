from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0003_importedtransaction_split_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="BybitConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="Bybit", max_length=120)),
                ("api_key", models.CharField(max_length=255)),
                ("api_secret", models.CharField(max_length=255)),
                ("is_testnet", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bybit_connections", to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={"ordering": ["-updated_at", "-id"]},
        ),
        migrations.CreateModel(
            name="BybitSyncRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("success", "Success"), ("failed", "Failed")], default="success", max_length=16)),
                ("range_from", models.DateTimeField()),
                ("range_to", models.DateTimeField()),
                ("streams", models.JSONField(blank=True, default=list)),
                ("fetched_count", models.IntegerField(default=0)),
                ("inserted_count", models.IntegerField(default=0)),
                ("updated_count", models.IntegerField(default=0)),
                ("message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "connection",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sync_runs", to="transactions.bybitconnection"),
                ),
            ],
            options={"ordering": ["-id"]},
        ),
        migrations.CreateModel(
            name="BybitExternalEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stream", models.CharField(max_length=64)),
                ("external_id", models.CharField(blank=True, default="", max_length=255)),
                ("event_hash", models.CharField(max_length=64)),
                ("occurred_at", models.DateTimeField(blank=True, null=True)),
                ("asset", models.CharField(blank=True, default="", max_length=32)),
                ("amount", models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True)),
                ("direction", models.CharField(choices=[("in", "In"), ("out", "Out"), ("unknown", "Unknown")], default="unknown", max_length=16)),
                ("description", models.CharField(blank=True, default="", max_length=512)),
                ("raw_payload", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "connection",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_events", to="transactions.bybitconnection"),
                ),
                (
                    "imported_transaction",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bybit_events", to="transactions.importedtransaction"),
                ),
            ],
            options={"ordering": ["-occurred_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="bybitexternalevent",
            constraint=models.UniqueConstraint(condition=~Q(external_id=""), fields=("connection", "stream", "external_id"), name="uniq_bybit_conn_stream_external_id"),
        ),
        migrations.AddConstraint(
            model_name="bybitexternalevent",
            constraint=models.UniqueConstraint(fields=("connection", "stream", "event_hash"), name="uniq_bybit_conn_stream_event_hash"),
        ),
        migrations.AddIndex(
            model_name="bybitexternalevent",
            index=models.Index(fields=["connection", "stream"], name="bybit_evt_conn_stream"),
        ),
        migrations.AddIndex(
            model_name="bybitexternalevent",
            index=models.Index(fields=["connection", "occurred_at"], name="bybit_evt_conn_occurred"),
        ),
    ]
