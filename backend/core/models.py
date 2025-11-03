from django.db import models
from django.contrib.auth.models import User

# === PROJECT ===
class Project(models.Model):
    STATUS_CHOICES = (
        ('active', 'Активен'),
        ('archived', 'В архиве'),
        ('deleted', 'Удалён'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')

    def __str__(self):
        return self.name

# === CATEGORY ===
class Category(models.Model):
    STATUS_CHOICES = (
        ('active', 'Активна'),
        ('archived', 'В архиве'),
        ('deleted', 'Удалена'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')

    def __str__(self):
        return self.name

# === SUBCATEGORY ===
class Subcategory(models.Model):
    STATUS_CHOICES = (
        ('active', 'Активна'),
        ('archived', 'В архиве'),
        ('deleted', 'Удалена'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')

    def __str__(self):
        return self.name

# === ACCOUNT ===
class Account(models.Model):
    ACCOUNT_TYPE_CHOICES = (
        ('normal', 'Обычный'),
        ('debt', 'Долговой'),
        ('saving', 'Сберегательный'),
    )
    STATUS_CHOICES = (
        ('active', 'Активен'),
        ('archived', 'В архиве'),
        ('deleted', 'Удалён'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=16, choices=ACCOUNT_TYPE_CHOICES, default='normal')
    currency = models.CharField(max_length=10, default='RUB')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')
    include_in_total = models.BooleanField(default=True, verbose_name="Учитывать в общем балансе")
    show_in_expenses = models.BooleanField(default=True, verbose_name="Показывать в расходах")
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    account_target = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_debt = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    def __str__(self):
        return self.name

# === EXPENSE LINK ===
class ExpenseLink(models.Model):
    STATUS_CHOICES = (
        ('active', 'Активен'),
        ('archived', 'В архиве'),
        ('deleted', 'Удалён'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    subcategory = models.ForeignKey(Subcategory, on_delete=models.SET_NULL, blank=True, null=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')

    def __str__(self):
        return f"{self.project} — {self.category}" + (f" — {self.subcategory}" if self.subcategory else "")

# === TRANSACTION ===
class Transaction(models.Model):
    TRANSACTION_TYPE_CHOICES = (
        ('income', 'Доход'),
        ('expense', 'Расход'),
        ('transfer', 'Перевод'),
    )
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    expense_link = models.ForeignKey(ExpenseLink, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default='RUB')
    date = models.DateTimeField()
    transaction_type = models.CharField(max_length=16, choices=TRANSACTION_TYPE_CHOICES)
    comment = models.TextField(blank=True, null=True)
    related_transaction = models.ForeignKey('self', on_delete=models.SET_NULL, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.transaction_type}: {self.amount} {self.currency}"

# === CURRENCY RATE ===
class CurrencyRate(models.Model):
    date = models.DateField()
    currency = models.CharField(max_length=10)
    amount = models.DecimalField(max_digits=14, decimal_places=6)  # Сколько рублей за 1 единицу валюты

    def __str__(self):
        return f"{self.currency} на {self.date}: {self.amount}"
# === CURRENCY DIRECTORY ===
class Currency(models.Model):
    STATUS_CHOICES = (
        ('active', 'Активна'),
        ('archived', 'В архиве'),
    )
    code = models.CharField(max_length=8, unique=True)
    name = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f"{self.code} — {self.name}"
