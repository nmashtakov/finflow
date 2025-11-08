from django import forms
from core.models import Project, Category, Subcategory, ExpenseLink, Account, Currency

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

    class Meta:
        model = Account
        fields = [
            'name',
            'account_type',
            'currency',
            'include_in_total',
            'show_in_expenses',
            'credit_limit',
            'account_target',
            'total_debt',
        ]
        labels = {
            'name': 'Название',
            'account_type': 'Тип счёта',
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
