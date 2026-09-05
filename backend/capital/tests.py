from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from capital.models import CapitalPosition, CapitalSnapshot, SteamAccount, WithdrawalSite
from capital.services import (
    copy_snapshots_from_previous_date,
    ensure_steam_positions,
    get_capital_at_date,
    get_capital_timeseries,
)
from core.models import Account, AccountBalanceSnapshot, Project


class CapitalServicesTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='capital_user', password='test')
        self.project = Project.objects.create(user=self.user, name='Crypto')
        self.account_included = Account.objects.create(
            user=self.user,
            name='Tinkoff',
            currency='RUB',
            include_in_total=True,
        )
        self.account_excluded = Account.objects.create(
            user=self.user,
            name='Alina',
            currency='RUB',
            include_in_total=False,
        )
        self.bank_position = CapitalPosition.objects.create(
            user=self.user,
            name='Tinkoff',
            kind=CapitalPosition.Kind.BANK_LINK,
            linked_account=self.account_included,
            currency='RUB',
        )
        CapitalPosition.objects.create(
            user=self.user,
            name='Alina',
            kind=CapitalPosition.Kind.BANK_LINK,
            linked_account=self.account_excluded,
            currency='RUB',
            include_in_total=False,
        )
        self.btc = CapitalPosition.objects.create(
            user=self.user,
            project=self.project,
            name='BTC',
            kind=CapitalPosition.Kind.INSTRUMENT,
            asset_class=CapitalPosition.AssetClass.CRYPTO,
            currency='USD',
        )
        AccountBalanceSnapshot.objects.create(
            account=self.account_included,
            snapshot_date=date(2026, 5, 1),
            balance=Decimal('100000'),
        )
        CapitalSnapshot.objects.create(
            position=self.btc,
            snapshot_date=date(2026, 5, 1),
            quantity=Decimal('1'),
            unit_price=Decimal('50000'),
            total_value=Decimal('50000'),
        )

    def test_steam_account_code_validation(self):
        form_data = {
            'account_code': 'INVALID',
            'currency': 'USD',
            'purpose': SteamAccount.Purpose.INVESTMENT,
            'is_active': True,
        }
        acc = SteamAccount(user=self.user, **{k: v for k, v in form_data.items() if k != 'is_active'})
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            acc.full_clean()

    def test_steam_adjusted_value(self):
        site = WithdrawalSite.objects.create(user=self.user, name='TM', default_coefficient=Decimal('0.85'))
        steam = SteamAccount.objects.create(user=self.user, account_code='MN2', currency='USD')
        ensure_steam_positions(self.user)
        position = CapitalPosition.objects.get(steam_account=steam, withdrawal_site=site)
        snap = CapitalSnapshot.objects.create(
            position=position,
            snapshot_date=date(2026, 5, 10),
            site_balance=Decimal('500'),
            skins_value=Decimal('200'),
            coefficient=Decimal('0.85'),
            total_value=Decimal('0'),
        )
        snap.total_value = snap.compute_total_value()
        snap.save()
        self.assertEqual(snap.total_value, Decimal('595'))

    def test_exclude_account_from_total(self):
        result = get_capital_at_date(self.user, date(2026, 5, 10), 'RUB')
        position_names = [p['name'] for p in result['positions'] if p['included'] and p['converted_value']]
        self.assertIn('Tinkoff', position_names)
        alina_rows = [p for p in result['positions'] if p['name'] == 'Alina']
        self.assertTrue(alina_rows)
        self.assertFalse(alina_rows[0]['included'])

    def test_forward_fill_timeseries(self):
        CapitalSnapshot.objects.create(
            position=self.btc,
            snapshot_date=date(2026, 5, 5),
            quantity=Decimal('2'),
            unit_price=Decimal('50000'),
            total_value=Decimal('100000'),
        )
        series = get_capital_timeseries(
            self.user,
            date(2026, 5, 3),
            date(2026, 5, 7),
            'USD',
            'day',
        )
        values_by_date = {p['date']: p['total'] for p in series}
        self.assertEqual(values_by_date['2026-05-03'], float(Decimal('50000')))
        self.assertEqual(values_by_date['2026-05-05'], float(Decimal('100000')))
        self.assertEqual(values_by_date['2026-05-07'], float(Decimal('100000')))

    def test_copy_snapshots_from_previous_date(self):
        copied = copy_snapshots_from_previous_date(self.user, date(2026, 5, 15))
        self.assertEqual(copied, 1)
        self.assertTrue(
            CapitalSnapshot.objects.filter(position=self.btc, snapshot_date=date(2026, 5, 15)).exists()
        )

    def test_withdrawal_site_default_coefficient_is_one(self):
        site = WithdrawalSite.objects.create(user=self.user, name='Plain')
        self.assertEqual(site.default_coefficient, Decimal('1'))

    def test_ensure_steam_positions_creates_pairs(self):
        WithdrawalSite.objects.create(user=self.user, name='TM')
        SteamAccount.objects.create(user=self.user, account_code='AB1')
        created = ensure_steam_positions(self.user)
        self.assertEqual(created, 1)
        self.assertEqual(
            CapitalPosition.objects.filter(kind=CapitalPosition.Kind.STEAM).count(),
            1,
        )
