"""
Notification backends.

The wallet never notifies anyone by itself. Enable one or more backends::

    FLW_WALLET_NOTIFICATION_BACKENDS = [
        'flutterwave_wallet.notifications.backends.EmailNotificationBackend',
        'myproject.notifications.SMSBackend',
    ]

Write your own (SMS, push, Slack...) by subclassing :class:`BaseNotificationBackend`::

    class SMSBackend(BaseNotificationBackend):
        events = {'deposit_completed', 'transfer_received'}

        def send(self, event, user, context):
            sms.send(user.profile.phone, self.render_text(event, context))

Events: deposit_completed, deposit_failed, withdrawal_completed, withdrawal_failed,
transfer_sent, transfer_received, payment_made, payment_received, payment_cancelled,
bill_payment_completed, bill_payment_failed, refund_completed, refund_failed,
virtual_account_created, wallet_locked.

Backends run inside the request/worker that moved the money (after commit). For slow
channels, have ``send`` enqueue a job rather than call the provider inline.
"""
import logging

from django.core.mail import send_mail
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

from flutterwave_wallet.conf import wallet_settings

logger = logging.getLogger('flutterwave_wallet.notifications')

SUBJECTS = {
    'deposit_completed': 'Your wallet has been funded',
    'deposit_failed': 'Your wallet funding failed',
    'withdrawal_completed': 'Your withdrawal was successful',
    'withdrawal_failed': 'Your withdrawal failed and was refunded',
    'transfer_sent': 'You sent money',
    'transfer_received': 'You received money',
    'payment_made': 'Payment successful',
    'payment_received': 'You received a payment',
    'payment_cancelled': 'Payment cancelled and refunded',
    'bill_payment_completed': 'Bill payment successful',
    'bill_payment_failed': 'Bill payment failed and was refunded',
    'refund_completed': 'Refund processed',
    'refund_failed': 'Refund failed',
    'virtual_account_created': 'Your account number is ready',
    'wallet_locked': 'Your wallet has been locked',
}


class BaseNotificationBackend:
    #: Events this backend handles (None = all)
    events = None

    def handles(self, event):
        return self.events is None or event in self.events

    def send(self, event, user, context):
        raise NotImplementedError

    def render_text(self, event, context):
        for template in (f'flutterwave_wallet/notifications/{event}.txt', 'flutterwave_wallet/notifications/default.txt'):
            try:
                return render_to_string(template, {'event': event, **context}).strip()
            except TemplateDoesNotExist:
                continue
        return f"{event}: {context.get('amount', '')}"

    def subject(self, event, context):
        return SUBJECTS.get(event, event.replace('_', ' ').capitalize())


class LoggingNotificationBackend(BaseNotificationBackend):
    """Writes notifications to the ``flutterwave_wallet.notifications`` logger (development)."""

    def send(self, event, user, context):
        logger.info("[notification] %s -> %s: %s", event, getattr(user, 'pk', None), self.render_text(event, context))


class EmailNotificationBackend(BaseNotificationBackend):
    """Plain-text email via Django's email framework. Override the templates to change the wording."""

    def send(self, event, user, context):
        email = getattr(user, 'email', None)
        if not email:
            return
        from django.conf import settings

        sender = wallet_settings.EMAIL_SENDER or getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        send_mail(self.subject(event, context), self.render_text(event, context), sender, [email],
                  fail_silently=False)
