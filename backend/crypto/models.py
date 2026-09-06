from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models

from core.currencies import USD_STABLECOINS


DEFAULT_CRYPTO_ASSETS = (
    ('BTC', 'Bitcoin'),
    ('ETH', 'Ethereum'),
    ('SOL', 'Solana'),
    ('TWT', 'Trust Wallet Token'),
    ('GRAM', 'Gram'),
    ('XRP', 'XRP'),
    ('MNT', 'Mantle'),
    ('DOGE', 'Dogecoin'),
)

QUOTE_CURRENCIES = set(USD_STABLECOINS) | {'USD'}


class CryptoAsset(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='crypto_assets')
    symbol = models.CharField(max_length=16)
    name = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    last_price_usd = models.DecimalField(max_digits=24, decimal_places=8, blank=True, null=True)
    change_24h_pct = models.DecimalField(max_digits=12, decimal_places=4, blank=True, null=True)
    price_updated_at = models.DateTimeField(blank=True, null=True)
    wallet_quantity = models.DecimalField(max_digits=24, decimal_places=8, blank=True, null=True)
    wallet_updated_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'symbol', 'id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'symbol'], name='uniq_crypto_asset_user_symbol'),
        ]

    def __str__(self):
        return self.symbol

    @property
    def bybit_spot_symbol(self) -> str:
        return f'{self.symbol}USDT'


class CryptoPortfolioSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='crypto_settings')
    bybit_connection = models.ForeignKey(
        'transactions.BybitConnection',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='crypto_portfolios',
    )
    report_currency = models.CharField(max_length=10, default='USD')
    cmc_api_key = models.CharField(max_length=255, blank=True, default='')
    last_rates_sync_at = models.DateTimeField(null=True, blank=True)
    last_tx_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Crypto settings for {self.user_id}'


class CryptoTransaction(models.Model):
    class TxType(models.TextChoices):
        BUY = ('buy', 'Покупка')
        SELL = ('sell', 'Продажа')

    class Source(models.TextChoices):
        MANUAL = ('manual', 'Вручную')
        BYBIT_CONVERT = ('bybit_convert', 'Bybit Convert')
        BYBIT_TRADE = ('bybit_trade', 'Bybit Trade')

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='crypto_transactions')
    asset = models.ForeignKey(CryptoAsset, on_delete=models.CASCADE, related_name='transactions')
    tx_type = models.CharField(max_length=8, choices=TxType.choices, default=TxType.BUY)
    occurred_at = models.DateTimeField()
    quantity = models.DecimalField(max_digits=24, decimal_places=8)
    price_quote = models.DecimalField(max_digits=24, decimal_places=8)
    total_quote = models.DecimalField(max_digits=24, decimal_places=8)
    fee_quote = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal('0'))
    note = models.TextField(blank=True, default='')
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    bybit_event = models.OneToOneField(
        'transactions.BybitExternalEvent',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='crypto_transaction',
    )
    external_key = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-occurred_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'external_key'],
                condition=~models.Q(external_key=''),
                name='uniq_crypto_tx_user_external_key',
            ),
        ]

    def __str__(self):
        return f'{self.tx_type} {self.quantity} {self.asset.symbol} @ {self.occurred_at}'
