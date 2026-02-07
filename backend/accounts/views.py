from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from .forms import LoginForm, RegisterForm
from .default_setup import create_default_finance_structure
from django.contrib.auth.decorators import login_required, user_passes_test


def login_view(request):
    if request.user.is_authenticated:
        return redirect('profile')
    elif request.method == 'POST':
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

def register_view(request):
    if request.user.is_authenticated:
        return redirect('profile')
    elif request.method == "POST":
         form = RegisterForm(request.POST)
         if form.is_valid():
             user = form.save(commit=False)
             user.set_password(form.cleaned_data["password"])
             user.save()
             create_default_finance_structure(user)
             login(request, user)
             return redirect("profile")  # или куда хочешь
    else:
        form = RegisterForm()
    return render(request, "accounts/register.html", {"form": form})

def logout_view(request):
    logout(request)
    return redirect('landing')

def profile_view(request):
    return render(request, 'accounts/profile.html')

def password_reset_view(request):
    return render(request, 'core/error-404-2.html')

@login_required
@user_passes_test(lambda u: u.is_superuser, login_url='profile')
def user_list(request):
    users = User.objects.all()
    return render(request, 'accounts/user-list.html', {'users': users})
