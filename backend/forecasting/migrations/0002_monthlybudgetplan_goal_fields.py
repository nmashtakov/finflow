from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('forecasting', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='monthlybudgetplan',
            name='goal_balance_after_saving',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name='monthlybudgetplan',
            name='goal_current_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name='monthlybudgetplan',
            name='goal_months_left',
            field=models.PositiveSmallIntegerField(default=6),
        ),
        migrations.AddField(
            model_name='monthlybudgetplan',
            name='goal_required_monthly',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name='monthlybudgetplan',
            name='goal_target_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
    ]
