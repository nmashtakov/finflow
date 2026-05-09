from django.urls import path

from .views import month_summary


app_name = 'forecasting'

urlpatterns = [
    path('', month_summary, name='month-summary'),
]
