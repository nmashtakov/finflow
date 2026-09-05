from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crypto', '0002_market_prices'),
    ]

    operations = [
        migrations.AddField(
            model_name='cryptoasset',
            name='wallet_quantity',
            field=models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True),
        ),
        migrations.AddField(
            model_name='cryptoasset',
            name='wallet_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
