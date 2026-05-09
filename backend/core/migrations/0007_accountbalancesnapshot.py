from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_currency_directory_full'),
    ]

    operations = [
        migrations.CreateModel(
            name='AccountBalanceSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('snapshot_date', models.DateField()),
                ('balance', models.DecimalField(decimal_places=2, max_digits=14)),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='balance_snapshots', to='core.account')),
            ],
            options={
                'ordering': ['-snapshot_date', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='accountbalancesnapshot',
            constraint=models.UniqueConstraint(fields=('account', 'snapshot_date'), name='uniq_account_snapshot_date'),
        ),
    ]
