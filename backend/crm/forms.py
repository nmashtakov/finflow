from decimal import Decimal

from django import forms
from django.db.models import Q
from django.forms import BaseFormSet, formset_factory
from django.utils import timezone

from core.forms import BootstrapModelForm
from core.models import Currency

from .models import Counterparty, DeliveryMethod, Order, Organization, Product, Purchase, SalesChannel, Warehouse
from .services import build_delivery_method_code


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


class CRMDirectoryForm(BootstrapModelForm):
    def clean_name(self):
        return (self.cleaned_data.get('name') or '').strip()


class SalesChannelForm(CRMDirectoryForm):
    class Meta:
        model = SalesChannel
        fields = ['name', 'is_listing_channel']
        labels = {
            'name': 'Название канала продаж',
            'is_listing_channel': 'Канал размещения товара',
        }
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Например, Wildberries'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault('is_listing_channel', True)


class WarehouseForm(CRMDirectoryForm):
    class Meta:
        model = Warehouse
        fields = ['name']
        labels = {
            'name': 'Название склада',
        }
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Например, Региональный склад'}),
        }


class CounterpartyForm(CRMDirectoryForm):
    class Meta:
        model = Counterparty
        fields = ['name', 'comment']
        labels = {
            'name': 'Название контрагента',
            'comment': 'Комментарий',
        }
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Например, Поставщик 1'}),
            'comment': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Опционально'}),
        }

    def clean_comment(self):
        return (self.cleaned_data.get('comment') or '').strip()


class DeliveryMethodForm(CRMDirectoryForm):
    def __init__(self, *args, **kwargs):
        self.organization = kwargs.pop('organization', None)
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault('sort_order', 100)

    class Meta:
        model = DeliveryMethod
        fields = ['name', 'sort_order']
        labels = {
            'name': 'Название способа доставки',
            'sort_order': 'Порядок',
        }
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Например, Курьер'}),
            'sort_order': forms.NumberInput(attrs={'min': 0}),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        organization = self.organization or getattr(instance, 'organization', None)
        if not instance.pk or not instance.code:
            base_code = build_delivery_method_code(instance.name, fallback='delivery-method')
            next_code = base_code
            suffix = 2
            if organization is not None:
                queryset = DeliveryMethod.objects.filter(organization=organization)
                if instance.pk:
                    queryset = queryset.exclude(pk=instance.pk)
                while queryset.filter(code=next_code).exists():
                    next_code = f'{base_code}-{suffix}'
                    suffix += 1
            instance.code = next_code
        if commit:
            instance.save()
        return instance


class ProductForm(BootstrapModelForm):
    remove_photo = forms.BooleanField(required=False, label='Удалить текущее фото')

    def __init__(self, *args, **kwargs):
        self.organization = kwargs.pop('organization', None)
        super().__init__(*args, **kwargs)
        self.fields['photo'].widget.attrs['data-photo-input'] = 'true'

    class Meta:
        model = Product
        fields = ['sku', 'name', 'photo', 'comment', 'is_active']
        labels = {
            'sku': 'SKU / артикул',
            'name': 'Название товара',
            'photo': 'Фото',
            'comment': 'Комментарий',
            'is_active': 'Товар активен',
        }
        widgets = {
            'sku': forms.TextInput(attrs={'placeholder': 'Например, 75192'}),
            'name': forms.TextInput(attrs={'placeholder': 'Например, Конструктор UCS'}),
            'photo': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'comment': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Короткая заметка по товару'}),
        }

    def clean_sku(self):
        sku = (self.cleaned_data.get('sku') or '').strip()
        organization = self.organization or getattr(self.instance, 'organization', None)
        if organization and sku:
            queryset = Product.objects.filter(organization=organization, sku=sku)
            if self.instance.pk:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise forms.ValidationError('Товар с таким SKU / артикулом уже существует в этой организации.')
        return sku

    def clean_name(self):
        return (self.cleaned_data.get('name') or '').strip()

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get('remove_photo') and not self.cleaned_data.get('photo'):
            if instance.photo:
                instance.photo.delete(save=False)
            instance.photo = None
        if commit:
            instance.save()
        return instance


