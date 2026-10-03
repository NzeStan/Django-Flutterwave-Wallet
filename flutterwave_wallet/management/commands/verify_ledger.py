from django.core.management.base import BaseCommand, CommandError

from flutterwave_wallet.models import Wallet
from flutterwave_wallet.services.reconciliation_service import ReconciliationService


class Command(BaseCommand):
    help = ("Recompute every wallet's ledger: positions, running balances and the tamper-evident hash chain. "
            "Exits non-zero if anything is wrong, so it can drive monitoring.")

    def add_arguments(self, parser):
        parser.add_argument('--wallet', action='append', help='Only these wallet ids (repeatable)')

    def handle(self, *args, **options):
        wallets = Wallet.objects.all()
        if options['wallet']:
            wallets = wallets.filter(pk__in=options['wallet'])
        report = ReconciliationService.verify_ledgers(wallets)
        if report:
            for wallet_id, problems in report.items():
                for problem in problems:
                    self.stderr.write(f"{wallet_id}: {problem}")
            raise CommandError(f"Ledger problems in {len(report)} wallet(s)")
        self.stdout.write(self.style.SUCCESS(f"{wallets.count()} ledger(s) verified"))
