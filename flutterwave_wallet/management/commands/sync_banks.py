from django.core.management.base import BaseCommand

from flutterwave_wallet.services.bank_account_service import BankAccountService


class Command(BaseCommand):
    help = "Fetch Flutterwave's bank lists (default countries: FLW_WALLET_BANK_COUNTRIES)."

    def add_arguments(self, parser):
        parser.add_argument('countries', nargs='*', help='ISO country codes, e.g. NG GH KE')

    def handle(self, *args, **options):
        created, updated = BankAccountService().sync_banks(options['countries'] or None)
        self.stdout.write(self.style.SUCCESS(f"Banks: {created} created, {updated} updated"))
