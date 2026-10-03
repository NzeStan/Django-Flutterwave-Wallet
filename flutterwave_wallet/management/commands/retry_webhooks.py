from django.core.management.base import BaseCommand

from flutterwave_wallet.services.webhook_service import WebhookService


class Command(BaseCommand):
    help = "Reprocess stored Flutterwave events that failed, and retry failed forwards to your endpoints."

    def handle(self, *args, **options):
        service = WebhookService()
        self.stdout.write(f"events reprocessed: {service.retry_failed_events()}")
        self.stdout.write(f"deliveries retried: {service.retry_failed_deliveries()}")
