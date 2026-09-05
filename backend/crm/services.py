from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Prefetch, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    DeliveryMethod,
    Order,
    OrderItem,
    PurchaseItem,
    ProductChannelOffer,
    SalesChannel,
    StockLot,
    StockMovement,
    StockReservation,
    Warehouse,
)


DEFAULT_SALES_CHANNELS = [
    {'name': 'Ozon', 'is_listing_channel': True},
    {'name': 'Яндекс Маркет', 'is_listing_channel': True},
    {'name': 'Avito', 'is_listing_channel': True},
    {'name': 'Самовывоз', 'is_listing_channel': False},
]

DEFAULT_WAREHOUSES = [
    'Основной склад',
    'Ozon',
    'Яндекс Маркет',
]

DEFAULT_DELIVERY_METHODS = [
    {'name': 'Ozon доставка', 'code': 'ozon-delivery', 'sort_order': 10},
    {'name': 'Яндекс доставка', 'code': 'yandex-delivery', 'sort_order': 20},
    {'name': 'Avito доставка', 'code': 'avito-delivery', 'sort_order': 30},
    {'name': 'Курьер', 'code': 'courier', 'sort_order': 40},
    {'name': 'Самовывоз', 'code': 'pickup', 'sort_order': 50},
    {'name': 'Другое', 'code': 'other', 'sort_order': 60},
]

PURCHASE_TEMPLATE_HEADERS = [
    'sku',
    'quantity',
    'unit_cost',
    'extra_cost_total',
    'comment',
]

PRODUCT_IMPORT_TEMPLATE_HEADERS = [
    'sku',
    'name',
    'comment',
    'photo',
]

PRODUCT_IMPORT_HEADER_ALIASES = {
    'sku': [
        'sku',
        'sku / артикул',
        'артикул',
        'артикул / sku',
        'номер',
        'код товара',
    ],
    'name': [
        'name',
        'название',
        'название товара',
        'товар',
        'набор',
    ],
    'comment': [
        'comment',
        'комментарий',
        'примечание',
        'заметка',
        'описание',
    ],
    'is_active': [
        'is_active',
        'active',
        'активен',
        'активный',
        'в архиве',
    ],
    'photo': [
        'photo',
        'фото',
        'изображение',
        'картинка',
    ],
}


def initialize_organization_defaults(organization):
    for channel_data in DEFAULT_SALES_CHANNELS:
        SalesChannel.objects.get_or_create(
            organization=organization,
            name=channel_data['name'],
            defaults={
                'is_active': True,
                'is_listing_channel': channel_data['is_listing_channel'],
            },
        )

    for name in DEFAULT_WAREHOUSES:
        Warehouse.objects.get_or_create(
            organization=organization,
            name=name,
            defaults={'is_active': True},
        )

    for method_data in DEFAULT_DELIVERY_METHODS:
        DeliveryMethod.objects.get_or_create(
            organization=organization,
            code=method_data['code'],
            defaults={
                'name': method_data['name'],
                'is_active': True,
                'sort_order': method_data['sort_order'],
            },
        )

    return organization


def build_delivery_method_code(name, fallback='delivery-method'):
    normalized_code = slugify(name or '', allow_unicode=True)
    return normalized_code or fallback


