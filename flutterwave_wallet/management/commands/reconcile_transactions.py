from django.core.management.base import BaseCommand

from flutterwave_wallet.services.reconciliation_service import ReconciliationService


class Command(BaseCommand):
    help = ("Settle stale pending deposits, withdrawals, bill payments and refunds with Flutterwave "
            "(run every few minutes from cron if you don't use Celery beat).")

    def add_arguments(self, parser):
        parser.add_argument('--older-than', type=int, default=None,
                            help='Minutes (default FLW_WALLET_RECONCILE_AFTER_MINUTES)')
        parser.add_argument('--limit', type=int, default=None, help='Per type (default FLW_WALLET_RECONCILE_BATCH_SIZE)')

    def handle(self, *args, **options):
        result = ReconciliationService().reconcile(options['older_than'], options['limit'])
        for kind, count in result.items():
            self.stdout.write(f"{kind}: checked {count}")
