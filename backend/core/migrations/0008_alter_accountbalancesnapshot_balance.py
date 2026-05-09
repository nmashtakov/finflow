from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_accountbalancesnapshot'),
    ]

    operations = [
        migrations.AlterField(
            model_name='accountbalancesnapshot',
            name='balance',
            field=models.DecimalField(decimal_places=8, max_digits=20),
        ),
    ]
