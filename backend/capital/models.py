import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models


STEAM_ACCOUNT_CODE_RE = re.compile(r'^[A-Za-z]{2}\d+$')


class WithdrawalSite(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='withdrawal_sites')
    name = models.CharField(max_length=64)
    default_coefficient = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('1'))
    currency = models.CharField(max_length=10, default='USD')
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name', 'id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='uniq_withdrawal_site_user_name'),
        ]

    def __str__(self):
        return self.name


class SteamAccount(models.Model):
    class Purpose(models.TextChoices):
        INVESTMENT = ('investment', 'Инвестиционный')
        TRADE = ('trade', 'Для трейда')
        EMPTY = ('empty', 'Пустой')

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='steam_accounts')
    project = models.ForeignKey(
        'core.Project',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='steam_accounts',
    )
    account_code = models.CharField(max_length=16)
    login = models.CharField(max_length=128, blank=True, default='')
    phone = models.CharField(max_length=32, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    currency = models.CharField(max_length=10, default='USD')
    steam_id = models.CharField(max_length=64, blank=True, default='')
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.INVESTMENT)
    note = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account_code', 'id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'account_code'], name='uniq_steam_account_user_code'),
        ]

    def __str__(self):
        return self.account_code

    def clean(self):
        super().clean()
        code = (self.account_code or '').strip()
        if not STEAM_ACCOUNT_CODE_RE.match(code):
            raise ValidationError({
                'account_code': 'Код аккаунта: 2 буквы + число (например MN2).',
            })
        self.account_code = code.upper()


class CapitalPosition(models.Model):
    class Kind(models.TextChoices):
        INSTRUMENT = ('instrument', 'Инструмент')
        BANK_LINK = ('bank_link', 'Банковский счёт')
        INVENTORY = ('inventory', 'Товарный остаток')
        STEAM = ('steam', 'Steam')

    class AssetClass(models.TextChoices):
        CRYPTO = ('crypto', 'Криптовалюта')
        STOCK_RU = ('stock_ru', 'Акции РФ')
        STOCK_FOREIGN = ('stock_foreign', 'Акции иностранные')
        ETF = ('etf', 'ETF')
        BOND = ('bond', 'Облигации')
        PRECIOUS_METAL = ('precious_metal', 'Драгметаллы')
        DIVIDEND = ('dividend', 'Дивиденды')

    class Status(models.TextChoices):
        ACTIVE = ('active', 'Активна')
        ARCHIVED = ('archived', 'В архиве')
        DELETED = ('deleted', 'Удалена')

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='capital_positions')
    project = models.ForeignKey(
        'core.Project',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='capital_positions',
    )
    name = models.CharField(max_length=128)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    asset_class = models.CharField(max_length=32, choices=AssetClass.choices, blank=True, default='')
    currency = models.CharField(max_length=10, default='RUB')
    linked_account = models.ForeignKey(
        'core.Account',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='capital_positions',
    )
    steam_account = models.ForeignKey(
        SteamAccount,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='capital_positions',
    )
    withdrawal_site = models.ForeignKey(
        WithdrawalSite,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='capital_positions',
    )
    include_in_total = models.BooleanField(default=True)
    is_liability = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['linked_account'],
                condition=models.Q(kind='bank_link', status='active'),
                name='uniq_active_bank_link_account',
            ),
            models.UniqueConstraint(
                fields=['steam_account', 'withdrawal_site'],
                condition=models.Q(kind='steam', status='active'),
                name='uniq_active_steam_site_pair',
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.kind == self.Kind.BANK_LINK and not self.linked_account_id:
            raise ValidationError({'linked_account': 'Для банковской позиции укажите счёт.'})
        if self.kind == self.Kind.STEAM:
            if not self.steam_account_id or not self.withdrawal_site_id:
                raise ValidationError('Для Steam-позиции укажите аккаунт и сайт.')
        if self.kind == self.Kind.INSTRUMENT and not self.asset_class:
            raise ValidationError({'asset_class': 'Укажите класс инструмента.'})


class CapitalSnapshot(models.Model):
    position = models.ForeignKey(CapitalPosition, on_delete=models.CASCADE, related_name='snapshots')
    snapshot_date = models.DateField()
    quantity = models.DecimalField(max_digits=20, decimal_places=8, blank=True, null=True)
    unit_price = models.DecimalField(max_digits=20, decimal_places=8, blank=True, null=True)
    site_balance = models.DecimalField(max_digits=20, decimal_places=8, blank=True, null=True)
    skins_value = models.DecimalField(max_digits=20, decimal_places=8, blank=True, null=True)
    coefficient = models.DecimalField(max_digits=8, decimal_places=4, blank=True, null=True)
    total_value = models.DecimalField(max_digits=20, decimal_places=8)
    note = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-snapshot_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['position', 'snapshot_date'], name='uniq_capital_snapshot_date'),
        ]

    def __str__(self):
        return f'{self.position} @ {self.snapshot_date}: {self.total_value}'

    def compute_total_value(self):
        position = self.position
        if position.kind == CapitalPosition.Kind.INSTRUMENT:
            if self.quantity is not None and self.unit_price is not None:
                return self.quantity * self.unit_price
            return self.total_value
        if position.kind == CapitalPosition.Kind.INVENTORY:
            return self.total_value
        if position.kind == CapitalPosition.Kind.STEAM:
            balance = self.site_balance or Decimal('0')
            skins = self.skins_value or Decimal('0')
            coeff = self.coefficient
            if coeff is None and position.withdrawal_site_id:
                coeff = position.withdrawal_site.default_coefficient
            if coeff is None:
                coeff = Decimal('1')
            return (balance + skins) * coeff
        return self.total_value

    def save(self, *args, **kwargs):
        if self.position.kind != CapitalPosition.Kind.BANK_LINK:
            self.total_value = self.compute_total_value()
        super().save(*args, **kwargs)