class ProductChannelOfferForm(forms.Form):
    def __init__(self, *args, channels=None, offers=None, **kwargs):
        self.channels = list(channels or [])
        self.existing_offers = {offer.channel_id: offer for offer in (offers or [])}
        super().__init__(*args, **kwargs)
        self.channel_rows = []

        for channel in self.channels:
            enabled_name = f'channel_{channel.id}_enabled'
            price_name = f'channel_{channel.id}_price'
            comment_name = f'channel_{channel.id}_comment'
            offer = self.existing_offers.get(channel.id)

            self.fields[enabled_name] = forms.BooleanField(required=False)
            self.fields[price_name] = forms.DecimalField(
                required=False,
                decimal_places=2,
                max_digits=12,
                min_value=0,
            )
            self.fields[comment_name] = forms.CharField(
                required=False,
                widget=forms.TextInput(attrs={'placeholder': 'Комментарий по каналу'}),
            )

            self.fields[enabled_name].label = 'Размещать на канале'
            self.fields[price_name].label = 'Цена'
            self.fields[comment_name].label = 'Комментарий'
            self.fields[enabled_name].widget.attrs.setdefault('class', 'form-check-input')
            self.fields[price_name].widget.attrs['class'] = 'form-control'
            self.fields[comment_name].widget.attrs['class'] = 'form-control'

            if not self.is_bound:
                self.initial[enabled_name] = offer.is_enabled if offer else False
                self.initial[price_name] = offer.price if offer else None
                self.initial[comment_name] = offer.comment if offer else ''

            self.channel_rows.append(
                {
                    'channel': channel,
                    'enabled_field': self[enabled_name],
                    'price_field': self[price_name],
                    'comment_field': self[comment_name],
                }
            )

    def clean(self):
        cleaned_data = super().clean()
        for channel in self.channels:
            enabled = cleaned_data.get(f'channel_{channel.id}_enabled')
            price = cleaned_data.get(f'channel_{channel.id}_price')
            if enabled and price is None:
                self.add_error(
                    f'channel_{channel.id}_price',
                    'Укажите цену для канала, если товар на нем размещается.',
                )
        return cleaned_data

    def build_offer_payloads(self):
        payloads = []
        for channel in self.channels:
            enabled = self.cleaned_data.get(f'channel_{channel.id}_enabled', False)
            price = self.cleaned_data.get(f'channel_{channel.id}_price')
            comment = (self.cleaned_data.get(f'channel_{channel.id}_comment') or '').strip()
            existing_offer = self.existing_offers.get(channel.id)

            if not enabled and not existing_offer and price is None and not comment:
                continue

            payloads.append(
                {
                    'channel': channel,
                    'is_enabled': bool(enabled),
                    'price': price if enabled else None,
                    'comment': comment,
                    'existing_offer': existing_offer,
                }
            )
        return payloads


class PurchaseForm(BootstrapModelForm):
    counterparty = forms.ModelChoiceField(queryset=Counterparty.objects.none(), required=False, label='Контрагент')
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), label='Склад')

    def __init__(self, *args, **kwargs):
        organization = kwargs.pop('organization', None)
        super().__init__(*args, **kwargs)
        counterparty_queryset = Counterparty.objects.none()
        warehouse_queryset = Warehouse.objects.none()
        if organization is not None:
            used_counterparty_id = getattr(self.instance, 'counterparty_id', None)
            used_warehouse_id = getattr(self.instance, 'warehouse_id', None)
            counterparty_queryset = Counterparty.objects.filter(organization=organization).filter(
                Q(is_active=True) | Q(id=used_counterparty_id)
            ).order_by('name', 'id')
            warehouse_queryset = Warehouse.objects.filter(organization=organization).filter(
                Q(is_active=True) | Q(id=used_warehouse_id)
            ).order_by('name', 'id')
        self.fields['counterparty'].queryset = counterparty_queryset
        self.fields['warehouse'].queryset = warehouse_queryset
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault('purchase_date', timezone.localdate())
            self.initial.setdefault('status', Purchase.Status.POSTED)

    class Meta:
        model = Purchase
        fields = ['purchase_date', 'warehouse', 'counterparty', 'comment', 'status']
        labels = {
            'purchase_date': 'Дата закупки',
            'warehouse': 'Склад',
            'counterparty': 'Контрагент',
            'comment': 'Комментарий',
            'status': 'Статус',
        }
        widgets = {
            'purchase_date': forms.DateInput(attrs={'type': 'date'}),
            'comment': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Комментарий по закупке'}),
        }


