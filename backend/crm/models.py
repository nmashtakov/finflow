from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from decimal import Decimal, ROUND_HALF_UP


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


class Counterparty(OrganizationDirectoryItem):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='counterparties',
    )
    comment = models.TextField(blank=True)


class DeliveryMethod(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='delivery_methods',
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='uniq_crm_delivery_method_organization_code',
            ),
        ]

    def __str__(self):
        return self.name


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


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = ('new', 'Новый')
        AWAITING_SHIPMENT = ('awaiting_shipment', 'Ожидает отправки / самовывоз')
        IN_DELIVERY = ('in_delivery', 'В доставке')
        PICKUP_POINT = ('pickup_point', 'В ПВЗ')
        COMPLETED = ('completed', 'Завершен')
        CANCELLED = ('cancelled', 'Отменен')
        RETURNED = ('returned', 'Возврат')

    class PaymentStatus(models.TextChoices):
        UNPAID = ('unpaid', 'Не оплачено')
        PAID = ('paid', 'Оплачено')
        PARTIALLY_PAID = ('partially_paid', 'Частично оплачено')
        REFUNDED = ('refunded', 'Возврат оплаты')

    class PaymentMethod(models.TextChoices):
        CASH = ('cash', 'Наличные')
        TRANSFER = ('transfer', 'Перевод')
        MARKETPLACE = ('marketplace', 'Маркетплейс')
        OTHER = ('other', 'Другое')

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='orders',
    )
    internal_order_number = models.CharField(max_length=32, blank=True, null=True)
    external_order_number = models.CharField(max_length=255, blank=True)
    order_date = models.DateField()
    sales_channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        related_name='orders',
    )
    delivery_method = models.ForeignKey(
        DeliveryMethod,
        on_delete=models.SET_NULL,
        related_name='orders',
        null=True,
        blank=True,
    )
    customer_counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.SET_NULL,
        related_name='customer_orders',
        null=True,
        blank=True,
    )
    customer_name = models.CharField(max_length=255, blank=True)
    customer_phone = models.CharField(max_length=64, blank=True)
    customer_contact = models.CharField(max_length=255, blank=True)
    customer_address = models.TextField(blank=True)
    customer_comment = models.TextField(blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.NEW)
    payment_status = models.CharField(
        max_length=32,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
    )
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=32, choices=PaymentMethod.choices, blank=True)
    comment = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='crm_created_orders',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-order_date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'internal_order_number'],
                name='uniq_crm_order_organization_internal_number',
            ),
        ]

    def clean(self):
        errors = {}
        if self.sales_channel_id and self.organization_id and self.sales_channel.organization_id != self.organization_id:
            errors['sales_channel'] = 'Источник продаж должен принадлежать той же организации, что и заказ.'
        if self.delivery_method_id and self.organization_id and self.delivery_method.organization_id != self.organization_id:
            errors['delivery_method'] = 'Способ доставки должен принадлежать той же организации, что и заказ.'
        if self.customer_counterparty_id and self.organization_id and self.customer_counterparty.organization_id != self.organization_id:
            errors['customer_counterparty'] = 'Клиент из базы должен принадлежать той же организации, что и заказ.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.internal_order_number:
            self.internal_order_number = f'ORD-{self.pk:06d}'
            Order.objects.filter(pk=self.pk).update(internal_order_number=self.internal_order_number)

    def __str__(self):
        return self.internal_order_number or f'Заказ #{self.pk}'


class OrderItem(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='order_items',
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='order_items',
    )
    quantity = models.PositiveIntegerField()
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    source_price_channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.SET_NULL,
        related_name='order_items_with_price_source',
        null=True,
        blank=True,
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']

    def clean(self):
        errors = {}
        if self.order_id and self.organization_id and self.order.organization_id != self.organization_id:
            errors['order'] = 'Заказ должен принадлежать той же организации.'
        if self.product_id and self.organization_id and self.product.organization_id != self.organization_id:
            errors['product'] = 'Товар должен принадлежать той же организации, что и заказ.'
        if self.source_price_channel_id and self.organization_id:
            if self.source_price_channel.organization_id != self.organization_id:
                errors['source_price_channel'] = 'Источник цены должен принадлежать той же организации.'
        if self.quantity is None or self.quantity <= 0:
            errors['quantity'] = 'Количество должно быть больше нуля.'
        if errors:
            raise ValidationError(errors)

    @property
    def line_total(self):
        return (self.sale_price or Decimal('0.00')) * Decimal(self.quantity or 0)

    def __str__(self):
        return f'{self.order} — {self.product} x {self.quantity}'


