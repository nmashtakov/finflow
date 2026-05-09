from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0008_alter_accountbalancesnapshot_balance'),
    ]

    operations = [
        migrations.CreateModel(
            name='MonthlyBudgetPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('base_month', models.DateField()),
                ('plan_month', models.DateField()),
                ('currency', models.CharField(default='RUB', max_length=10)),
                ('distribution_window', models.PositiveSmallIntegerField(default=6)),
                ('forecast_income', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('forecast_expense', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('forecast_regular', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('forecast_risk', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('planned_total', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('planned_balance', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='monthly_budget_plans', to='core.project')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='monthly_budget_plans', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-plan_month', '-updated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='MonthlyBudgetLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category_name', models.CharField(max_length=150)),
                ('expense_type', models.CharField(choices=[('regular', 'Регулярная'), ('risk', 'Рисковая')], max_length=16)),
                ('avg_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('share_pct', models.DecimalField(decimal_places=2, default=0, max_digits=7)),
                ('suggested_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('planned_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='forecasting.monthlybudgetplan')),
            ],
            options={
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='monthlybudgetplan',
            index=models.Index(fields=['user', 'plan_month'], name='budget_user_month_idx'),
        ),
        migrations.AddIndex(
            model_name='monthlybudgetplan',
            index=models.Index(fields=['user', 'project', 'plan_month'], name='budget_user_project_month_idx'),
        ),
        migrations.AddConstraint(
            model_name='monthlybudgetline',
            constraint=models.UniqueConstraint(fields=('plan', 'category_name'), name='uniq_budget_plan_category'),
        ),
    ]