class OrderForm(BootstrapModelForm):
    sales_channel = forms.ModelChoiceField(queryset=SalesChannel.objects.none(), label='Источник продаж')
    delivery_method = forms.ModelChoiceField(queryset=DeliveryMethod.objects.none(), required=False, label='Способ доставки')
    customer_counterparty = forms.ModelChoiceField(
        queryset=Counterparty.objects.none(),
        required=False,
        label='Клиент из базы',
    )

    def __init__(self, *args, **kwargs):
        organization = kwargs.pop('organization', None)
        allow_status_edit = kwargs.pop('allow_status_edit', False)
        super().__init__(*args, **kwargs)

        used_channel_id = getattr(self.instance, 'sales_channel_id', None)
        used_delivery_method_id = getattr(self.instance, 'delivery_method_id', None)
        used_customer_counterparty_id = getattr(self.instance, 'customer_counterparty_id', None)
        sales_channel_queryset = SalesChannel.objects.none()
        delivery_method_queryset = DeliveryMethod.objects.none()
        customer_counterparty_queryset = Counterparty.objects.none()

        if organization is not None:
            sales_channel_queryset = SalesChannel.objects.filter(organization=organization).filter(
                Q(is_active=True) | Q(id=used_channel_id)
            ).order_by('name', 'id')
            delivery_method_queryset = DeliveryMethod.objects.filter(organization=organization).filter(
                Q(is_active=True) | Q(id=used_delivery_method_id)
            ).order_by('sort_order', 'name', 'id')
            customer_counterparty_queryset = Counterparty.objects.filter(organization=organization).filter(
                Q(is_active=True) | Q(id=used_customer_counterparty_id)
            ).order_by('name', 'id')

        self.fields['sales_channel'].queryset = sales_channel_queryset
        self.fields['delivery_method'].queryset = delivery_method_queryset
        self.fields['customer_counterparty'].queryset = customer_counterparty_queryset

        if not allow_status_edit:
            self.fields['status'].widget = forms.HiddenInput()

        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault('order_date', timezone.localdate())
            self.initial.setdefault('status', Order.Status.NEW)
            self.initial.setdefault('payment_status', Order.PaymentStatus.UNPAID)

    class Meta:
        model = Order
        fields = [
            'external_order_number',
            'order_date',
            'sales_channel',
            'delivery_method',
            'customer_counterparty',
            'customer_name',
            'customer_phone',
            'customer_contact',
            'payment_status',
            'status',
            'comment',
        ]
        labels = {
            'external_order_number': 'Внешний номер заказа',
            'order_date': 'Дата заказа',
            'sales_channel': 'Источник продаж',
            'delivery_method': 'Способ доставки',
            'customer_counterparty': 'Клиент из базы',
            'customer_name': 'Имя клиента',
            'customer_phone': 'Телефон',
            'customer_contact': 'Контакт / ник / ссылка',
            'payment_status': 'Оплата',
            'status': 'Статус заказа',
            'comment': 'Комментарий по заказу',
        }
        widgets = {
            'external_order_number': forms.TextInput(attrs={'placeholder': 'Например, AV-12345'}),
            'order_date': forms.DateInput(attrs={'type': 'date'}),
            'customer_name': forms.TextInput(attrs={'placeholder': 'Например, Иван'}),
            'customer_phone': forms.TextInput(attrs={'placeholder': '+7...'}),
            'customer_contact': forms.TextInput(attrs={'placeholder': 'Телеграм, Avito, ссылка'}),
            'comment': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Внутренний комментарий по заказу'}),
        }

    def clean_external_order_number(self):
        return (self.cleaned_data.get('external_order_number') or '').strip()

    def clean_customer_name(self):
        return (self.cleaned_data.get('customer_name') or '').strip()

    def clean_customer_phone(self):
        return (self.cleaned_data.get('customer_phone') or '').strip()

    def clean_customer_contact(self):
        return (self.cleaned_data.get('customer_contact') or '').strip()

    def clean_comment(self):
        return (self.cleaned_data.get('comment') or '').strip()

    def clean(self):
        cleaned_data = super().clean()
        customer_counterparty = cleaned_data.get('customer_counterparty')
        customer_name = (cleaned_data.get('customer_name') or '').strip()
        if customer_counterparty and not customer_name:
            cleaned_data['customer_name'] = customer_counterparty.name
        return cleaned_data


class PurchaseItemRowForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none(), required=False, label='Товар')
    quantity = forms.IntegerField(required=False, min_value=1, label='Количество')
    unit_cost = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=2, label='Цена за единицу')
    extra_cost_total = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        label='Доп. расходы',
    )
    comment = forms.CharField(
        required=False,
        label='Комментарий',
        widget=forms.TextInput(attrs={'placeholder': 'Опционально'}),
    )

    tracked_fields = ('product', 'quantity', 'unit_cost', 'extra_cost_total', 'comment')

    def __init__(self, *args, **kwargs):
        product_queryset = kwargs.pop('product_queryset', Product.objects.none())
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = product_queryset
        self.fields['product'].label_from_instance = lambda product: f'{product.sku} — {product.name}'

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs['class'] = 'form-select'
            else:
                css_classes = widget.attrs.get('class', '')
                widget.attrs['class'] = f"{css_classes} form-control".strip()

        self.fields['extra_cost_total'].initial = self.initial.get('extra_cost_total', Decimal('0.00'))

    def _row_has_input(self):
        if self.is_bound:
            for field_name in self.tracked_fields:
                value = self.data.get(self.add_prefix(field_name))
                if value not in (None, ''):
                    return True
            return False

        return any(
            self.initial.get(field_name) not in (None, '', Decimal('0.00'))
            for field_name in self.tracked_fields
        )

    def clean(self):
        cleaned_data = super().clean()
        row_has_input = self._row_has_input()
        cleaned_data['_row_has_input'] = row_has_input
        if not row_has_input:
            cleaned_data['extra_cost_total'] = Decimal('0.00')
            cleaned_data['comment'] = ''
            return cleaned_data

        required_fields = {
            'product': 'Выберите товар.',
            'quantity': 'Укажите количество.',
            'unit_cost': 'Укажите цену закупки за единицу.',
        }
        for field_name, error_message in required_fields.items():
            if field_name not in self.errors and not cleaned_data.get(field_name):
                self.add_error(field_name, error_message)

        cleaned_data['extra_cost_total'] = cleaned_data.get('extra_cost_total') or Decimal('0.00')
        cleaned_data['comment'] = (cleaned_data.get('comment') or '').strip()
        return cleaned_data


class BasePurchaseItemFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        has_rows = False
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue
            if form.cleaned_data.get('DELETE'):
                continue
            if form.cleaned_data.get('_row_has_input'):
                has_rows = True
                break

        if not has_rows:
            raise forms.ValidationError('Добавьте хотя бы одну строку закупки.')

    def get_cleaned_rows(self):
        rows = []
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue
            if form.cleaned_data.get('DELETE'):
                continue
            if not form.cleaned_data.get('_row_has_input'):
                continue
            rows.append(
                {
                    'product': form.cleaned_data['product'],
                    'quantity': form.cleaned_data['quantity'],
                    'unit_cost': form.cleaned_data['unit_cost'],
                    'extra_cost_total': form.cleaned_data['extra_cost_total'],
                    'comment': form.cleaned_data['comment'],
                }
            )
        return rows


class OrderItemRowForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none(), required=False, label='Товар')
    quantity = forms.IntegerField(required=False, min_value=1, label='Количество')
    sale_price = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=2, label='Цена продажи')
    comment = forms.CharField(
        required=False,
        label='Комментарий',
        widget=forms.TextInput(attrs={'placeholder': 'Опционально'}),
    )

    tracked_fields = ('product', 'quantity', 'sale_price', 'comment')

    def __init__(self, *args, **kwargs):
        product_queryset = kwargs.pop('product_queryset', Product.objects.none())
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = product_queryset
        self.fields['product'].label_from_instance = lambda product: f'{product.sku} — {product.name}'

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs['class'] = 'form-select'
            else:
                css_classes = widget.attrs.get('class', '')
                widget.attrs['class'] = f"{css_classes} form-control".strip()

    def _row_has_input(self):
        if self.is_bound:
            for field_name in self.tracked_fields:
                value = self.data.get(self.add_prefix(field_name))
                if value not in (None, ''):
                    return True
            return False

        return any(self.initial.get(field_name) not in (None, '') for field_name in self.tracked_fields)

    def clean(self):
        cleaned_data = super().clean()
        row_has_input = self._row_has_input()
        cleaned_data['_row_has_input'] = row_has_input
        if not row_has_input:
            cleaned_data['comment'] = ''
            return cleaned_data

        required_fields = {
            'product': 'Выберите товар.',
            'quantity': 'Укажите количество.',
        }
        for field_name, error_message in required_fields.items():
            if field_name not in self.errors and not cleaned_data.get(field_name):
                self.add_error(field_name, error_message)

        cleaned_data['comment'] = (cleaned_data.get('comment') or '').strip()
        return cleaned_data


class BaseOrderItemFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        has_rows = False
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue
            if form.cleaned_data.get('DELETE'):
                continue
            if form.cleaned_data.get('_row_has_input'):
                has_rows = True
                break

        if not has_rows:
            raise forms.ValidationError('Добавьте хотя бы одну строку заказа.')

    def get_cleaned_rows(self):
        rows = []
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue
            if form.cleaned_data.get('DELETE'):
                continue
            if not form.cleaned_data.get('_row_has_input'):
                continue
            rows.append(
                {
                    'product': form.cleaned_data['product'],
                    'quantity': form.cleaned_data['quantity'],
                    'sale_price': form.cleaned_data.get('sale_price'),
                    'comment': form.cleaned_data['comment'],
                }
            )
        return rows


class PurchaseCSVUploadForm(forms.Form):
    file = forms.FileField(
        required=False,
        label='CSV-файл',
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.csv,text/csv'}),
    )

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file and not file.name.lower().endswith('.csv'):
            raise forms.ValidationError('Используйте CSV-файл по шаблону.')
        return file


class ProductBulkUploadForm(forms.Form):
    xlsx_file = forms.FileField(
        label='Excel-файл',
        widget=forms.ClearableFileInput(
            attrs={
                'class': 'form-control',
                'accept': '.xlsx,.xlsm,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            }
        ),
    )

    def clean_xlsx_file(self):
        file = self.cleaned_data.get('xlsx_file')
        if file and not file.name.lower().endswith(('.xlsx', '.xlsm')):
            raise forms.ValidationError('Используйте Excel-файл в формате .xlsx или .xlsm.')
        return file


PurchaseItemFormSet = formset_factory(
    PurchaseItemRowForm,
    formset=BasePurchaseItemFormSet,
    extra=1,
    can_delete=True,
)

OrderItemFormSet = formset_factory(
    OrderItemRowForm,
    formset=BaseOrderItemFormSet,
    extra=1,
    can_delete=True,
)
