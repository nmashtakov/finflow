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
    parent_transaction = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='split_children',
    )
    is_split_parent = models.BooleanField(default=False)
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


class UserPreferences(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='preferences')
    default_account = models.ForeignKey(
        'core.Account',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='preferred_by_users'
    )
    default_project = models.ForeignKey(
        'core.Project',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='preferred_by_users'
    )

    def __str__(self):
        return f"Настройки пользователя {self.user.username}"


class TransactionLinkGroup(models.Model):
    class LinkType(models.TextChoices):
        REIMBURSEMENT = ("reimbursement", "Возврат")
        TRANSFER_PAIR = ("transfer_pair", "Перевод между счетами")
        EXCHANGE_PAIR = ("exchange_pair", "Обмен")

    class Status(models.TextChoices):
        ACTIVE = ("active", "Активна")
        ARCHIVED = ("archived", "В архиве")

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transaction_link_groups")
    link_type = models.CharField(max_length=32, choices=LinkType.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_link_type_display()} #{self.id}"


class TransactionLinkItem(models.Model):
    class Role(models.TextChoices):
        PRIMARY = ("primary", "Основная")
        OFFSET = ("offset", "Компенсация")
        OUTGOING = ("outgoing", "Исходящая")
        INCOMING = ("incoming", "Входящая")

    group = models.ForeignKey(TransactionLinkGroup, on_delete=models.CASCADE, related_name="items")
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="link_items")
    role = models.CharField(max_length=16, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["group", "transaction"], name="uniq_group_transaction_item"),
            models.UniqueConstraint(fields=["transaction"], name="uniq_transaction_link_item"),
        ]

    def __str__(self):
        return f"{self.group_id}: {self.transaction_id} ({self.role})"


class AccountBalanceSnapshot(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='balance_snapshots')
    snapshot_date = models.DateField()
    balance = models.DecimalField(max_digits=20, decimal_places=8)
    note = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-snapshot_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['account', 'snapshot_date'], name='uniq_account_snapshot_date'),
        ]

    def __str__(self):
        return f"{self.account} @ {self.snapshot_date}: {self.balance}"
