from django.urls import path

from .views import dashboard_view, onboarding_view


app_name = 'crm'


urlpatterns = [
    path('', dashboard_view, name='dashboard'),
    path('onboarding/', onboarding_view, name='onboarding'),
]
