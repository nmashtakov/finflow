from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0004_bybit_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="bybitexternalevent",
            name="fee_amount",
            field=models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True),
        ),
    ]
