from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from core.views import landing_view, dashboard_view, categories_settings, accounts_directory

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('transactions/', include(('transactions.urls', 'transactions'), namespace='transactions')),
    path('home/', lambda request: render(request, "error-404-2.html"), name="home"),  # Заглушка для главной
    path('', landing_view, name='landing'),
    path('dashboard/', dashboard_view, name='dashboard'),
    path('categories/', categories_settings, name='categories_settings'),
    path('settings/accounts/', accounts_directory, name='accounts_directory'),
]
