from django.urls import path

from capital import views


app_name = 'capital'

urlpatterns = [
    path('', views.capital_dashboard, name='dashboard'),
    path('settings/', views.capital_settings, name='settings'),
    path('snapshots/', views.capital_snapshots, name='snapshots'),
]
