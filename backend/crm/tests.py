from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from .access import get_current_organization, user_has_crm_access
from .models import Membership, Organization, Product, ProductChannelOffer, SalesChannel, Warehouse
from .services import DEFAULT_SALES_CHANNELS, DEFAULT_WAREHOUSES


TEST_GIF_BYTES = (
    b'GIF89a\x01\x00\x01\x00\x80\x00\x00'
    b'\x00\x00\x00\xff\xff\xff!\xf9\x04'
    b'\x01\x00\x00\x00\x00,\x00\x00\x00'
    b'\x00\x01\x00\x01\x00\x00\x02\x02D'
    b'\x01\x00;'
)


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
        self.assertCountEqual(
            SalesChannel.objects.filter(organization=organization).values_list('name', flat=True),
            [item['name'] for item in DEFAULT_SALES_CHANNELS],
        )
        self.assertCountEqual(
            Warehouse.objects.filter(organization=organization).values_list('name', flat=True),
            DEFAULT_WAREHOUSES,
        )
        self.assertTrue(
            SalesChannel.objects.get(organization=organization, name='Ozon').is_listing_channel
        )
        self.assertTrue(
            SalesChannel.objects.get(organization=organization, name='Яндекс Маркет').is_listing_channel
        )
        self.assertTrue(
            SalesChannel.objects.get(organization=organization, name='Avito').is_listing_channel
        )
        self.assertFalse(
            SalesChannel.objects.get(organization=organization, name='Самовывоз').is_listing_channel
        )

        dashboard_response = self.client.get(reverse('crm:dashboard'))
        self.assertContains(dashboard_response, 'LEGO бизнес')
        self.assertContains(dashboard_response, 'Справочники')
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


class CRMSettingsDirectoriesTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='crm-settings-owner', password='password123')
        self.other_owner = User.objects.create_user(username='crm-settings-other', password='password123')

        self.organization = Organization.objects.create(
            owner=self.owner,
            name='Brick Trade',
            base_currency='RUB',
        )
        self.other_organization = Organization.objects.create(
            owner=self.other_owner,
            name='Other Org',
            base_currency='RUB',
        )

        Membership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=Membership.Role.OWNER,
        )
        Membership.objects.create(
            organization=self.other_organization,
            user=self.other_owner,
            role=Membership.Role.OWNER,
        )

        self.sales_channel = SalesChannel.objects.create(
            organization=self.organization,
            name='Ozon',
        )
        self.archived_sales_channel = SalesChannel.objects.create(
            organization=self.organization,
            name='Старый канал',
            is_active=False,
        )
        self.other_sales_channel = SalesChannel.objects.create(
            organization=self.other_organization,
            name='Чужой канал',
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            name='Основной склад',
        )
        self.archived_warehouse = Warehouse.objects.create(
            organization=self.organization,
            name='Старый склад',
            is_active=False,
        )
        self.other_warehouse = Warehouse.objects.create(
            organization=self.other_organization,
            name='Чужой склад',
        )

    def test_settings_page_shows_only_current_organization_records(self):
        self.client.login(username='crm-settings-owner', password='password123')

        response = self.client.get(reverse('crm:settings'))

        self.assertContains(response, 'Ozon')
        self.assertContains(response, 'Старый канал')
        self.assertContains(response, 'Основной склад')
        self.assertContains(response, 'Старый склад')
        self.assertNotContains(response, 'Чужой канал')
        self.assertNotContains(response, 'Чужой склад')

    def test_user_can_create_edit_archive_and_restore_sales_channel_in_own_organization(self):
        self.client.login(username='crm-settings-owner', password='password123')

        create_response = self.client.post(
            reverse('crm:sales_channel_create'),
            {'name': 'Wildberries', 'is_listing_channel': 'on'},
        )
        self.assertRedirects(create_response, reverse('crm:settings'))

        created_channel = SalesChannel.objects.get(name='Wildberries')
        self.assertEqual(created_channel.organization, self.organization)
        self.assertTrue(created_channel.is_active)
        self.assertTrue(created_channel.is_listing_channel)

        edit_response = self.client.post(
            reverse('crm:sales_channel_edit', args=[created_channel.id]),
            {'name': 'WB'},
        )
        self.assertRedirects(edit_response, reverse('crm:settings'))
        created_channel.refresh_from_db()
        self.assertEqual(created_channel.name, 'WB')
        self.assertFalse(created_channel.is_listing_channel)

        archive_response = self.client.post(
            reverse('crm:sales_channel_archive', args=[created_channel.id]),
        )
        self.assertRedirects(archive_response, reverse('crm:settings'))
        created_channel.refresh_from_db()
        self.assertFalse(created_channel.is_active)

        restore_response = self.client.post(
            reverse('crm:sales_channel_restore', args=[created_channel.id]),
        )
        self.assertRedirects(restore_response, reverse('crm:settings'))
        created_channel.refresh_from_db()
        self.assertTrue(created_channel.is_active)

    def test_user_can_create_edit_archive_and_restore_warehouse_in_own_organization(self):
        self.client.login(username='crm-settings-owner', password='password123')

        create_response = self.client.post(
            reverse('crm:warehouse_create'),
            {'name': 'Региональный склад'},
        )
        self.assertRedirects(create_response, reverse('crm:settings'))

        warehouse = Warehouse.objects.get(name='Региональный склад')
        self.assertEqual(warehouse.organization, self.organization)
        self.assertTrue(warehouse.is_active)

        edit_response = self.client.post(
            reverse('crm:warehouse_edit', args=[warehouse.id]),
            {'name': 'Склад Север'},
        )
        self.assertRedirects(edit_response, reverse('crm:settings'))
        warehouse.refresh_from_db()
        self.assertEqual(warehouse.name, 'Склад Север')

        archive_response = self.client.post(
            reverse('crm:warehouse_archive', args=[warehouse.id]),
        )
        self.assertRedirects(archive_response, reverse('crm:settings'))
        warehouse.refresh_from_db()
        self.assertFalse(warehouse.is_active)

        restore_response = self.client.post(
            reverse('crm:warehouse_restore', args=[warehouse.id]),
        )
        self.assertRedirects(restore_response, reverse('crm:settings'))
        warehouse.refresh_from_db()
        self.assertTrue(warehouse.is_active)

    def test_user_cannot_edit_archive_or_restore_other_organization_records(self):
        self.client.login(username='crm-settings-owner', password='password123')

        edit_channel_response = self.client.post(
            reverse('crm:sales_channel_edit', args=[self.other_sales_channel.id]),
            {'name': 'Попытка'},
        )
        self.assertEqual(edit_channel_response.status_code, 404)

        archive_channel_response = self.client.post(
            reverse('crm:sales_channel_archive', args=[self.other_sales_channel.id]),
        )
        self.assertEqual(archive_channel_response.status_code, 404)

        restore_channel_response = self.client.post(
            reverse('crm:sales_channel_restore', args=[self.other_sales_channel.id]),
        )
        self.assertEqual(restore_channel_response.status_code, 404)

        edit_warehouse_response = self.client.post(
            reverse('crm:warehouse_edit', args=[self.other_warehouse.id]),
            {'name': 'Попытка'},
        )
        self.assertEqual(edit_warehouse_response.status_code, 404)

        archive_warehouse_response = self.client.post(
            reverse('crm:warehouse_archive', args=[self.other_warehouse.id]),
        )
        self.assertEqual(archive_warehouse_response.status_code, 404)

        restore_warehouse_response = self.client.post(
            reverse('crm:warehouse_restore', args=[self.other_warehouse.id]),
        )
        self.assertEqual(restore_warehouse_response.status_code, 404)


class CRMProductsTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='crm-products-owner', password='password123')
        self.other_owner = User.objects.create_user(username='crm-products-other', password='password123')

        self.organization = Organization.objects.create(
            owner=self.owner,
            name='Brick Trade',
            base_currency='RUB',
        )
        self.other_organization = Organization.objects.create(
            owner=self.other_owner,
            name='Other Org',
            base_currency='RUB',
        )

        Membership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=Membership.Role.OWNER,
        )
        Membership.objects.create(
            organization=self.other_organization,
            user=self.other_owner,
            role=Membership.Role.OWNER,
        )

        self.ozon = SalesChannel.objects.create(
            organization=self.organization,
            name='Ozon',
            is_listing_channel=True,
        )
        self.avito = SalesChannel.objects.create(
            organization=self.organization,
            name='Avito',
            is_listing_channel=True,
        )
        self.pickup = SalesChannel.objects.create(
            organization=self.organization,
            name='Самовывоз',
            is_listing_channel=False,
        )
        self.archived_listing_channel = SalesChannel.objects.create(
            organization=self.organization,
            name='Старый маркетплейс',
            is_listing_channel=True,
            is_active=False,
        )
        self.other_listing_channel = SalesChannel.objects.create(
            organization=self.other_organization,
            name='Чужой канал',
            is_listing_channel=True,
        )

        self.product = Product.objects.create(
            organization=self.organization,
            sku='SKU-001',
            name='Базовый товар',
            comment='Тестовый комментарий',
        )
        ProductChannelOffer.objects.create(
            product=self.product,
            channel=self.ozon,
            is_enabled=True,
            price='9990.00',
            comment='Основной канал',
        )
        ProductChannelOffer.objects.create(
            product=self.product,
            channel=self.avito,
            is_enabled=False,
            price=None,
            comment='Пока выключен',
        )

        self.other_product = Product.objects.create(
            organization=self.other_organization,
            sku='SKU-999',
            name='Чужой товар',
        )
        ProductChannelOffer.objects.create(
            product=self.other_product,
            channel=self.other_listing_channel,
            is_enabled=True,
            price='555.00',
        )

    def test_products_page_filters_to_current_organization_and_supports_search(self):
        self.client.login(username='crm-products-owner', password='password123')

        response = self.client.get(reverse('crm:products'))

        self.assertContains(response, 'SKU-001')
        self.assertContains(response, 'Базовый товар')
        self.assertNotContains(response, 'SKU-999')
        self.assertNotContains(response, 'Чужой товар')

        search_response = self.client.get(reverse('crm:products'), {'sku': '001', 'name': 'Базовый'})
        self.assertContains(search_response, 'SKU-001')
        self.assertNotContains(search_response, 'SKU-999')

    def test_product_create_shows_only_listing_channels_and_creates_offers_with_photo(self):
        self.client.login(username='crm-products-owner', password='password123')

        page_response = self.client.get(reverse('crm:product_create'))
        self.assertContains(page_response, 'Ozon')
        self.assertContains(page_response, 'Avito')
        self.assertNotContains(page_response, 'Самовывоз')
        self.assertNotContains(page_response, 'Старый маркетплейс')
        self.assertNotContains(page_response, 'Чужой канал')

        response = self.client.post(
            reverse('crm:product_create'),
            {
                'sku': 'SKU-NEW',
                'name': 'Новый товар',
                'comment': 'Комментарий',
                'is_active': 'on',
                'photo': SimpleUploadedFile(
                    'product.gif',
                    TEST_GIF_BYTES,
                    content_type='image/gif',
                ),
                f'channel_{self.ozon.id}_enabled': 'on',
                f'channel_{self.ozon.id}_price': '9990',
                f'channel_{self.ozon.id}_comment': 'Цена для Ozon',
                f'channel_{self.avito.id}_enabled': 'on',
                f'channel_{self.avito.id}_price': '9500',
                f'channel_{self.avito.id}_comment': 'Цена для Avito',
                f'channel_{self.other_listing_channel.id}_enabled': 'on',
                f'channel_{self.other_listing_channel.id}_price': '1',
            },
            follow=False,
        )

        self.assertRedirects(response, reverse('crm:products'))
        product = Product.objects.get(sku='SKU-NEW')
        self.assertEqual(product.organization, self.organization)
        self.assertTrue(product.is_active)
        self.assertTrue(product.photo.name.startswith('crm/products/'))

        offers = ProductChannelOffer.objects.filter(product=product).order_by('channel__name')
        self.assertEqual(offers.count(), 2)
        self.assertCountEqual(offers.values_list('channel__name', flat=True), ['Ozon', 'Avito'])
        self.assertEqual(str(ProductChannelOffer.objects.get(product=product, channel=self.ozon).price), '9990.00')
        self.assertFalse(
            ProductChannelOffer.objects.filter(product=product, channel=self.other_listing_channel).exists()
        )

    def test_product_create_requires_price_for_enabled_channel(self):
        self.client.login(username='crm-products-owner', password='password123')

        response = self.client.post(
            reverse('crm:product_create'),
            {
                'sku': 'SKU-ERR',
                'name': 'Товар без цены',
                'is_active': 'on',
                f'channel_{self.ozon.id}_enabled': 'on',
                f'channel_{self.ozon.id}_price': '',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Укажите цену для канала, если товар на нем размещается.')
        self.assertFalse(Product.objects.filter(sku='SKU-ERR').exists())

    def test_product_edit_updates_fields_and_offer_matrix(self):
        self.client.login(username='crm-products-owner', password='password123')

        response = self.client.post(
            reverse('crm:product_edit', args=[self.product.id]),
            {
                'sku': 'SKU-001-UPDATED',
                'name': 'Обновленный товар',
                'comment': 'Новый комментарий',
                'is_active': 'on',
                f'channel_{self.ozon.id}_price': '',
                f'channel_{self.ozon.id}_comment': 'Отключили Ozon',
                f'channel_{self.avito.id}_enabled': 'on',
                f'channel_{self.avito.id}_price': '9300',
                f'channel_{self.avito.id}_comment': 'Включили Avito',
            },
        )

        self.assertRedirects(response, reverse('crm:products'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.sku, 'SKU-001-UPDATED')
        self.assertEqual(self.product.name, 'Обновленный товар')
        self.assertEqual(self.product.comment, 'Новый комментарий')

        ozon_offer = ProductChannelOffer.objects.get(product=self.product, channel=self.ozon)
        avito_offer = ProductChannelOffer.objects.get(product=self.product, channel=self.avito)
        self.assertFalse(ozon_offer.is_enabled)
        self.assertIsNone(ozon_offer.price)
        self.assertEqual(ozon_offer.comment, 'Отключили Ozon')
        self.assertTrue(avito_offer.is_enabled)
        self.assertEqual(str(avito_offer.price), '9300.00')
        self.assertEqual(avito_offer.comment, 'Включили Avito')

    def test_product_edit_page_loads_with_existing_photo(self):
        self.client.login(username='crm-products-owner', password='password123')
        self.product.photo = SimpleUploadedFile(
            'existing-product.gif',
            TEST_GIF_BYTES,
            content_type='image/gif',
        )
        self.product.save(update_fields=['photo', 'updated_at'])

        response = self.client.get(reverse('crm:product_edit', args=[self.product.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-photo-trigger')
        self.assertContains(response, 'data-photo-preview')

    def test_product_archive_sets_inactive_flag(self):
        self.client.login(username='crm-products-owner', password='password123')

        response = self.client.post(reverse('crm:product_archive', args=[self.product.id]))

        self.assertRedirects(response, reverse('crm:products'))
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

    def test_user_cannot_open_or_change_other_organization_product(self):
        self.client.login(username='crm-products-owner', password='password123')

        edit_page_response = self.client.get(reverse('crm:product_edit', args=[self.other_product.id]))
        self.assertEqual(edit_page_response.status_code, 404)

        edit_response = self.client.post(
            reverse('crm:product_edit', args=[self.other_product.id]),
            {
                'sku': 'HACK',
                'name': 'Попытка',
            },
        )
        self.assertEqual(edit_response.status_code, 404)

        archive_response = self.client.post(reverse('crm:product_archive', args=[self.other_product.id]))
        self.assertEqual(archive_response.status_code, 404)

    def test_product_sku_is_unique_within_organization(self):
        self.client.login(username='crm-products-owner', password='password123')

        response = self.client.post(
            reverse('crm:product_create'),
            {
                'sku': 'SKU-001',
                'name': 'Дубликат',
                'is_active': 'on',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Товар с таким SKU / артикулом уже существует в этой организации.')
        self.assertEqual(Product.objects.filter(organization=self.organization, sku='SKU-001').count(), 1)
