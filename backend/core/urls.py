from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from core.views import landing_view, dashboard_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('home/', lambda request: render(request, "error-404-2.html"), name="home"),  # Заглушка для главной
    path('', landing_view, name='landing'),
    path('dashboard/', dashboard_view, name='dashboard')
]
