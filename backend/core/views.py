from django.shortcuts import render, redirect

def landing_view(request):
    return render(request, 'landing.html')

def dashboard_view(request):
    return render(request, 'dashboard.html')