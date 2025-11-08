from django.urls import path
from .views import transaction_list, transaction_import, transaction_data

app_name = 'transactions'

urlpatterns = [
    path('', transaction_list, name='list'),
    path('import/', transaction_import, name='import'),
    path('data/', transaction_data, name='data'),
]
