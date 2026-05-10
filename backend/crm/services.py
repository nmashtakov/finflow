from .models import SalesChannel, Warehouse


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

    return organization
