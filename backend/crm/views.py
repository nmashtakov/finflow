from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from .access import crm_access_required, get_current_organization
from .forms import OrganizationOnboardingForm
from .models import Membership
from .services import initialize_organization_defaults


@crm_access_required
def dashboard_view(request):
    organization = request.crm_organization
    return render(
        request,
        'crm/dashboard.html',
        {
            'organization': organization,
            'dashboard_cards': [
                ('Новые заказы', '—'),
                ('Продажи сегодня', '—'),
                ('Проблемы', '—'),
                ('Остатки', '—'),
            ],
        },
    )


@login_required
def onboarding_view(request):
    current_organization = get_current_organization(request.user)
    if current_organization is not None:
        return redirect('crm:dashboard')

    if request.method == 'POST':
        form = OrganizationOnboardingForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                organization = form.save(commit=False)
                organization.owner = request.user
                organization.save()
                Membership.objects.create(
                    organization=organization,
                    user=request.user,
                    role=Membership.Role.OWNER,
                )
                initialize_organization_defaults(organization)
            return redirect('crm:dashboard')
    else:
        form = OrganizationOnboardingForm()

    return render(request, 'crm/onboarding.html', {'form': form})
