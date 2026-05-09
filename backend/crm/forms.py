from django import forms

from core.forms import BootstrapModelForm
from core.models import Currency

from .models import Organization


DEFAULT_CURRENCY_CHOICES = [
    ('RUB', 'RUB'),
    ('USD', 'USD'),
    ('EUR', 'EUR'),
]


class OrganizationOnboardingForm(BootstrapModelForm):
    base_currency = forms.ChoiceField(label='Основная валюта')

    class Meta:
        model = Organization
        fields = ['name', 'base_currency']
        labels = {
            'name': 'Название организации',
        }
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'LEGO бизнес'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        currency_choices = list(
            Currency.objects.filter(status='active')
            .order_by('code')
            .values_list('code', 'name')
        )
        if currency_choices:
            self.fields['base_currency'].choices = [
                (code, f'{code} — {name}') for code, name in currency_choices
            ]
        else:
            self.fields['base_currency'].choices = DEFAULT_CURRENCY_CHOICES

        if not self.is_bound:
            self.initial.setdefault('name', 'LEGO бизнес')
            self.initial.setdefault('base_currency', 'RUB')
