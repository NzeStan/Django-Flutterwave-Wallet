"""Importable helpers referenced by dotted-path settings in tests."""
from decimal import Decimal

from flutterwave_wallet.notifications.backends import BaseNotificationBackend
from flutterwave_wallet.services.fee_service import FeeCalculator


def upper_normalizer(value):
    return str(value).upper()


class FlatTenCalculator(FeeCalculator):
    """Custom pricing: always 10."""

    def is_enabled(self, operation):
        return True

    def get_fee(self, amount, currency, operation, rule, context):
        return Decimal('10.00'), 'custom', {'rule': 'flat ten'}


class RecordingBackend(BaseNotificationBackend):
    sent = []

    def send(self, event, user, context):
        RecordingBackend.sent.append((event, user.pk, context))


class ExplodingBackend(BaseNotificationBackend):
    def send(self, event, user, context):
        raise RuntimeError('boom')
