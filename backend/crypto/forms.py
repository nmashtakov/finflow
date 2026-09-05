from django import forms

from crypto.models import CryptoAsset, CryptoPortfolioSettings, CryptoTransaction
from transactions.models import BybitConnection


class CryptoPortfolioSettingsForm(forms.ModelForm):
    class Meta:
        model = CryptoPortfolioSettings
        fields = ['bybit_connection', 'report_currency', 'cmc_api_key']
        widgets = {
            'bybit_connection': forms.Select(attrs={'class': 'form-select'}),
            'report_currency': forms.Select(attrs={'class': 'form-select'}),
            'cmc_api_key': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'CoinMarketCap API key', 'autocomplete': 'off'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if user is not None:
            self.fields['bybit_connection'].queryset = BybitConnection.objects.filter(
                user=user,
                is_active=True,
            ).order_by('-updated_at')
        self.fields['bybit_connection'].required = False
        self.fields['bybit_connection'].empty_label = '— не выбрано —'
        self.fields['report_currency'].choices = [
            ('USD', 'USD'),
            ('USDT', 'USDT'),
            ('RUB', 'RUB'),
        ]


class CryptoAssetForm(forms.ModelForm):
    class Meta:
        model = CryptoAsset
        fields = ['symbol', 'name', 'is_active', 'sort_order']
        widgets = {
            'symbol': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'BTC'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Bitcoin'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
        }

    def clean_symbol(self):
        return (self.cleaned_data.get('symbol') or '').strip().upper()


class CryptoTransactionForm(forms.ModelForm):
    occurred_at = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
        label='Дата и время',
    )
    total_quote = forms.DecimalField(
        required=False,
        label='Сумма покупки ($)',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
    )

    class Meta:
        model = CryptoTransaction
        fields = ['tx_type', 'occurred_at', 'quantity', 'price_quote', 'fee_quote', 'note']
        widgets = {
            'tx_type': forms.Select(attrs={'class': 'form-select'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
            'price_quote': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
            'fee_quote': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
            'note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get('quantity')
        price_quote = cleaned.get('price_quote')
        total_quote = cleaned.get('total_quote')
        if quantity and quantity <= 0:
            self.add_error('quantity', 'Количество должно быть больше нуля.')
        if total_quote is None and price_quote is not None and quantity:
            cleaned['total_quote'] = quantity * price_quote
        elif total_quote is None and price_quote is None:
            self.add_error('price_quote', 'Укажите цену или сумму покупки.')
        return cleaned