class Purchase(models.Model):
    class Status(models.TextChoices):
        DRAFT = ('draft', 'Черновик')
        POSTED = ('posted', 'Проведена')
        CANCELLED = ('cancelled', 'Отменена')

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='purchases',
    )
    purchase_date = models.DateField()
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='purchases',
        null=True,
        blank=True,
    )
    counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.SET_NULL,
        related_name='purchases',
        null=True,
        blank=True,
    )
    comment = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.POSTED)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='crm_created_purchases',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-purchase_date', '-id']

    @property
    def affects_stock(self):
        return self.status == self.Status.POSTED

    def clean(self):
        errors = {}
        if self.warehouse_id and self.organization_id and self.warehouse.organization_id != self.organization_id:
            errors['warehouse'] = 'Склад должен принадлежать той же организации, что и закупка.'
        if self.counterparty_id and self.organization_id and self.counterparty.organization_id != self.organization_id:
            errors['counterparty'] = 'Контрагент должен принадлежать той же организации, что и закупка.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'Закупка от {self.purchase_date:%d.%m.%Y}'


class PurchaseItem(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='purchase_items',
    )
    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.CASCADE,
        related_name='items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='purchase_items',
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='purchase_items',
    )
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    extra_cost_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['purchase__purchase_date', 'id']

    def clean(self):
        errors = {}
        if self.purchase_id and self.organization_id and self.purchase.organization_id != self.organization_id:
            errors['purchase'] = 'Закупка должна принадлежать той же организации.'
        if self.purchase_id and self.warehouse_id and self.purchase.warehouse_id and self.purchase.warehouse_id != self.warehouse_id:
            errors['warehouse'] = 'Склад строки должен совпадать со складом закупки.'
        if self.product_id and self.organization_id and self.product.organization_id != self.organization_id:
            errors['product'] = 'Товар должен принадлежать той же организации, что и закупка.'
        if self.warehouse_id and self.organization_id and self.warehouse.organization_id != self.organization_id:
            errors['warehouse'] = 'Склад должен принадлежать той же организации, что и закупка.'
        if self.quantity is None or self.quantity <= 0:
            errors['quantity'] = 'Количество должно быть больше нуля.'
        if errors:
            raise ValidationError(errors)

    @property
    def extra_cost_per_unit(self):
        if not self.quantity:
            return Decimal('0.00')
        return (self.extra_cost_total / Decimal(self.quantity)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def line_total(self):
        return (self.unit_cost * self.quantity) + self.extra_cost_total

    def __str__(self):
        return f'{self.product} x {self.quantity}'


class StockLot(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='stock_lots',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='stock_lots',
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='stock_lots',
    )
    purchase_item = models.ForeignKey(
        PurchaseItem,
        on_delete=models.CASCADE,
        related_name='stock_lots',
    )
    initial_quantity = models.PositiveIntegerField()
    remaining_quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    extra_cost_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['purchase_item__purchase__purchase_date', 'id']

    def clean(self):
        errors = {}
        if self.purchase_item_id and self.organization_id and self.purchase_item.organization_id != self.organization_id:
            errors['purchase_item'] = 'Партия должна ссылаться на строку закупки той же организации.'
        if self.product_id and self.organization_id and self.product.organization_id != self.organization_id:
            errors['product'] = 'Товар должен принадлежать той же организации, что и партия.'
        if self.warehouse_id and self.organization_id and self.warehouse.organization_id != self.organization_id:
            errors['warehouse'] = 'Склад должен принадлежать той же организации, что и партия.'
        if (
            self.initial_quantity is not None
            and self.remaining_quantity is not None
            and self.remaining_quantity > self.initial_quantity
        ):
            errors['remaining_quantity'] = 'Остаток не может быть больше исходного количества.'
        if errors:
            raise ValidationError(errors)

    @property
    def unit_cost_with_extra(self):
        return self.unit_cost + self.extra_cost_per_unit

    def __str__(self):
        return f'Лот {self.product} @ {self.warehouse.name}'


class StockReservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = ('active', 'Активен')
        RELEASED = ('released', 'Освобожден')
        CONSUMED = ('consumed', 'Списан')

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='stock_reservations',
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    stock_lot = models.ForeignKey(
        StockLot,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['stock_lot__purchase_item__purchase__purchase_date', 'id']

    def clean(self):
        errors = {}
        if self.order_id and self.organization_id and self.order.organization_id != self.organization_id:
            errors['order'] = 'Заказ должен принадлежать той же организации.'
        if self.order_item_id and self.organization_id and self.order_item.organization_id != self.organization_id:
            errors['order_item'] = 'Строка заказа должна принадлежать той же организации.'
        if self.stock_lot_id and self.organization_id and self.stock_lot.organization_id != self.organization_id:
            errors['stock_lot'] = 'Лот должен принадлежать той же организации.'
        if self.order_item_id and self.order_id and self.order_item.order_id != self.order_id:
            errors['order_item'] = 'Строка заказа должна относиться к выбранному заказу.'
        if self.stock_lot_id and self.order_item_id and self.stock_lot.product_id != self.order_item.product_id:
            errors['stock_lot'] = 'Лот должен относиться к тому же товару, что и строка заказа.'
        if self.quantity is None or self.quantity <= 0:
            errors['quantity'] = 'Количество резерва должно быть больше нуля.'
        if errors:
            raise ValidationError(errors)

    @property
    def unit_cost_with_extra(self):
        return self.stock_lot.unit_cost_with_extra

    @property
    def total_cost(self):
        return self.unit_cost_with_extra * Decimal(self.quantity or 0)

    def __str__(self):
        return f'{self.order} -> {self.stock_lot} x {self.quantity}'


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        PURCHASE_IN = ('purchase_in', 'Поступление по закупке')
        SALE_OUT = ('sale_out', 'Списание по продаже')
        RESERVATION = ('reservation', 'Резерв')
        RESERVATION_RELEASE = ('reservation_release', 'Снятие резерва')
        TRANSFER = ('transfer', 'Перемещение')
        INVENTORY_ADJUSTMENT = ('inventory_adjustment', 'Корректировка инвентаризации')
        RETURN_IN = ('return_in', 'Возврат на склад')

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='stock_movements',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='stock_movements',
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='stock_movements',
    )
    stock_lot = models.ForeignKey(
        StockLot,
        on_delete=models.CASCADE,
        related_name='movements',
        null=True,
        blank=True,
    )
    movement_type = models.CharField(max_length=32, choices=MovementType.choices)
    quantity = models.IntegerField()
    related_purchase_item = models.ForeignKey(
        PurchaseItem,
        on_delete=models.CASCADE,
        related_name='stock_movements',
        null=True,
        blank=True,
    )
    comment = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='crm_stock_movements',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def clean(self):
        errors = {}
        if self.product_id and self.organization_id and self.product.organization_id != self.organization_id:
            errors['product'] = 'Товар должен принадлежать той же организации.'
        if self.warehouse_id and self.organization_id and self.warehouse.organization_id != self.organization_id:
            errors['warehouse'] = 'Склад должен принадлежать той же организации.'
        if self.stock_lot_id:
            if self.organization_id and self.stock_lot.organization_id != self.organization_id:
                errors['stock_lot'] = 'Лот должен принадлежать той же организации.'
            if self.product_id and self.stock_lot.product_id != self.product_id:
                errors['stock_lot'] = 'Лот должен относиться к выбранному товару.'
            if self.warehouse_id and self.stock_lot.warehouse_id != self.warehouse_id:
                errors['stock_lot'] = 'Лот должен относиться к выбранному складу.'
        if self.related_purchase_item_id and self.organization_id:
            if self.related_purchase_item.organization_id != self.organization_id:
                errors['related_purchase_item'] = 'Строка закупки должна принадлежать той же организации.'
        if self.movement_type == self.MovementType.PURCHASE_IN and (self.quantity is None or self.quantity <= 0):
            errors['quantity'] = 'Для поступления по закупке количество должно быть положительным.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.get_movement_type_display()} {self.product} x {self.quantity}'
