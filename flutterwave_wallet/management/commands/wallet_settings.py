import json

from django.core.management.base import BaseCommand

from flutterwave_wallet.conf import setting_name, wallet_settings


class Command(BaseCommand):
    help = "Show the Flutterwave wallet settings in effect (secrets masked)."

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **options):
        resolved = wallet_settings.as_dict()
        if options['json']:
            self.stdout.write(json.dumps(resolved, indent=2, default=str))
            return
        for name, value in resolved.items():
            self.stdout.write(f"{setting_name(name)} = {value!r}")
