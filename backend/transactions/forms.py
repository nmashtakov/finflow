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


class TransactionImportUploadForm(forms.Form):
    BANK_CHOICES = [
        ('auto', 'Определить автоматически'),
        ('tinkoff', 'Тинькофф'),
        ('alfa', 'Альфа-Банк'),
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


class TransactionImportMappingForm(forms.Form):
    def __init__(self, *args, columns=None, user=None, preset_initial=None, **kwargs):
        super().__init__(*args, **kwargs)
        columns = columns or []
        required_choices = [('', '— Выберите колонку —')] + [(col, col) for col in columns]
        optional_choices = [('', '— Не использовать —')] + [(col, col) for col in columns]

        self.fields['column_date'] = forms.ChoiceField(label='Дата операции', choices=required_choices)
        self.fields['column_amount'] = forms.ChoiceField(label='Сумма', choices=required_choices)
        self.fields['column_currency'] = forms.ChoiceField(label='Валюта', choices=optional_choices, required=False)
        self.fields['default_currency'] = forms.CharField(label='Валюта по умолчанию', initial='RUB', required=False)

        self.fields['column_type'] = forms.ChoiceField(label='Колонка типа операции (доход/расход)', choices=optional_choices, required=False)
        self.fields['income_markers'] = forms.CharField(
            label='Значения для доходов',
            required=False,
            initial='доход,поступление,пополнение',
            help_text='Через запятую; сравнение без учёта регистра.'
        )
        self.fields['expense_markers'] = forms.CharField(
            label='Значения для расходов',
            required=False,
            initial='расход,списание,платёж,платеж',
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
        self.fields['default_category_name'] = forms.CharField(
            label='Категория по умолчанию',
            required=False,
            help_text='Будет создана, если отсутствует.'
        )

        self.fields['column_subcategory'] = forms.ChoiceField(label='Колонка с подкатегорией', choices=optional_choices, required=False)
        self.fields['default_subcategory_name'] = forms.CharField(
            label='Подкатегория по умолчанию',
            required=False,
            help_text='Оставьте пустым, чтобы не использовать подкатегории.'
        )

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

        if not cleaned.get('column_category') and not cleaned.get('default_category_name'):
            raise forms.ValidationError('Укажите колонку с категорией или задайте категорию по умолчанию.')

        return cleaned


class TransactionForm(forms.ModelForm):
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
    project = forms.ModelChoiceField(label='Проект', queryset=Project.objects.none())
    category = forms.ModelChoiceField(label='Категория', queryset=Category.objects.none())
    subcategory = forms.ModelChoiceField(
        label='Подкатегория',
        queryset=Subcategory.objects.none(),
        required=False,
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
        self.fields['project'].queryset = Project.objects.filter(user=user, status='active').order_by('name')
        self.fields['category'].queryset = Category.objects.filter(user=user, status='active').order_by('name')
        self.fields['subcategory'].queryset = Subcategory.objects.filter(user=user, status='active').order_by('name')

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

    def clean_project(self):
        project = self.cleaned_data['project']
        if project.user != self.user or project.status != 'active':
            raise forms.ValidationError('Проект недоступен.')
        return project

    def clean_category(self):
        category = self.cleaned_data['category']
        if category.user != self.user or category.status != 'active':
            raise forms.ValidationError('Категория недоступна.')
        return category

    def clean_subcategory(self):
        subcategory = self.cleaned_data.get('subcategory')
        if subcategory:
            if subcategory.user != self.user or subcategory.status != 'active':
                raise forms.ValidationError('Подкатегория недоступна.')
        return subcategory

    def clean(self):
        cleaned_data = super().clean()
        project = cleaned_data.get('project')
        category = cleaned_data.get('category')
        subcategory = cleaned_data.get('subcategory')

        if project and category:
            expense_link = ExpenseLink.objects.filter(
                user=self.user,
                project=project,
                category=category,
                subcategory=subcategory,
                status='active'
            ).first()
            if not expense_link:
                raise forms.ValidationError('Для выбранных проекта и категории нет активной связи.')
            cleaned_data['expense_link'] = expense_link
        return cleaned_data

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
