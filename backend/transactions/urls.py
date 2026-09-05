from django.urls import path
from .views import (
    transaction_list,
    transaction_import,
    transaction_import_sessions,
    transaction_import_review,
    transaction_links,
    transaction_links_lookup,
    transaction_rules,
    transaction_rule_delete,
    transaction_data,
    transaction_export,
    transaction_create,
    transaction_update,
    transaction_split,
    transaction_delete,
    bybit_connections,
    bybit_staging,
)

app_name = 'transactions'

urlpatterns = [
    path('', transaction_list, name='list'),
    path('import/', transaction_import, name='import'),
    path('import/sessions/', transaction_import_sessions, name='import-sessions'),
    path('import/review/', transaction_import_review, name='review'),
    path('links/', transaction_links, name='links'),
    path('links/lookup/', transaction_links_lookup, name='links-lookup'),
    path('rules/', transaction_rules, name='rules'),
    path('rules/<int:rule_id>/delete/', transaction_rule_delete, name='rule-delete'),
    path('integrations/bybit/', bybit_connections, name='bybit-connections'),
    path('bybit/', bybit_staging, name='bybit'),
    path('data/', transaction_data, name='data'),
    path('export/', transaction_export, name='export'),
    path('create/', transaction_create, name='create'),
    path('<int:pk>/update/', transaction_update, name='update'),
    path('<int:pk>/split/', transaction_split, name='split'),
    path('<int:pk>/delete/', transaction_delete, name='delete'),
]
