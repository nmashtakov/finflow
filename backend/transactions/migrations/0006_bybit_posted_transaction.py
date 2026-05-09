from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_currency_directory_full"),
        ("transactions", "0005_bybit_fee_amount"),
    ]

    operations = [
        migrations.AddField(
            model_name="bybitexternalevent",
            name="posted_transaction",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bybit_source_events",
                to="core.transaction",
            ),
        ),
    ]
