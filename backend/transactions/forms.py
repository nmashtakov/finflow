from django import forms
from django.utils import timezone
from core.models import (
    Account,
    Project,
    Category,
    Subcategory,
    ExpenseLink,
    Currency,
    Transaction,
)
from .models import BybitConnection


class TransactionImportUploadForm(forms.Form):
    BANK_CHOICES = [
        ('auto', 'Определить автоматически'),
        ('tinkoff', 'Тинькофф'),
        ('alfa', 'Альфа-Банк'),
        ('t_business', 'Т Бизнес'),
        ('other', 'Другое'),
    ]
    file = forms.FileField(
        label='Файл с транзакциями',
        help_text='Поддерживаются CSV (разделитель , или ;) и Excel (.xlsx).',
        widget=forms.FileInput(attrs={'class': 'form-control'}),
    )
    bank_preset = forms.ChoiceField(
        label='Тип выгрузки',
        choices=BANK_CHOICES,
        initial='auto',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    include_tinkoff_invest_rounding = forms.BooleanField(
        label='Учитывать пополнение Инвесткопилки',
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )


class TinkoffImportAccountForm(forms.Form):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        account_choices = Account.objects.filter(user=user, status='active').order_by('name') if user else Account.objects.none()
        currency_choices = [
            (c.code, f"{c.code} — {c.name}")
            for c in Currency.objects.filter(status='active').order_by('code')
        ]
        self.fields['default_account'] = forms.ModelChoiceField(
            queryset=account_choices,
            required=False,
            label='Счёт для всей выгрузки'
        )
        self.fields['default_account_name'] = forms.CharField(
            label='Или создать новый счёт',
            required=False
        )
        self.fields['default_currency'] = forms.ChoiceField(
            label='Валюта по умолчанию',
            choices=currency_choices,
            initial='RUB' if any(code == 'RUB' for code, _ in currency_choices) else (currency_choices[0][0] if currency_choices else ''),
            required=False,
        )
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault('class', 'form-select')
            else:
                field.widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('default_account') and not cleaned.get('default_account_name'):
            raise forms.ValidationError('Выберите существующий счёт или укажите название нового.')
        return cleaned


class TransactionImportMappingForm(forms.Form):
    def __init__(self, *args, columns=None, user=None, preset_initial=None, **kwargs):
        super().__init__(*args, **kwargs)
        columns = columns or []
        currency_choices = [
            (c.code, f"{c.code} — {c.name}")
            for c in Currency.objects.filter(status='active').order_by('code')
        ]
        required_choices = [('', '— Выберите колонку —')] + [(col, col) for col in columns]
        optional_choices = [('', '— Не использовать —')] + [(col, col) for col in columns]

        self.fields['column_date'] = forms.ChoiceField(label='Дата операции', choices=required_choices)
        self.fields['column_amount'] = forms.ChoiceField(label='Сумма', choices=required_choices)
        self.fields['column_currency'] = forms.ChoiceField(label='Валюта', choices=optional_choices, required=False)
        self.fields['default_currency'] = forms.ChoiceField(
            label='Валюта по умолчанию',
            choices=currency_choices,
            initial='RUB' if any(code == 'RUB' for code, _ in currency_choices) else (currency_choices[0][0] if currency_choices else ''),
            required=False,
        )

        self.fields['column_type'] = forms.ChoiceField(label='Колонка типа операции (доход/расход)', choices=optional_choices, required=False)
        self.fields['income_markers'] = forms.CharField(
            label='Значения для доходов',
            required=False,
            initial='доход,поступление,пополнение,кредит',
            help_text='Через запятую; сравнение без учёта регистра.'
        )
        self.fields['expense_markers'] = forms.CharField(
            label='Значения для расходов',
            required=False,
            initial='расход,списание,платёж,платеж,дебет',
            help_text='Через запятую; сравнение без учёта регистра.'
        )

        account_choices = Account.objects.filter(user=user, status='active').order_by('name') if user else Account.objects.none()
        project_choices = Project.objects.filter(user=user, status='active').order_by('name') if user else Project.objects.none()

        self.fields['column_account'] = forms.ChoiceField(label='Колонка со счётом', choices=optional_choices, required=False)
        self.fields['default_account'] = forms.ModelChoiceField(
            queryset=account_choices,
            required=False,
            label='Счёт по умолчанию'
        )
        self.fields['default_account_name'] = forms.CharField(
            label='Название счёта (создать, если не найден)',
            required=False
        )

        self.fields['column_project'] = forms.ChoiceField(label='Колонка с проектом', choices=optional_choices, required=False)
        self.fields['default_project'] = forms.ModelChoiceField(
            queryset=project_choices,
            required=False,
            label='Проект по умолчанию'
        )
        self.fields['default_project_name'] = forms.CharField(
            label='Название проекта (создать, если не найден)',
            required=False
        )

        self.fields['column_category'] = forms.ChoiceField(label='Колонка с категорией', choices=optional_choices, required=False)
        self.fields['column_subcategory'] = forms.ChoiceField(label='Колонка с подкатегорией', choices=optional_choices, required=False)

        self.fields['column_comment'] = forms.ChoiceField(label='Колонка с комментарием', choices=optional_choices, required=False)
        self.fields['default_comment'] = forms.CharField(label='Комментарий по умолчанию', required=False)

        initial = preset_initial or {}
        for name, value in initial.items():
            if name in self.fields and not self.initial.get(name):
                self.initial[name] = value

        for field_name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault('class', 'form-select')
            else:
                widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        cleaned = super().clean()

        if not cleaned.get('column_account') and not cleaned.get('default_account') and not cleaned.get('default_account_name'):
            raise forms.ValidationError('Укажите колонку со счётом или задайте счёт по умолчанию.')

        if not cleaned.get('column_project') and not cleaned.get('default_project') and not cleaned.get('default_project_name'):
            raise forms.ValidationError('Укажите колонку с проектом или задайте проект по умолчанию.')

        return cleaned


class TransactionForm(forms.ModelForm):
    class ExpenseLinkChoiceField(forms.ModelChoiceField):
        def label_from_instance(self, obj):
            subcategory = f" -> {obj.subcategory.name}" if obj.subcategory else ""
            return f"{obj.project.name} -> {obj.category.name}{subcategory}"

    date = forms.DateTimeField(
        label='Дата и время',
        widget=forms.DateTimeInput(
            attrs={'type': 'datetime-local'}
        ),
        initial=lambda: timezone.now().strftime('%Y-%m-%dT%H:%M'),
    )
    amount = forms.DecimalField(label='Сумма', max_digits=14, decimal_places=2)
    currency = forms.ChoiceField(label='Валюта')
    account = forms.ModelChoiceField(label='Счёт', queryset=Account.objects.none())
    expense_link = ExpenseLinkChoiceField(
        label='Категория',
        queryset=ExpenseLink.objects.none(),
    )
    comment = forms.CharField(
        label='Комментарий',
        required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
    )

    class Meta:
        model = Transaction
        fields = [
            'date',
            'amount',
            'currency',
            'account',
            'expense_link',
            'comment',
        ]

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault('class', 'form-select')
            else:
                css_classes = widget.attrs.get('class', '')
                widget.attrs['class'] = f"{css_classes} form-control".strip()

        self.fields['account'].queryset = Account.objects.filter(user=user, status='active').order_by('name')
        self.fields['expense_link'].queryset = (
            ExpenseLink.objects.filter(
                user=user,
                status='active',
                project__status='active',
                category__status='active',
            )
            .select_related('project', 'category', 'subcategory')
            .order_by('project__name', 'category__name', 'subcategory__name')
        )

        currencies = Currency.objects.filter(status='active').order_by('code')
        self.fields['currency'].choices = [(c.code, f"{c.code} — {c.name}") for c in currencies]
        if not self.is_bound and self.fields['currency'].choices and 'currency' not in self.initial:
            preferred = next((code for code, _ in self.fields['currency'].choices if code == 'RUB'),
                             self.fields['currency'].choices[0][0])
            self.initial['currency'] = preferred

        if not self.initial.get('date'):
            self.initial['date'] = timezone.now().strftime('%Y-%m-%dT%H:%M')

    def clean_account(self):
        account = self.cleaned_data['account']
        if account.user != self.user or account.status != 'active':
            raise forms.ValidationError('Счёт недоступен.')
        return account

    def clean_expense_link(self):
        expense_link = self.cleaned_data['expense_link']
        if (
            expense_link.user != self.user
            or expense_link.status != 'active'
            or expense_link.project.status != 'active'
            or expense_link.category.status != 'active'
        ):
            raise forms.ValidationError('Категория недоступна.')
        return expense_link

    def save(self, commit=True):
        instance = super().save(commit=False)
        expense_link = self.cleaned_data.get('expense_link')
        if expense_link:
            instance.expense_link = expense_link

        # Определяем тип транзакции по знаку суммы
        instance.transaction_type = 'income' if instance.amount > 0 else 'expense'

        if instance.date is None:
            instance.date = timezone.now()
        elif timezone.is_naive(instance.date):
            instance.date = timezone.make_aware(instance.date, timezone.get_current_timezone())

        if commit:
            instance.save()
        return instance


class BybitConnectionForm(forms.ModelForm):
    class Meta:
        model = BybitConnection
        fields = ["name", "api_key", "api_secret", "is_testnet", "is_active"]
        labels = {
            "name": "Название подключения",
            "api_key": "API Key",
            "api_secret": "API Secret",
            "is_testnet": "Testnet",
            "is_active": "Активно",
        }
        widgets = {
            "api_secret": forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            # Не подставляем секрет в HTML при редактировании.
            self.fields["api_secret"].required = False
            self.fields["api_key"].required = False
            self.initial["api_key"] = ""
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", "form-select")
            else:
                css_classes = widget.attrs.get("class", "")
                widget.attrs["class"] = f"{css_classes} form-control".strip()

    def clean_api_secret(self):
        secret = (self.cleaned_data.get("api_secret") or "").strip()
        if secret:
            return secret
        if self.instance and self.instance.pk:
            # Если поле пустое при редактировании, оставляем старый секрет.
            return self.instance.api_secret
        raise forms.ValidationError("API Secret обязателен.")

    def clean_api_key(self):
        key = (self.cleaned_data.get("api_key") or "").strip()
        if key:
            return key
        if self.instance and self.instance.pk:
            return self.instance.api_key
        raise forms.ValidationError("API Key обязателен.")
