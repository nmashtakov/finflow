from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .access import crm_access_required, get_current_organization
from .forms import (
    OrganizationOnboardingForm,
    ProductChannelOfferForm,
    ProductForm,
    SalesChannelForm,
    WarehouseForm,
)
from .models import Membership, Product, ProductChannelOffer, SalesChannel, Warehouse
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


def _get_organization_object_or_404(request, model, object_id):
    return get_object_or_404(
        model,
        pk=object_id,
        organization=request.crm_organization,
    )


def _render_directory_form(
    request,
    form,
    page_title,
    submit_label,
    cancel_url,
):
    return render(
        request,
        'crm/directory_form.html',
        {
            'organization': request.crm_organization,
            'form': form,
            'page_title': page_title,
            'submit_label': submit_label,
            'cancel_url': cancel_url,
        },
    )


def _get_listing_channels(organization):
    return SalesChannel.objects.filter(
        organization=organization,
        is_active=True,
        is_listing_channel=True,
    ).order_by('name', 'id')


def _save_product_channel_offers(product, offer_form):
    for payload in offer_form.build_offer_payloads():
        existing_offer = payload['existing_offer']
        if existing_offer:
            existing_offer.is_enabled = payload['is_enabled']
            existing_offer.price = payload['price']
            existing_offer.comment = payload['comment']
            existing_offer.save(update_fields=['is_enabled', 'price', 'comment', 'updated_at'])
            continue

        ProductChannelOffer.objects.create(
            product=product,
            channel=payload['channel'],
            is_enabled=payload['is_enabled'],
            price=payload['price'],
            comment=payload['comment'],
        )


def _render_product_form(
    request,
    form,
    offer_form,
    page_title,
    submit_label,
):
    return render(
        request,
        'crm/product_form.html',
        {
            'organization': request.crm_organization,
            'form': form,
            'offer_form': offer_form,
            'page_title': page_title,
            'submit_label': submit_label,
        },
    )


@crm_access_required
def settings_view(request):
    organization = request.crm_organization
    sales_channels = SalesChannel.objects.filter(organization=organization).order_by('is_active', 'name', 'id')
    warehouses = Warehouse.objects.filter(organization=organization).order_by('is_active', 'name', 'id')

    return render(
        request,
        'crm/settings.html',
        {
            'organization': organization,
            'active_sales_channels': sales_channels.filter(is_active=True),
            'archived_sales_channels': sales_channels.filter(is_active=False),
            'active_warehouses': warehouses.filter(is_active=True),
            'archived_warehouses': warehouses.filter(is_active=False),
        },
    )


@crm_access_required
def sales_channel_create_view(request):
    if request.method == 'POST':
        form = SalesChannelForm(request.POST)
        if form.is_valid():
            sales_channel = form.save(commit=False)
            sales_channel.organization = request.crm_organization
            sales_channel.save()
            messages.success(request, 'Канал продаж создан.')
            return redirect('crm:settings')
    else:
        form = SalesChannelForm()

    return _render_directory_form(
        request,
        form,
        page_title='Новый канал продаж',
        submit_label='Создать канал',
        cancel_url='crm:settings',
    )


@crm_access_required
def sales_channel_edit_view(request, channel_id):
    sales_channel = _get_organization_object_or_404(request, SalesChannel, channel_id)

    if request.method == 'POST':
        form = SalesChannelForm(request.POST, instance=sales_channel)
        if form.is_valid():
            form.save()
            messages.success(request, 'Канал продаж обновлен.')
            return redirect('crm:settings')
    else:
        form = SalesChannelForm(instance=sales_channel)

    return _render_directory_form(
        request,
        form,
        page_title=f'Редактирование канала продаж: {sales_channel.name}',
        submit_label='Сохранить',
        cancel_url='crm:settings',
    )


@crm_access_required
@require_POST
def sales_channel_archive_view(request, channel_id):
    sales_channel = _get_organization_object_or_404(request, SalesChannel, channel_id)
    sales_channel.is_active = False
    sales_channel.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Канал продаж отправлен в архив.')
    return redirect('crm:settings')


@crm_access_required
@require_POST
def sales_channel_restore_view(request, channel_id):
    sales_channel = _get_organization_object_or_404(request, SalesChannel, channel_id)
    sales_channel.is_active = True
    sales_channel.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Канал продаж восстановлен из архива.')
    return redirect('crm:settings')


