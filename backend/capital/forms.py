from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from capital.models import STEAM_ACCOUNT_CODE_RE, CapitalPosition, CapitalSnapshot, SteamAccount, WithdrawalSite
from core.forms import BootstrapModelForm
from core.models import Account, Project


class WithdrawalSiteForm(BootstrapModelForm):
    class Meta:
        model = WithdrawalSite
        fields = ['name', 'default_coefficient', 'currency', 'is_active', 'sort_order']
        labels = {
            'name': 'Название сайта',
            'default_coefficient': 'Коэффициент по умолчанию',
            'currency': 'Валюта',
            'is_active': 'Активен',
            'sort_order': 'Порядок',
        }
        widgets = {
            'default_coefficient': forms.NumberInput(attrs={'step': '0.0001', 'min': '0'}),
        }


class SteamAccountForm(BootstrapModelForm):
    class Meta:
        model = SteamAccount
        fields = [
            'project',
            'account_code',
            'login',
            'phone',
            'email',
            'currency',
            'steam_id',
            'purpose',
            'note',
            'is_active',
        ]
        labels = {
            'project': 'Проект',
            'account_code': 'Код (2 буквы + число)',
            'login': 'Логин',
            'phone': 'Телефон',
            'email': 'Почта',
            'currency': 'Валюта аккаунта',
            'steam_id': 'Steam ID',
            'purpose': 'Назначение',
            'note': 'Заметка',
            'is_active': 'Активен',
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user:
            self.fields['project'].queryset = Project.objects.filter(user=user, status='active').order_by('name')

    def clean_account_code(self):
        code = (self.cleaned_data.get('account_code') or '').strip().upper()
        if not STEAM_ACCOUNT_CODE_RE.match(code):
            raise ValidationError('Код аккаунта: 2 буквы + число (например MN2).')
        qs = SteamAccount.objects.filter(user=self.user, account_code__iexact=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError('Аккаунт с таким кодом уже существует.')
        return code


class CapitalPositionForm(BootstrapModelForm):
    class Meta:
        model = CapitalPosition
        fields = [
            'project',
            'name',
            'kind',
            'asset_class',
            'currency',
            'linked_account',
            'steam_account',
            'withdrawal_site',
            'include_in_total',
            'is_liability',
            'sort_order',
            'status',
        ]
        labels = {
            'project': 'Проект',
            'name': 'Название',
            'kind': 'Тип',
            'asset_class': 'Класс инструмента',
            'currency': 'Валюта',
            'linked_account': 'Банковский счёт',
            'steam_account': 'Steam-аккаунт',
            'withdrawal_site': 'Сайт вывода',
            'include_in_total': 'Учитывать в капитале',
            'is_liability': 'Обязательство (вычитается)',
            'sort_order': 'Порядок',
            'status': 'Статус',
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user:
            self.fields['project'].queryset = Project.objects.filter(user=user, status='active').order_by('name')
            self.fields['linked_account'].queryset = Account.objects.filter(
                user=user,
                status__in=['active', 'archived'],
            ).order_by('name')
            self.fields['steam_account'].queryset = SteamAccount.objects.filter(user=user, is_active=True).order_by('account_code')
            self.fields['withdrawal_site'].queryset = WithdrawalSite.objects.filter(user=user, is_active=True).order_by('sort_order', 'name')

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get('kind')
        if kind == CapitalPosition.Kind.BANK_LINK and not cleaned.get('linked_account'):
            self.add_error('linked_account', 'Укажите счёт.')
        if kind == CapitalPosition.Kind.STEAM:
            if not cleaned.get('steam_account'):
                self.add_error('steam_account', 'Укажите Steam-аккаунт.')
            if not cleaned.get('withdrawal_site'):
                self.add_error('withdrawal_site', 'Укажите сайт.')
        if kind == CapitalPosition.Kind.INSTRUMENT and not cleaned.get('asset_class'):
            self.add_error('asset_class', 'Укажите класс инструмента.')
        return cleaned


class CapitalSnapshotForm(BootstrapModelForm):
    class Meta:
        model = CapitalSnapshot
        fields = [
            'quantity',
            'unit_price',
            'site_balance',
            'skins_value',
            'coefficient',
            'total_value',
            'note',
        ]

    def __init__(self, *args, position=None, **kwargs):
        self.position = position
        super().__init__(*args, **kwargs)
        if not position:
            return
        kind = position.kind
        instrument_fields = {'quantity', 'unit_price', 'total_value'}
        steam_fields = {'site_balance', 'skins_value', 'coefficient', 'total_value'}
        inventory_fields = {'total_value'}
        for name in self.fields:
            field = self.fields[name]
            if kind == CapitalPosition.Kind.INSTRUMENT:
                field.required = name in {'quantity', 'unit_price'}
                if name not in instrument_fields | {'note'}:
                    field.widget = forms.HiddenInput()
            elif kind == CapitalPosition.Kind.STEAM:
                if name not in steam_fields | {'note'}:
                    field.widget = forms.HiddenInput()
            elif kind == CapitalPosition.Kind.INVENTORY:
                field.required = name == 'total_value'
                if name not in inventory_fields | {'note'}:
                    field.widget = forms.HiddenInput()
            else:
                for fname in self.fields:
                    self.fields[fname].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        if not self.position:
            return cleaned
        if self.position.kind == CapitalPosition.Kind.INSTRUMENT:
            qty = cleaned.get('quantity')
            price = cleaned.get('unit_price')
            if qty is not None and price is not None:
                cleaned['total_value'] = Decimal(qty) * Decimal(price)
            elif cleaned.get('total_value') is None:
                raise ValidationError('Укажите количество и цену или итоговую сумму.')
        if self.position.kind == CapitalPosition.Kind.INVENTORY and cleaned.get('total_value') is None:
            raise ValidationError('Укажите сумму остатка.')
        return cleaned
