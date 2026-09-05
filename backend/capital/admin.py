from django.contrib import admin

from capital.models import CapitalPosition, CapitalSnapshot, SteamAccount, WithdrawalSite


@admin.register(WithdrawalSite)
class WithdrawalSiteAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'name', 'default_coefficient', 'currency', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'user__username')


@admin.register(SteamAccount)
class SteamAccountAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'account_code', 'login', 'purpose', 'currency', 'is_active')
    list_filter = ('purpose', 'is_active')
    search_fields = ('account_code', 'login', 'email', 'steam_id')


@admin.register(CapitalPosition)
class CapitalPositionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'name', 'kind', 'asset_class', 'currency', 'include_in_total', 'status')
    list_filter = ('kind', 'asset_class', 'status')
    search_fields = ('name', 'user__username')


@admin.register(CapitalSnapshot)
class CapitalSnapshotAdmin(admin.ModelAdmin):
    list_display = ('id', 'position', 'snapshot_date', 'total_value')
    list_filter = ('snapshot_date',)
    date_hierarchy = 'snapshot_date'
