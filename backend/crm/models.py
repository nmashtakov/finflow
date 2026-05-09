from django.contrib.auth.models import User
from django.db import models


class Organization(models.Model):
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='crm_owned_organizations',
    )
    name = models.CharField(max_length=255)
    base_currency = models.CharField(max_length=8, default='RUB')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = ('owner', 'Owner')
        ADMIN = ('admin', 'Admin')
        MANAGER = ('manager', 'Manager')
        VIEWER = ('viewer', 'Viewer')

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='crm_memberships',
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'user'],
                name='uniq_crm_membership_organization_user',
            ),
        ]

    def __str__(self):
        return f'{self.user.username} @ {self.organization.name} ({self.role})'
