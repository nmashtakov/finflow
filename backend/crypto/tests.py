from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import CurrencyRate
from crypto.models import CryptoAsset, CryptoTransaction
from crypto.services import (
    _build_extended_asset_map,
    _effective_buy_price,
    _parse_convert_event,
    _parse_convert_sell_event,
    _parse_trade_events,
    ensure_default_assets,
    ensure_legacy_manual_transactions,
    get_portfolio_summary,
    sync_crypto_rates,
)
from transactions.models import BybitConnection, BybitExternalEvent


class CryptoServicesTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='crypto_user', password='test')
        ensure_default_assets(self.user)
        self.btc = CryptoAsset.objects.get(user=self.user, symbol='BTC')
        self.gram = CryptoAsset.objects.get(user=self.user, symbol='GRAM')
        CurrencyRate.objects.create(date=date(2026, 7, 4), currency='USD', amount=Decimal('90'))
        CurrencyRate.objects.create(date=date(2026, 7, 4), currency='BTC', amount=Decimal('5850000'))

    def test_ensure_default_assets(self):
        created = ensure_default_assets(self.user)
        self.assertEqual(created, 0)
        self.assertEqual(CryptoAsset.objects.filter(user=self.user).count(), 8)

    def test_portfolio_summary_from_manual_buy(self):
        CryptoTransaction.objects.create(
            user=self.user,
            asset=self.btc,
            tx_type=CryptoTransaction.TxType.BUY,
            occurred_at=timezone.now(),
            quantity=Decimal('0.001'),
            price_quote=Decimal('60000'),
            total_quote=Decimal('60'),
            fee_quote=Decimal('0.1'),
            source=CryptoTransaction.Source.MANUAL,
        )
        summary = get_portfolio_summary(self.user, date(2026, 7, 4))
        self.assertEqual(len(summary['assets']), 1)
        row = summary['assets'][0]
        self.assertEqual(row.symbol, 'BTC')
        self.assertEqual(row.cost_basis, Decimal('60.10000000'))
        self.assertGreater(row.current_value, Decimal('0'))

    def test_parse_convert_event(self):
        connection = BybitConnection.objects.create(
            user=self.user,
            api_key='key',
            api_secret='secret',
        )
        event = BybitExternalEvent.objects.create(
            connection=connection,
            stream='convert_history',
            external_id='conv-1',
            event_hash='hash-1',
            occurred_at=timezone.now(),
            asset='USDT',
            amount=Decimal('-15'),
            raw_payload={
                'fromCoin': 'USDT',
                'toCoin': 'BTC',
                'fromAmount': '15',
                'toAmount': '0.0002',
                'fee': '0.027',
            },
        )
        asset_map = {self.btc.symbol: self.btc}
        parsed = _parse_convert_event(event, asset_map)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['asset'], self.btc)
        self.assertEqual(parsed['quantity'], Decimal('0.0002'))
        self.assertEqual(parsed['total_quote'], Decimal('15'))

    def test_sync_crypto_rates_requires_usd(self):
        CurrencyRate.objects.filter(currency='USD').delete()
        result = sync_crypto_rates(self.user, date(2026, 7, 4))
        self.assertTrue(result['failed'])

    def test_effective_buy_price_includes_fee(self):
        price = _effective_buy_price(Decimal('0.001'), Decimal('60'), Decimal('0.1'))
        self.assertEqual(price, Decimal('60100.00000000'))

    def test_legacy_eth_transaction(self):
        ensure_legacy_manual_transactions(self.user)
        tx = CryptoTransaction.objects.get(user=self.user, external_key='manual:eth:2018-05-13-external')
        self.assertEqual(tx.quantity, Decimal('0.02464'))
        self.assertEqual(tx.total_quote, Decimal('32.40'))
        self.assertEqual(tx.source, CryptoTransaction.Source.MANUAL)

    def test_convert_ton_maps_to_gram(self):
        connection = BybitConnection.objects.create(
            user=self.user,
            api_key='key',
            api_secret='secret',
        )
        event = BybitExternalEvent.objects.create(
            connection=connection,
            stream='convert_history',
            external_id='conv-ton',
            event_hash='hash-ton',
            occurred_at=timezone.now(),
            asset='USDT',
            amount=Decimal('-100'),
            raw_payload={
                'fromCoin': 'USDT',
                'toCoin': 'TON',
                'fromAmount': '100',
                'toAmount': '500',
                'fee': '0.5',
            },
        )
        asset_map = _build_extended_asset_map(self.user)
        parsed = _parse_convert_event(event, asset_map)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['asset'], self.gram)
        self.assertEqual(parsed['quantity'], Decimal('500'))
        self.assertEqual(parsed['fee_quote'], Decimal('0.5'))

    def test_convert_sell_gram_to_usdt(self):
        connection = BybitConnection.objects.create(
            user=self.user,
            api_key='key',
            api_secret='secret',
        )
        event = BybitExternalEvent.objects.create(
            connection=connection,
            stream='convert_history',
            external_id='conv-sell',
            event_hash='hash-sell',
            occurred_at=timezone.now(),
            asset='TON',
            amount=Decimal('-10'),
            raw_payload={
                'fromCoin': 'TON',
                'toCoin': 'USDT',
                'fromAmount': '10',
                'toAmount': '25',
                'fee': '0.1',
            },
        )
        asset_map = _build_extended_asset_map(self.user)
        parsed = _parse_convert_sell_event(event, asset_map)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['asset'], self.gram)
        self.assertEqual(parsed['quantity'], Decimal('10'))
        self.assertEqual(parsed['total_quote'], Decimal('25'))

    def test_trade_sell_with_fee(self):
        connection = BybitConnection.objects.create(
            user=self.user,
            api_key='key',
            api_secret='secret',
        )
        moment = timezone.now()
        usdt_event = BybitExternalEvent.objects.create(
            connection=connection,
            stream='uta_translog',
            external_id='usdt-sell',
            event_hash='hash-usdt-sell',
            occurred_at=moment,
            asset='USDT',
            amount=Decimal('100'),
            description='TRADE',
            raw_payload={'symbol': 'BTCUSDT', 'type': 'TRADE', 'fee': '0.05'},
        )
        btc_event = BybitExternalEvent.objects.create(
            connection=connection,
            stream='uta_translog',
            external_id='btc-sell',
            event_hash='hash-btc-sell',
            occurred_at=moment,
            asset='BTC',
            amount=Decimal('-0.001'),
            description='TRADE',
            raw_payload={'symbol': 'BTCUSDT', 'type': 'TRADE', 'tradePrice': '100000', 'fee': '0.0000001'},
        )
        asset_map = _build_extended_asset_map(self.user)
        parsed = _parse_trade_events([usdt_event, btc_event], asset_map, set())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['kind'], 'sell')
        self.assertEqual(parsed[0]['quantity'], Decimal('0.001'))
        self.assertEqual(parsed[0]['total_quote'], Decimal('100'))
        self.assertGreater(parsed[0]['fee_quote'], Decimal('0'))

    def test_portfolio_avg_buy_after_sell(self):
        moment = timezone.now()
        CryptoTransaction.objects.create(
            user=self.user,
            asset=self.btc,
            tx_type=CryptoTransaction.TxType.BUY,
            occurred_at=moment,
            quantity=Decimal('1'),
            price_quote=Decimal('50000'),
            total_quote=Decimal('50000'),
            fee_quote=Decimal('10'),
            source=CryptoTransaction.Source.MANUAL,
        )
        CryptoTransaction.objects.create(
            user=self.user,
            asset=self.btc,
            tx_type=CryptoTransaction.TxType.SELL,
            occurred_at=moment,
            quantity=Decimal('0.5'),
            price_quote=Decimal('60000'),
            total_quote=Decimal('30000'),
            fee_quote=Decimal('5'),
            source=CryptoTransaction.Source.BYBIT_TRADE,
        )
        self.btc.wallet_quantity = Decimal('0.5')
        self.btc.save(update_fields=['wallet_quantity'])
        summary = get_portfolio_summary(self.user, date(2026, 7, 4))
        row = summary['assets'][0]
        self.assertEqual(row.quantity, Decimal('0.5'))
        self.assertEqual(row.avg_buy_price, Decimal('50010.00000000'))
        self.assertEqual(row.cost_basis, Decimal('25005.00000000'))
