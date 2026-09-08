from datetime import date, datetime

from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from core.views import resolve_dashboard_period


class DashboardPeriodTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.now = timezone.make_aware(datetime(2026, 9, 6, 12, 0))

    def test_default_period_is_current_year(self):
        request = self.factory.get('/dashboard/')
        preset, start, end = resolve_dashboard_period(request, self.now)
        self.assertEqual(preset, 'year')
        self.assertEqual(timezone.localtime(start).date(), date(2026, 1, 1))
        self.assertEqual(timezone.localtime(end).date(), date(2026, 9, 6))

    def test_month_preset(self):
        request = self.factory.get('/dashboard/', {'period': 'month'})
        preset, start, end = resolve_dashboard_period(request, self.now)
        self.assertEqual(preset, 'month')
        self.assertEqual(timezone.localtime(start).date(), date(2026, 9, 1))
        self.assertEqual(timezone.localtime(end).date(), date(2026, 9, 6))

    def test_twelve_months_preset(self):
        request = self.factory.get('/dashboard/', {'period': '12m'})
        preset, start, _end = resolve_dashboard_period(request, self.now)
        self.assertEqual(preset, '12m')
        self.assertEqual(timezone.localtime(start).date(), date(2025, 10, 1))

    def test_explicit_dates_without_period_are_custom(self):
        request = self.factory.get('/dashboard/', {'start': '2025-01-01', 'end': '2025-03-31'})
        preset, start, end = resolve_dashboard_period(request, self.now)
        self.assertEqual(preset, 'custom')
        self.assertEqual(timezone.localtime(start).date(), date(2025, 1, 1))
        self.assertEqual(timezone.localtime(end).date(), date(2025, 3, 31))

    def test_year_preset_ignores_query_dates(self):
        request = self.factory.get(
            '/dashboard/',
            {'period': 'year', 'start': '2024-02-01', 'end': '2024-02-28'},
        )
        preset, start, end = resolve_dashboard_period(request, self.now)
        self.assertEqual(preset, 'year')
        self.assertEqual(timezone.localtime(start).date(), date(2026, 1, 1))
        self.assertEqual(timezone.localtime(end).date(), date(2026, 9, 6))
