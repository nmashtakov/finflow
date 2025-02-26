from django.urls import path
from accounts.views import login_view, logout_view, profile_view, register_view, password_reset_view, user_list


urlpatterns = [
    path('register/', register_view, name='register'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('password_reset/', password_reset_view, name='password_reset'),
    path('users/', user_list, name='user-list'),
]