@crm_access_required
def products_view(request):
    organization = request.crm_organization
    sku_query = (request.GET.get('sku') or '').strip()
    name_query = (request.GET.get('name') or '').strip()
    products = (
        Product.objects.filter(organization=organization)
        .prefetch_related(
            Prefetch(
                'channel_offers',
                queryset=ProductChannelOffer.objects.select_related('channel').filter(
                    is_enabled=True,
                    channel__is_active=True,
                    channel__is_listing_channel=True,
                ),
                to_attr='enabled_listing_offers',
            )
        )
        .order_by('-is_active', 'name', 'sku', 'id')
    )

    if sku_query:
        products = products.filter(sku__icontains=sku_query)
    if name_query:
        products = products.filter(name__icontains=name_query)

    return render(
        request,
        'crm/products.html',
        {
            'organization': organization,
            'products': products,
            'filters': {
                'sku': sku_query,
                'name': name_query,
            },
        },
    )


@crm_access_required
def product_create_view(request):
    listing_channels = list(_get_listing_channels(request.crm_organization))

    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, organization=request.crm_organization)
        offer_form = ProductChannelOfferForm(request.POST, channels=listing_channels)
        if form.is_valid() and offer_form.is_valid():
            with transaction.atomic():
                product = form.save(commit=False)
                product.organization = request.crm_organization
                product.save()
                _save_product_channel_offers(product, offer_form)
            messages.success(request, 'Товар создан.')
            return redirect('crm:products')
    else:
        form = ProductForm(initial={'is_active': True}, organization=request.crm_organization)
        offer_form = ProductChannelOfferForm(channels=listing_channels)

    return _render_product_form(
        request,
        form,
        offer_form,
        page_title='Новый товар',
        submit_label='Создать товар',
    )


@crm_access_required
def product_edit_view(request, product_id):
    product = _get_organization_object_or_404(request, Product, product_id)
    listing_channels = list(_get_listing_channels(request.crm_organization))
    existing_offers = list(
        ProductChannelOffer.objects.filter(
            product=product,
            channel__organization=request.crm_organization,
            channel__is_listing_channel=True,
        ).select_related('channel')
    )

    if request.method == 'POST':
        form = ProductForm(
            request.POST,
            request.FILES,
            instance=product,
            organization=request.crm_organization,
        )
        offer_form = ProductChannelOfferForm(
            request.POST,
            channels=listing_channels,
            offers=existing_offers,
        )
        if form.is_valid() and offer_form.is_valid():
            with transaction.atomic():
                form.save()
                _save_product_channel_offers(product, offer_form)
            messages.success(request, 'Товар обновлен.')
            return redirect('crm:products')
    else:
        form = ProductForm(instance=product, organization=request.crm_organization)
        offer_form = ProductChannelOfferForm(channels=listing_channels, offers=existing_offers)

    return _render_product_form(
        request,
        form,
        offer_form,
        page_title=f'Редактирование товара: {product.name}',
        submit_label='Сохранить',
    )


@crm_access_required
@require_POST
def product_archive_view(request, product_id):
    product = _get_organization_object_or_404(request, Product, product_id)
    product.is_active = False
    product.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Товар отправлен в архив.')
    return redirect('crm:products')


@crm_access_required
def warehouse_create_view(request):
    if request.method == 'POST':
        form = WarehouseForm(request.POST)
        if form.is_valid():
            warehouse = form.save(commit=False)
            warehouse.organization = request.crm_organization
            warehouse.save()
            messages.success(request, 'Склад создан.')
            return redirect('crm:settings')
    else:
        form = WarehouseForm()

    return _render_directory_form(
        request,
        form,
        page_title='Новый склад',
        submit_label='Создать склад',
        cancel_url='crm:settings',
    )


@crm_access_required
def warehouse_edit_view(request, warehouse_id):
    warehouse = _get_organization_object_or_404(request, Warehouse, warehouse_id)

    if request.method == 'POST':
        form = WarehouseForm(request.POST, instance=warehouse)
        if form.is_valid():
            form.save()
            messages.success(request, 'Склад обновлен.')
            return redirect('crm:settings')
    else:
        form = WarehouseForm(instance=warehouse)

    return _render_directory_form(
        request,
        form,
        page_title=f'Редактирование склада: {warehouse.name}',
        submit_label='Сохранить',
        cancel_url='crm:settings',
    )


@crm_access_required
@require_POST
def warehouse_archive_view(request, warehouse_id):
    warehouse = _get_organization_object_or_404(request, Warehouse, warehouse_id)
    warehouse.is_active = False
    warehouse.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Склад отправлен в архив.')
    return redirect('crm:settings')


@crm_access_required
@require_POST
def warehouse_restore_view(request, warehouse_id):
    warehouse = _get_organization_object_or_404(request, Warehouse, warehouse_id)
    warehouse.is_active = True
    warehouse.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Склад восстановлен из архива.')
    return redirect('crm:settings')


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
