from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from .forms import LoginForm

# Create your views here.
def login_view(request):
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect('landing')
            else:
                form.add_error(None, 'Неверный email или пароль')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {'form': form})

def logout_view(request):
    logout(request)
    return redirect('landing')

def profile_view(request):
    return render(request, 'accounts/profile.html')

def register_view(request):
    return render(request, 'core/error-404-2.html')

def password_reset_view(request):
    return render(request, 'core/error-404-2.html')