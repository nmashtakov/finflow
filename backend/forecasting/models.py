from django.conf import settings
from django.db import models

from core.models import Project


class MonthlyBudgetPlan(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='monthly_budget_plans',
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='monthly_budget_plans',
    )
    base_month = models.DateField()
    plan_month = models.DateField()
    currency = models.CharField(max_length=10, default='RUB')
    distribution_window = models.PositiveSmallIntegerField(default=6)
    forecast_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    forecast_expense = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    forecast_regular = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    forecast_risk = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    planned_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    planned_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-plan_month', '-updated_at', '-id']
        indexes = [
            models.Index(fields=['user', 'plan_month'], name='budget_user_month_idx'),
            models.Index(fields=['user', 'project', 'plan_month'], name='budget_user_project_month_idx'),
        ]

    def __str__(self):
        project_name = self.project.name if self.project_id else 'Все проекты'
        return f'Бюджет {self.plan_month:%Y-%m} · {project_name}'


class MonthlyBudgetLine(models.Model):
    class ExpenseType(models.TextChoices):
        REGULAR = ('regular', 'Регулярная')
        RISK = ('risk', 'Рисковая')

    plan = models.ForeignKey(
        MonthlyBudgetPlan,
        on_delete=models.CASCADE,
        related_name='lines',
    )
    category_name = models.CharField(max_length=150)
    expense_type = models.CharField(max_length=16, choices=ExpenseType.choices)
    avg_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    share_pct = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    suggested_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    planned_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['plan', 'category_name'], name='uniq_budget_plan_category'),
        ]

    def __str__(self):
        return f'{self.plan_id}: {self.category_name}'
