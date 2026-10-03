from django.core.management.base import BaseCommand

from flutterwave_wallet.services.fee_sweep_service import FeeService


class Command(BaseCommand):
    help = "Move accrued platform fees into FLW_WALLET_FEE_COLLECTION_WALLET_ID."

    def handle(self, *args, **options):
        result = FeeService().sweep()
        if not result:
            self.stdout.write("Nothing to sweep (or no fee collection wallet configured)")
        for currency, amount in result.items():
            self.stdout.write(self.style.SUCCESS(f"Swept {amount} {currency}"))
