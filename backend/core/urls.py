from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from core.views import (
    accounts_directory,
    categories_settings,
    currency_converter_view,
    currency_rates_view,
    currencies_directory,
    dashboard_cell_transactions_view,
    dashboard_checklist_view,
    dashboard_view,
    landing_view,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('capital/', include(('capital.urls', 'capital'), namespace='capital')),
    path('crypto/', include(('crypto.urls', 'crypto'), namespace='crypto')),
    path('crm/', include(('crm.urls', 'crm'), namespace='crm')),
    path('transactions/', include(('transactions.urls', 'transactions'), namespace='transactions')),
    path('forecast/', include(('forecasting.urls', 'forecasting'), namespace='forecasting')),
    path('home/', lambda request: render(request, "error-404-2.html"), name="home"),  # Заглушка для главной
    path('', landing_view, name='landing'),
    path('dashboard/', dashboard_view, name='dashboard'),
    path('dashboard/cell-transactions/', dashboard_cell_transactions_view, name='dashboard_cell_transactions'),
    path('dashboard/checklist/', dashboard_checklist_view, name='dashboard_checklist'),
    path('categories/', categories_settings, name='categories_settings'),
    path('settings/accounts/', accounts_directory, name='accounts_directory'),
    path('settings/currencies/', currencies_directory, name='currencies_directory'),
    path('settings/currencies/rates/', currency_rates_view, name='currency_rates'),
    path('settings/currencies/converter/', currency_converter_view, name='currency_converter'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
