from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crypto', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='cryptoportfoliosettings',
            name='cmc_api_key',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='cryptoasset',
            name='last_price_usd',
            field=models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True),
        ),
        migrations.AddField(
            model_name='cryptoasset',
            name='change_24h_pct',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='cryptoasset',
            name='price_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
