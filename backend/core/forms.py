from datetime import date

from django import forms
from django.utils import timezone
from core.models import Project, Category, Subcategory, ExpenseLink, Account, Currency, AccountBalanceSnapshot

class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ['name', 'description']

class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name']

class SubcategoryForm(forms.ModelForm):
    class Meta:
        model = Subcategory
        fields = ['name']


class BootstrapModelForm(forms.ModelForm):
    """Assigns Bootstrap classes to widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs['class'] = 'form-select'
            else:
                css_classes = widget.attrs.get('class', '')
                widget.attrs['class'] = f"{css_classes} form-control".strip()


class AccountForm(BootstrapModelForm):
    currency = forms.ChoiceField(label='Валюта')
    status = forms.ChoiceField(
        label='Статус',
        choices=[
            ('active', 'Активен'),
            ('archived', 'В архиве'),
        ],
    )

    class Meta:
        model = Account
        fields = [
            'name',
            'account_type',
            'currency',
            'status',
            'include_in_total',
            'show_in_expenses',
            'credit_limit',
            'account_target',
            'total_debt',
        ]
        labels = {
            'name': 'Название',
            'account_type': 'Тип счёта',
            'status': 'Статус',
            'include_in_total': 'Учитывать в общем балансе',
            'show_in_expenses': 'Показывать в расходах',
            'credit_limit': 'Кредитный лимит',
            'account_target': 'Целевая сумма',
            'total_debt': 'Текущий долг',
        }
        widgets = {
            'credit_limit': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'account_target': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'total_debt': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        currencies = Currency.objects.filter(status='active').order_by('code')
        choices = [(c.code, f"{c.code} — {c.name}") for c in currencies]
        self.fields['currency'].choices = choices
        if not self.is_bound and choices and 'currency' not in self.initial:
            preferred = next((code for code, _ in choices if code == 'RUB'), choices[0][0])
            self.initial['currency'] = preferred


class AccountBalanceSnapshotForm(BootstrapModelForm):
    class Meta:
        model = AccountBalanceSnapshot
        fields = ['snapshot_date', 'balance', 'note']
        labels = {
            'snapshot_date': 'Дата баланса',
            'balance': 'Баланс на конец дня',
            'note': 'Комментарий',
        }
        widgets = {
            'snapshot_date': forms.DateInput(attrs={'type': 'date'}),
            'balance': forms.NumberInput(attrs={'step': '0.00000001'}),
            'note': forms.TextInput(attrs={'placeholder': 'Например: стартовая точка или сверка с банком'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and 'snapshot_date' not in self.initial:
            self.initial['snapshot_date'] = timezone.localdate()


class CurrencyRateSyncForm(forms.Form):
    start_date = forms.DateField(
        label='Дата с',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        initial=date(2020, 1, 1),
    )
    end_date = forms.DateField(
        label='Дата по',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault('end_date', timezone.localdate())

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get('start_date')
        end_date = cleaned.get('end_date')
        if start_date and end_date and start_date > end_date:
            raise forms.ValidationError('Дата начала не может быть позже даты окончания.')
        return cleaned


class CurrencyConverterForm(forms.Form):
    amount = forms.DecimalField(
        label='Сумма',
        decimal_places=8,
        max_digits=20,
        widget=forms.NumberInput(attrs={'step': '0.00000001', 'class': 'form-control'}),
    )
    from_currency = forms.ChoiceField(label='Из валюты')
    to_currency = forms.ChoiceField(label='В валюту')
    rate_date = forms.DateField(
        label='Дата курса',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )

    def __init__(self, *args, **kwargs):
        currency_choices = kwargs.pop('currency_choices', [])
        super().__init__(*args, **kwargs)
        self.fields['from_currency'].choices = currency_choices
        self.fields['to_currency'].choices = currency_choices
        self.fields['from_currency'].widget.attrs['class'] = 'form-select js-currency-select'
        self.fields['to_currency'].widget.attrs['class'] = 'form-select js-currency-select'
        if not self.is_bound:
            self.initial.setdefault('rate_date', timezone.localdate())
            self.initial.setdefault('from_currency', 'USD')
            self.initial.setdefault('to_currency', 'RUB')
