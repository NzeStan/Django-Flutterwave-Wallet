from django.core.management.base import BaseCommand

from flutterwave_wallet.services.webhook_service import WebhookService


class Command(BaseCommand):
    help = ("Delete old processed webhook events, delivery attempts and expired idempotency records. "
            "Ledger data is never deleted.")

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None, help='Default FLW_WALLET_WEBHOOK_RETENTION_DAYS')

    def handle(self, *args, **options):
        for name, count in WebhookService.prune(options['days']).items():
            self.stdout.write(f"{name}: {count} deleted")
