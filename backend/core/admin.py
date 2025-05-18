from django.contrib import admin
from .models import Project, Category, Subcategory, Account, ExpenseLink, Transaction, CurrencyRate

admin.site.register(Project)
admin.site.register(Category)
admin.site.register(Subcategory)
admin.site.register(Account)
admin.site.register(ExpenseLink)
admin.site.register(Transaction)
admin.site.register(CurrencyRate)