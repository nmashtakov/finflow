from django.contrib import admin

from crypto.models import CryptoAsset, CryptoPortfolioSettings, CryptoTransaction


@admin.register(CryptoAsset)
class CryptoAssetAdmin(admin.ModelAdmin):
    list_display = ('user', 'symbol', 'name', 'is_active', 'sort_order')
    list_filter = ('is_active',)
    search_fields = ('symbol', 'name', 'user__username')


@admin.register(CryptoPortfolioSettings)
class CryptoPortfolioSettingsAdmin(admin.ModelAdmin):
    list_display = ('user', 'bybit_connection', 'report_currency', 'last_rates_sync_at', 'last_tx_sync_at')


@admin.register(CryptoTransaction)
class CryptoTransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'asset', 'tx_type', 'occurred_at', 'quantity', 'total_quote', 'source')
    list_filter = ('tx_type', 'source')
    search_fields = ('asset__symbol', 'external_key', 'note')
