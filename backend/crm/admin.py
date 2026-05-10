from django.contrib import admin

from .models import Membership, Organization, Product, ProductChannelOffer, SalesChannel, Warehouse


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'base_currency', 'is_active', 'created_at')
    list_filter = ('is_active', 'base_currency')
    search_fields = ('name', 'owner__username', 'owner__email')


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ('organization', 'user', 'role', 'is_active', 'created_at')
    list_filter = ('role', 'is_active')
    search_fields = ('organization__name', 'user__username', 'user__email')


@admin.register(SalesChannel)
class SalesChannelAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'is_listing_channel', 'is_active', 'updated_at')
    list_filter = ('is_active', 'is_listing_channel')
    search_fields = ('name', 'organization__name')


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'organization__name')


class ProductChannelOfferInline(admin.TabularInline):
    model = ProductChannelOffer
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'organization', 'is_active', 'updated_at')
    list_filter = ('is_active', 'organization')
    search_fields = ('sku', 'name', 'organization__name')
    inlines = [ProductChannelOfferInline]


@admin.register(ProductChannelOffer)
class ProductChannelOfferAdmin(admin.ModelAdmin):
    list_display = ('product', 'channel', 'is_enabled', 'price', 'updated_at')
    list_filter = ('is_enabled', 'channel__organization')
    search_fields = ('product__sku', 'product__name', 'channel__name')
