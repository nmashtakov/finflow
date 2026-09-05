from django.urls import path

from crypto import views


app_name = 'crypto'

urlpatterns = [
    path('', views.crypto_dashboard, name='dashboard'),
    path('asset/<str:symbol>/', views.crypto_asset_detail, name='asset'),
    path('settings/', views.crypto_settings, name='settings'),
]
