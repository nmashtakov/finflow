import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0008_alter_accountbalancesnapshot_balance'),
    ]

    operations = [
        migrations.CreateModel(
            name='WithdrawalSite',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=64)),
                ('default_coefficient', models.DecimalField(decimal_places=4, default=Decimal('1'), max_digits=8)),
                ('currency', models.CharField(default='USD', max_length=10)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='withdrawal_sites', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['sort_order', 'name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='SteamAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account_code', models.CharField(max_length=16)),
                ('login', models.CharField(blank=True, default='', max_length=128)),
                ('phone', models.CharField(blank=True, default='', max_length=32)),
                ('email', models.EmailField(blank=True, default='', max_length=254)),
                ('currency', models.CharField(default='USD', max_length=10)),
                ('steam_id', models.CharField(blank=True, default='', max_length=64)),
                ('purpose', models.CharField(choices=[('investment', 'Инвестиционный'), ('trade', 'Для трейда'), ('empty', 'Пустой')], default='investment', max_length=16)),
                ('note', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='steam_accounts', to='core.project')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steam_accounts', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['account_code', 'id'],
            },
        ),
        migrations.CreateModel(
            name='CapitalPosition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('kind', models.CharField(choices=[('instrument', 'Инструмент'), ('bank_link', 'Банковский счёт'), ('inventory', 'Товарный остаток'), ('steam', 'Steam')], max_length=16)),
                ('asset_class', models.CharField(blank=True, choices=[('crypto', 'Криптовалюта'), ('stock_ru', 'Акции РФ'), ('stock_foreign', 'Акции иностранные'), ('etf', 'ETF'), ('bond', 'Облигации'), ('precious_metal', 'Драгметаллы'), ('dividend', 'Дивиденды')], default='', max_length=32)),
                ('currency', models.CharField(default='RUB', max_length=10)),
                ('include_in_total', models.BooleanField(default=True)),
                ('is_liability', models.BooleanField(default=False)),
                ('sort_order', models.IntegerField(default=0)),
                ('status', models.CharField(choices=[('active', 'Активна'), ('archived', 'В архиве'), ('deleted', 'Удалена')], default='active', max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('linked_account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='capital_positions', to='core.account')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='capital_positions', to='core.project')),
                ('steam_account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='capital_positions', to='capital.steamaccount')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='capital_positions', to=settings.AUTH_USER_MODEL)),
                ('withdrawal_site', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='capital_positions', to='capital.withdrawalsite')),
            ],
            options={
                'ordering': ['sort_order', 'name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='CapitalSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('snapshot_date', models.DateField()),
                ('quantity', models.DecimalField(blank=True, decimal_places=8, max_digits=20, null=True)),
                ('unit_price', models.DecimalField(blank=True, decimal_places=8, max_digits=20, null=True)),
                ('site_balance', models.DecimalField(blank=True, decimal_places=8, max_digits=20, null=True)),
                ('skins_value', models.DecimalField(blank=True, decimal_places=8, max_digits=20, null=True)),
                ('coefficient', models.DecimalField(blank=True, decimal_places=4, max_digits=8, null=True)),
                ('total_value', models.DecimalField(decimal_places=8, max_digits=20)),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('position', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='snapshots', to='capital.capitalposition')),
            ],
            options={
                'ordering': ['-snapshot_date', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='withdrawalsite',
            constraint=models.UniqueConstraint(fields=('user', 'name'), name='uniq_withdrawal_site_user_name'),
        ),
        migrations.AddConstraint(
            model_name='steamaccount',
            constraint=models.UniqueConstraint(fields=('user', 'account_code'), name='uniq_steam_account_user_code'),
        ),
        migrations.AddConstraint(
            model_name='capitalposition',
            constraint=models.UniqueConstraint(condition=models.Q(('kind', 'bank_link'), ('status', 'active')), fields=('linked_account',), name='uniq_active_bank_link_account'),
        ),
        migrations.AddConstraint(
            model_name='capitalposition',
            constraint=models.UniqueConstraint(condition=models.Q(('kind', 'steam'), ('status', 'active')), fields=('steam_account', 'withdrawal_site'), name='uniq_active_steam_site_pair'),
        ),
        migrations.AddConstraint(
            model_name='capitalsnapshot',
            constraint=models.UniqueConstraint(fields=('position', 'snapshot_date'), name='uniq_capital_snapshot_date'),
        ),
    ]
