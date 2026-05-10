from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
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


class OrganizationDirectoryItem(models.Model):
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['name', 'id']

    def __str__(self):
        return self.name


class SalesChannel(OrganizationDirectoryItem):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='sales_channels',
    )
    is_listing_channel = models.BooleanField(default=True)


class Warehouse(OrganizationDirectoryItem):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='warehouses',
    )


class Product(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='products',
    )
    sku = models.CharField(max_length=128)
    name = models.CharField(max_length=255)
    photo = models.ImageField(upload_to='crm/products/', blank=True, null=True)
    comment = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'sku', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'sku'],
                name='uniq_crm_product_organization_sku',
            ),
        ]

    def __str__(self):
        return f'{self.sku} — {self.name}'


class ProductChannelOffer(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='channel_offers',
    )
    channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.CASCADE,
        related_name='product_offers',
    )
    is_enabled = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['channel__name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'channel'],
                name='uniq_crm_product_channel_offer',
            ),
        ]

    def clean(self):
        errors = {}
        if self.product_id and self.channel_id:
            if self.product.organization_id != self.channel.organization_id:
                errors['channel'] = 'Канал продаж должен принадлежать той же организации, что и товар.'
            if not self.channel.is_listing_channel:
                errors['channel'] = 'Можно использовать только каналы размещения товара.'
        if self.is_enabled and self.price is None:
            errors['price'] = 'Укажите цену для включенного канала.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.product} @ {self.channel.name}'
