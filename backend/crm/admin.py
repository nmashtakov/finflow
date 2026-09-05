from django.contrib import admin

from .models import (
    Counterparty,
    DeliveryMethod,
    Membership,
    Order,
    OrderItem,
    Organization,
    Product,
    ProductChannelOffer,
    Purchase,
    PurchaseItem,
    SalesChannel,
    StockLot,
    StockMovement,
    StockReservation,
    Warehouse,
)


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


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'organization__name', 'comment')


@admin.register(DeliveryMethod)
class DeliveryMethodAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'organization', 'sort_order', 'is_active', 'updated_at')
    list_filter = ('is_active', 'organization')
    search_fields = ('name', 'code', 'organization__name')


class ProductChannelOfferInline(admin.TabularInline):
    model = ProductChannelOffer
    extra = 0


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0


class OrderItemInline(admin.TabularInline):
    model = OrderItem
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


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ('purchase_date', 'counterparty', 'organization', 'status', 'created_by', 'updated_at')
    list_filter = ('status', 'organization')
    search_fields = ('counterparty__name', 'organization__name', 'comment')
    inlines = [PurchaseItemInline]


@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = ('purchase', 'product', 'warehouse', 'quantity', 'unit_cost', 'extra_cost_total')
    list_filter = ('organization', 'warehouse')
    search_fields = ('purchase__counterparty__name', 'product__sku', 'product__name', 'warehouse__name')


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'internal_order_number',
        'external_order_number',
        'order_date',
        'sales_channel',
        'delivery_method',
        'status',
        'payment_status',
        'organization',
    )
    list_filter = ('status', 'payment_status', 'organization', 'sales_channel')
    search_fields = (
        'internal_order_number',
        'external_order_number',
        'customer_name',
        'customer_phone',
        'customer_contact',
    )
    inlines = [OrderItemInline]


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ('order', 'product', 'quantity', 'sale_price', 'source_price_channel', 'organization')
    list_filter = ('organization', 'source_price_channel')
    search_fields = ('order__internal_order_number', 'product__sku', 'product__name')


@admin.register(StockLot)
class StockLotAdmin(admin.ModelAdmin):
    list_display = ('product', 'warehouse', 'remaining_quantity', 'unit_cost', 'extra_cost_per_unit', 'organization')
    list_filter = ('organization', 'warehouse')
    search_fields = ('product__sku', 'product__name', 'warehouse__name', 'purchase_item__purchase__counterparty__name')


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ('movement_type', 'product', 'warehouse', 'quantity', 'organization', 'created_at')
    list_filter = ('movement_type', 'organization', 'warehouse')
    search_fields = ('product__sku', 'product__name', 'warehouse__name', 'comment')


@admin.register(StockReservation)
class StockReservationAdmin(admin.ModelAdmin):
    list_display = ('order', 'order_item', 'stock_lot', 'quantity', 'status', 'organization', 'updated_at')
    list_filter = ('status', 'organization')
    search_fields = (
        'order__internal_order_number',
        'order_item__product__sku',
        'order_item__product__name',
        'stock_lot__warehouse__name',
    )
