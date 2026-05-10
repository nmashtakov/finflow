from django import forms

from core.forms import BootstrapModelForm
from core.models import Currency

from .models import Organization, Product, SalesChannel, Warehouse


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
