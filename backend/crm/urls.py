from django.urls import path

from .views import (
    dashboard_view,
    onboarding_view,
    product_archive_view,
    product_create_view,
    product_edit_view,
    products_view,
    sales_channel_archive_view,
    sales_channel_create_view,
    sales_channel_edit_view,
    sales_channel_restore_view,
    settings_view,
    warehouse_archive_view,
    warehouse_create_view,
    warehouse_edit_view,
    warehouse_restore_view,
)


app_name = 'crm'


urlpatterns = [
    path('', dashboard_view, name='dashboard'),
    path('onboarding/', onboarding_view, name='onboarding'),
    path('products/', products_view, name='products'),
    path('products/create/', product_create_view, name='product_create'),
    path('products/<int:product_id>/edit/', product_edit_view, name='product_edit'),
    path('products/<int:product_id>/archive/', product_archive_view, name='product_archive'),
    path('settings/', settings_view, name='settings'),
    path('settings/channels/create/', sales_channel_create_view, name='sales_channel_create'),
    path('settings/channels/<int:channel_id>/edit/', sales_channel_edit_view, name='sales_channel_edit'),
    path('settings/channels/<int:channel_id>/archive/', sales_channel_archive_view, name='sales_channel_archive'),
    path('settings/channels/<int:channel_id>/restore/', sales_channel_restore_view, name='sales_channel_restore'),
    path('settings/warehouses/create/', warehouse_create_view, name='warehouse_create'),
    path('settings/warehouses/<int:warehouse_id>/edit/', warehouse_edit_view, name='warehouse_edit'),
    path('settings/warehouses/<int:warehouse_id>/archive/', warehouse_archive_view, name='warehouse_archive'),
    path('settings/warehouses/<int:warehouse_id>/restore/', warehouse_restore_view, name='warehouse_restore'),
]
