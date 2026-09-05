from datetime import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.services.crypto_rates import portfolio_crypto_symbols, sync_crypto_rates_historical


class Command(BaseCommand):
    help = 'Backfill daily crypto rates into CurrencyRate from CoinGecko history'

    def add_arguments(self, parser):
        parser.add_argument(
            '--from',
            dest='start_date',
            default='2020-01-01',
            help='Start date YYYY-MM-DD (default: 2020-01-01)',
        )
        parser.add_argument(
            '--to',
            dest='end_date',
            default=None,
            help='End date YYYY-MM-DD (default: today)',
        )
        parser.add_argument(
            '--symbols',
            default='',
            help='Comma-separated tickers, e.g. BTC,ETH. Default: active non-fiat currencies in directory.',
        )

    def handle(self, *args, **options):
        start_date = datetime.strptime(options['start_date'], '%Y-%m-%d').date()
        end_date = (
            datetime.strptime(options['end_date'], '%Y-%m-%d').date()
            if options['end_date']
            else timezone.localdate()
        )

        symbols_raw = (options['symbols'] or '').strip()
        if symbols_raw:
            symbols = portfolio_crypto_symbols(
                [part.strip().upper() for part in symbols_raw.split(',') if part.strip()]
            )
        else:
            symbols = portfolio_crypto_symbols()

        self.stdout.write(
            f'Backfill crypto rates {start_date.isoformat()} .. {end_date.isoformat()} '
            f'for {", ".join(symbols)}'
        )
        result = sync_crypto_rates_historical(symbols, start_date, end_date)
        self.stdout.write(
            self.style.SUCCESS(
                f"done: inserted={result['inserted']}, updated={result['updated']}, "
                f"days={result['days_written']}, synced={result['synced']}, "
                f"skipped={result['skipped']}, failed={result['failed']}"
            )
        )
