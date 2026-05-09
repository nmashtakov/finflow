from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect

from .models import Membership


def get_current_organization(user):
    if not getattr(user, 'is_authenticated', False):
        return None

    membership = (
        Membership.objects.select_related('organization')
        .filter(
            user=user,
            is_active=True,
            organization__is_active=True,
        )
        .order_by('created_at', 'id')
        .first()
    )
    return membership.organization if membership else None


def user_has_crm_access(user, organization):
    if not getattr(user, 'is_authenticated', False) or organization is None or not organization.is_active:
        return False

    return Membership.objects.filter(
        organization=organization,
        user=user,
        is_active=True,
        organization__is_active=True,
    ).exists()


def crm_access_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        organization = get_current_organization(request.user)
        if organization is None:
            return redirect('crm:onboarding')

        request.crm_organization = organization
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped_view)


class CRMAccessMixin(LoginRequiredMixin):
    crm_organization_attr = 'crm_organization'

    def dispatch(self, request, *args, **kwargs):
        organization = get_current_organization(request.user)
        if organization is None:
            return redirect('crm:onboarding')

        setattr(request, self.crm_organization_attr, organization)
        self.crm_organization = organization
        return super().dispatch(request, *args, **kwargs)
