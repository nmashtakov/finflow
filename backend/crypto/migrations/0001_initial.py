from django.db import migrations, models
import django.db.models.deletion
from decimal import Decimal


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('transactions', '0006_bybit_posted_transaction'),
        migrations.swappable_dependency('auth.user'),
    ]

    operations = [
        migrations.CreateModel(
            name='CryptoAsset',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('symbol', models.CharField(max_length=16)),
                ('name', models.CharField(max_length=64)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='crypto_assets', to='auth.user')),
            ],
            options={
                'ordering': ['sort_order', 'symbol', 'id'],
            },
        ),
        migrations.CreateModel(
            name='CryptoPortfolioSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('report_currency', models.CharField(default='USD', max_length=10)),
                ('last_rates_sync_at', models.DateTimeField(blank=True, null=True)),
                ('last_tx_sync_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('bybit_connection', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='crypto_portfolios', to='transactions.bybitconnection')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='crypto_settings', to='auth.user')),
            ],
        ),
        migrations.CreateModel(
            name='CryptoTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tx_type', models.CharField(choices=[('buy', 'Покупка'), ('sell', 'Продажа')], default='buy', max_length=8)),
                ('occurred_at', models.DateTimeField()),
                ('quantity', models.DecimalField(decimal_places=8, max_digits=24)),
                ('price_quote', models.DecimalField(decimal_places=8, max_digits=24)),
                ('total_quote', models.DecimalField(decimal_places=8, max_digits=24)),
                ('fee_quote', models.DecimalField(decimal_places=8, default=Decimal('0'), max_digits=24)),
                ('note', models.TextField(blank=True, default='')),
                ('source', models.CharField(choices=[('manual', 'Вручную'), ('bybit_convert', 'Bybit Convert'), ('bybit_trade', 'Bybit Trade')], default='manual', max_length=16)),
                ('external_key', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('asset', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='transactions', to='crypto.cryptoasset')),
                ('bybit_event', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='crypto_transaction', to='transactions.bybitexternalevent')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='crypto_transactions', to='auth.user')),
            ],
            options={
                'ordering': ['-occurred_at', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='cryptoasset',
            constraint=models.UniqueConstraint(fields=('user', 'symbol'), name='uniq_crypto_asset_user_symbol'),
        ),
        migrations.AddConstraint(
            model_name='cryptotransaction',
            constraint=models.UniqueConstraint(condition=~models.Q(external_key=''), fields=('user', 'external_key'), name='uniq_crypto_tx_user_external_key'),
        ),
    ]
