from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .access import get_current_organization, user_has_crm_access
from .models import Membership, Organization


class CRMOnboardingFlowTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='crm-owner', password='password123')

    def test_dashboard_redirects_to_onboarding_without_active_membership(self):
        self.client.login(username='crm-owner', password='password123')

        response = self.client.get(reverse('crm:dashboard'))

        self.assertRedirects(response, reverse('crm:onboarding'))

    def test_onboarding_creates_organization_and_owner_membership(self):
        self.client.login(username='crm-owner', password='password123')

        response = self.client.post(
            reverse('crm:onboarding'),
            {
                'name': 'LEGO бизнес',
                'base_currency': 'RUB',
            },
        )

        self.assertRedirects(response, reverse('crm:dashboard'))
        organization = Organization.objects.get()
        membership = Membership.objects.get()

        self.assertEqual(organization.owner, self.user)
        self.assertEqual(organization.name, 'LEGO бизнес')
        self.assertEqual(organization.base_currency, 'RUB')
        self.assertEqual(membership.organization, organization)
        self.assertEqual(membership.user, self.user)
        self.assertEqual(membership.role, Membership.Role.OWNER)

        dashboard_response = self.client.get(reverse('crm:dashboard'))
        self.assertContains(dashboard_response, 'LEGO бизнес')
        self.assertContains(dashboard_response, 'Настройка')
        self.assertNotContains(dashboard_response, 'Аналитика')


class CRMAccessHelpersTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='crm-owner', password='password123')
        self.outsider = User.objects.create_user(username='crm-outsider', password='password123')
        self.organization = Organization.objects.create(
            owner=self.owner,
            name='Brick Trade',
            base_currency='RUB',
        )
        Membership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=Membership.Role.OWNER,
        )

    def test_get_current_organization_returns_first_active_organization(self):
        organization = get_current_organization(self.owner)

        self.assertEqual(organization, self.organization)

    def test_user_has_crm_access_denies_non_member(self):
        self.assertFalse(user_has_crm_access(self.outsider, self.organization))

    def test_dashboard_does_not_expose_other_users_organization(self):
        self.client.login(username='crm-outsider', password='password123')

        response = self.client.get(reverse('crm:dashboard'))

        self.assertRedirects(response, reverse('crm:onboarding'))

    def test_membership_unique_constraint_blocks_duplicate_user_in_organization(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Membership.objects.create(
                    organization=self.organization,
                    user=self.owner,
                    role=Membership.Role.ADMIN,
                )

    def test_finance_pages_use_finance_sidebar(self):
        self.client.login(username='crm-owner', password='password123')

        response = self.client.get(reverse('profile'))

        self.assertContains(response, 'Аналитика')
        self.assertContains(response, 'Финансы')
        self.assertContains(response, 'CRM')
        self.assertNotContains(response, 'Инвентаризация')
