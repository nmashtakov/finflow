from django.db import migrations, models


def seed_currencies(apps, schema_editor):
    Currency = apps.get_model('core', 'Currency')
    initial_data = [
        ('RUB', 'Российский рубль'),
        ('USD', 'Доллар США'),
        ('CNY', 'Китайский юань'),
        ('KZT', 'Казахстанский тенге'),
    ]

    for code, name in initial_data:
        Currency.objects.update_or_create(
            code=code,
            defaults={'name': name, 'status': 'active'},
        )


def remove_currencies(apps, schema_editor):
    Currency = apps.get_model('core', 'Currency')
    Currency.objects.filter(code__in=['RUB', 'USD', 'CNY', 'KZT']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Currency',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=8, unique=True)),
                ('name', models.CharField(max_length=64)),
                ('status', models.CharField(choices=[('active', 'Активна'), ('archived', 'В архиве')], default='active', max_length=16)),
            ],
            options={
                'ordering': ['code'],
            },
        ),
        migrations.RunPython(seed_currencies, remove_currencies),
    ]