def calculate_extra_cost_per_unit(quantity, extra_cost_total):
    if not quantity:
        return Decimal('0.00')
    normalized_extra_cost_total = Decimal(str(extra_cost_total or '0'))
    return (normalized_extra_cost_total / Decimal(quantity)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def replace_purchase_items_and_stock(purchase, item_rows, created_by=None):
    organization = purchase.organization
    PurchaseItem.objects.filter(purchase=purchase).delete()

    created_items = []
    for row in item_rows:
        warehouse = row.get('warehouse') or purchase.warehouse
        if warehouse is None:
            raise ValueError('Для закупки не указан склад.')

        purchase_item = PurchaseItem.objects.create(
            organization=organization,
            purchase=purchase,
            product=row['product'],
            warehouse=warehouse,
            quantity=row['quantity'],
            unit_cost=row['unit_cost'],
            extra_cost_total=row['extra_cost_total'],
            comment=row['comment'],
        )
        if not purchase.affects_stock:
            created_items.append(purchase_item)
            continue

        extra_cost_per_unit = calculate_extra_cost_per_unit(
            quantity=purchase_item.quantity,
            extra_cost_total=purchase_item.extra_cost_total,
        )
        stock_lot = StockLot.objects.create(
            organization=organization,
            product=purchase_item.product,
            warehouse=purchase_item.warehouse,
            purchase_item=purchase_item,
            initial_quantity=purchase_item.quantity,
            remaining_quantity=purchase_item.quantity,
            unit_cost=purchase_item.unit_cost,
            extra_cost_per_unit=extra_cost_per_unit,
        )
        StockMovement.objects.create(
            organization=organization,
            product=purchase_item.product,
            warehouse=purchase_item.warehouse,
            stock_lot=stock_lot,
            movement_type=StockMovement.MovementType.PURCHASE_IN,
            quantity=purchase_item.quantity,
            related_purchase_item=purchase_item,
            comment=purchase_item.comment,
            created_by=created_by,
        )
        created_items.append(purchase_item)

    return created_items


def get_order_channel_offer_price_map(products, sales_channel):
    if not sales_channel:
        return {}

    product_ids = [product.id for product in products if product is not None]
    if not product_ids:
        return {}

    return {
        offer.product_id: offer.price
        for offer in ProductChannelOffer.objects.filter(
            product_id__in=product_ids,
            channel=sales_channel,
            is_enabled=True,
            price__isnull=False,
        )
    }


def reserve_stock_for_order_item(order_item):
    reserved_quantity_map = {
        row['stock_lot_id']: row['reserved_quantity']
        for row in StockReservation.objects.filter(
            organization=order_item.organization,
            stock_lot__product=order_item.product,
            status=StockReservation.Status.ACTIVE,
        )
        .values('stock_lot_id')
        .annotate(
            reserved_quantity=Coalesce(
                Sum('quantity'),
                Value(0),
            )
        )
    }

    stock_lots = list(
        StockLot.objects.filter(
            organization=order_item.organization,
            product=order_item.product,
            remaining_quantity__gt=0,
        )
        .select_related('warehouse', 'purchase_item__purchase')
        .order_by('purchase_item__purchase__purchase_date', 'created_at', 'id')
    )

    remaining_to_reserve = order_item.quantity
    reservations = []
    for stock_lot in stock_lots:
        already_reserved = int(reserved_quantity_map.get(stock_lot.id, 0))
        available_quantity = max(stock_lot.remaining_quantity - already_reserved, 0)
        if available_quantity <= 0:
            continue

        reserve_quantity = min(remaining_to_reserve, available_quantity)
        reservation = StockReservation.objects.create(
            organization=order_item.organization,
            order=order_item.order,
            order_item=order_item,
            stock_lot=stock_lot,
            quantity=reserve_quantity,
            status=StockReservation.Status.ACTIVE,
        )
        reservations.append(reservation)
        remaining_to_reserve -= reserve_quantity
        if remaining_to_reserve <= 0:
            break

    return reservations


def release_order_reservations(order):
    StockReservation.objects.filter(
        order=order,
        status=StockReservation.Status.ACTIVE,
    ).update(
        status=StockReservation.Status.RELEASED,
        updated_at=timezone.now(),
    )


def _normalize_order_status_after_reservation(order, is_fully_reserved):
    if order.status == Order.Status.CANCELLED:
        return

    if order.status in {Order.Status.NEW, Order.Status.AWAITING_SHIPMENT}:
        next_status = Order.Status.AWAITING_SHIPMENT if is_fully_reserved else Order.Status.NEW
        if order.status != next_status:
            order.status = next_status
            order.save(update_fields=['status', 'updated_at'])


def replace_order_items_and_reservations(order, item_rows):
    order.items.all().delete()

    created_items = []
    products = [row['product'] for row in item_rows]
    channel_price_map = get_order_channel_offer_price_map(products, order.sales_channel)
    missing_price_product_ids = set()
    shortage_item_ids = set()

    for row in item_rows:
        resolved_price = row['sale_price']
        source_price_channel = None
        if resolved_price is None:
            resolved_price = channel_price_map.get(row['product'].id)
            if resolved_price is not None:
                source_price_channel = order.sales_channel
            else:
                missing_price_product_ids.add(row['product'].id)

        order_item = OrderItem.objects.create(
            organization=order.organization,
            order=order,
            product=row['product'],
            quantity=row['quantity'],
            sale_price=resolved_price,
            source_price_channel=source_price_channel,
            comment=row['comment'],
        )
        created_items.append(order_item)

        if order.status == Order.Status.CANCELLED:
            continue

        reservations = reserve_stock_for_order_item(order_item)
        reserved_quantity = sum(reservation.quantity for reservation in reservations)
        if reserved_quantity < order_item.quantity:
            shortage_item_ids.add(order_item.id)

    is_fully_reserved = bool(created_items) and not shortage_item_ids and order.status != Order.Status.CANCELLED
    _normalize_order_status_after_reservation(order, is_fully_reserved)

    return {
        'items': created_items,
        'is_fully_reserved': is_fully_reserved,
        'shortage_item_ids': shortage_item_ids,
        'missing_price_product_ids': missing_price_product_ids,
    }


def get_order_detail_summary(order):
    items = list(
        order.items.select_related('product', 'source_price_channel').prefetch_related(
            Prefetch(
                'reservations',
                queryset=StockReservation.objects.select_related(
                    'stock_lot__warehouse',
                    'stock_lot__purchase_item__purchase',
                ).order_by('stock_lot__purchase_item__purchase__purchase_date', 'id'),
            )
        )
    )

    total_quantity = 0
    total_sale_amount = Decimal('0.00')
    total_reserved_cost = Decimal('0.00')
    missing_quantity_total = 0
    has_active_reservations = False

    for item in items:
        active_reservations = [
            reservation for reservation in item.reservations.all()
            if reservation.status == StockReservation.Status.ACTIVE
        ]
        item.active_reservations = active_reservations
        item.reserved_quantity = sum(reservation.quantity for reservation in active_reservations)
        item.shortage_quantity = max(item.quantity - item.reserved_quantity, 0)
        item.line_total_value = item.line_total
        item.reserved_cost_total = sum(
            (reservation.total_cost for reservation in active_reservations),
            Decimal('0.00'),
        )

        total_quantity += item.quantity
        total_sale_amount += item.line_total_value
        total_reserved_cost += item.reserved_cost_total
        missing_quantity_total += item.shortage_quantity
        has_active_reservations = has_active_reservations or bool(active_reservations)

    if order.status == Order.Status.CANCELLED:
        reservation_status_code = 'released'
        reservation_status_label = 'Резерв снят'
    elif missing_quantity_total == 0 and items:
        reservation_status_code = 'full'
        reservation_status_label = 'Полный резерв'
    elif has_active_reservations:
        reservation_status_code = 'partial'
        reservation_status_label = 'Частичный резерв'
    else:
        reservation_status_code = 'none'
        reservation_status_label = 'Нет резерва'

    return {
        'items': items,
        'line_count': len(items),
        'total_quantity': total_quantity,
        'total_sale_amount': total_sale_amount,
        'total_reserved_cost': total_reserved_cost,
        'gross_profit': total_sale_amount - total_reserved_cost,
        'missing_quantity_total': missing_quantity_total,
        'has_active_reservations': has_active_reservations,
        'is_fully_reserved': bool(items) and missing_quantity_total == 0 and order.status != Order.Status.CANCELLED,
        'reservation_status_code': reservation_status_code,
        'reservation_status_label': reservation_status_label,
    }
