"""
Flutterwave webhook handling.

1. (optional) check the source IP
2. verify the request: ``flutterwave-signature`` (HMAC-SHA256 of the raw body) and/or
   ``verif-hash`` (your secret hash), compared in constant time
3. store the event; Flutterwave retries of the same body are detected by its hash
4. dispatch to the built-in handler - which **re-fetches the object from the Flutterwave
   API** before moving any money (``FLW_WALLET_VERIFY_WEBHOOKS_WITH_API``), so a leaked
   secret hash still cannot be used to forge a credit
5. send ``flutterwave_webhook_received`` - hook your own logic here
6. (optional) forward a signed copy to registered HTTP endpoints

Processing errors are stored on the event and never turned into a non-2xx response;
failed events are retried by ``retry_failed_webhook_events`` / Celery.
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta

import requests
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone

from flutterwave_wallet import __version__
from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    WEBHOOK_EVENT_BILL_PAYMENT,
    WEBHOOK_EVENT_BVN_COMPLETED,
    WEBHOOK_EVENT_CARD_TRANSACTION,
    WEBHOOK_EVENT_CHARGE_COMPLETED,
    WEBHOOK_EVENT_REFUND_COMPLETED,
    WEBHOOK_EVENT_SUBSCRIPTION_CANCELLED,
    WEBHOOK_EVENT_TRANSFER_COMPLETED,
    WEBHOOK_EVENT_TRANSFER_DISBURSE,
    WEBHOOK_EVENT_TRANSFER_REVERSAL,
)
from flutterwave_wallet.exceptions import InvalidWebhookSignature, WalletError
from flutterwave_wallet.flutterwave.client import verify_webhook
from flutterwave_wallet.models import Transaction, WebhookDeliveryAttempt, WebhookEndpoint, WebhookEvent
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.signals import (
    flutterwave_webhook_received,
    send_on_commit,
    subscription_cancelled,
    virtual_card_event,
)

logger = logging.getLogger('flutterwave_wallet')

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_PROCESSING_ATTEMPTS = 10


def event_type_of(payload):
    """Flutterwave's event name (virtual-card events use ``event.type`` instead of ``event``)."""
    for key in ('event', 'event.type', 'type'):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:100]
    return 'unknown'


class WebhookService(BaseService):

    def __init__(self, flutterwave=None):
        super().__init__(flutterwave)
        self._handlers = {
            WEBHOOK_EVENT_CHARGE_COMPLETED: self._handle_charge,
            WEBHOOK_EVENT_TRANSFER_COMPLETED: self._handle_transfer,
            WEBHOOK_EVENT_TRANSFER_DISBURSE: self._handle_transfer,
            WEBHOOK_EVENT_TRANSFER_REVERSAL: self._handle_transfer,
            WEBHOOK_EVENT_BILL_PAYMENT: self._handle_bill,
            WEBHOOK_EVENT_REFUND_COMPLETED: self._handle_refund,
            WEBHOOK_EVENT_BVN_COMPLETED: self._handle_bvn,
            'BVN.completed': self._handle_bvn,
            WEBHOOK_EVENT_SUBSCRIPTION_CANCELLED: self._handle_subscription,
            WEBHOOK_EVENT_CARD_TRANSACTION: self._handle_virtual_card,
        }

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    @staticmethod
    def is_allowed_ip(ip_address):
        if not wallet_settings.WEBHOOK_VERIFY_IP:
            return True
        return ip_address in set(wallet_settings.WEBHOOK_ALLOWED_IPS or [])

    @staticmethod
    def verify_request(payload_bytes, headers):
        if not wallet_settings.FLUTTERWAVE_SECRET_HASH:
            raise InvalidWebhookSignature("FLUTTERWAVE_SECRET_HASH is not configured; refusing webhooks")
        if not payload_bytes:
            raise InvalidWebhookSignature("Empty webhook payload")
        if not verify_webhook(payload_bytes, headers):
            raise InvalidWebhookSignature()
        return True

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle_webhook(self, payload_bytes, headers, ip_address=None):
        """Verify, store and process a webhook. Returns ``(event, created)`` (False for a duplicate)."""
        if not self.is_allowed_ip(ip_address):
            raise InvalidWebhookSignature(f"Webhook from unexpected IP {ip_address}")
        if payload_bytes is not None and len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise ValueError("Webhook payload too large")
        self.verify_request(payload_bytes, headers)

        try:
            payload = json.loads(payload_bytes.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid webhook payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Webhook payload must be a JSON object")

        event_type = event_type_of(payload)
        data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
        reference = data.get('tx_ref') or data.get('reference') or data.get('flw_ref')
        flw_id = str(data['id'])[:64] if isinstance(data.get('id'), (int, str)) and data.get('id') != '' else None
        key = hashlib.sha256(payload_bytes).hexdigest()

        try:
            with db_transaction.atomic():
                event, created = WebhookEvent.objects.get_or_create(
                    idempotency_key=key,
                    defaults={'event_type': event_type, 'payload': payload,
                              'reference': str(reference)[:150] if reference else None, 'flw_id': flw_id,
                              'source_ip': ip_address or None},
                )
        except IntegrityError:
            event, created = WebhookEvent.objects.get(idempotency_key=key), False

        if not created and event.processed:
            logger.info("Duplicate webhook %s ignored", event.pk)
            return event, False

        if wallet_settings.USE_CELERY:
            from flutterwave_wallet.tasks import process_webhook_event_task
            event_pk = str(event.pk)
            db_transaction.on_commit(lambda: process_webhook_event_task.delay(event_pk))
        else:
            self.process_event(event)
        return event, created

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def process_event(self, event):
        """Run the built-in handler. Never raises; errors are stored on the event for retry."""
        if not self.claim(f'webhook:{event.pk}', 120):
            logger.info("Webhook %s is being processed elsewhere", event.pk)
            return False
        try:
            return self._process(event)
        finally:
            self.release(f'webhook:{event.pk}')

    def _process(self, event):
        event.refresh_from_db()
        if event.processed:
            return True
        payload = event.payload or {}
        data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
        handler = self._handlers.get(event.event_type)
        handled = False
        error = ''
        event.processing_attempts += 1
        try:
            if handler is not None:
                handled = handler(event.event_type, data, event) is not None
        except Exception as exc:  # stored for retry; must never bubble up to Flutterwave
            logger.exception("Error processing webhook %s (%s)", event.pk, event.event_type)
            error = f"{type(exc).__name__}: {exc}"[:2000]
            if event.transaction_id and not Transaction.objects.filter(pk=event.transaction_id).exists():
                event.transaction = None

        event.processing_error = error
        event.processed = not error
        event.processed_at = timezone.now() if not error else None
        event.save(update_fields=['processed', 'processed_at', 'processing_error', 'processing_attempts',
                                  'transaction', 'updated_at'])
        if not error:
            send_on_commit(flutterwave_webhook_received, sender=WebhookEvent, event=event.event_type, data=data,
                           webhook_event=event, handled=handled)
            if wallet_settings.ENABLE_WEBHOOK_FORWARDING:
                self._forward(event)
        return not error

    def reprocess_webhook_event(self, event_id):
        return self.process_event(WebhookEvent.objects.get(pk=event_id))

    def retry_failed_events(self, limit=200):
        """Re-run stored events whose processing failed (e.g. Flutterwave was briefly unreachable)."""
        retried = 0
        for event in WebhookEvent.objects.filter(processed=False, processing_attempts__lt=MAX_PROCESSING_ATTEMPTS,
                                                 created_at__lte=timezone.now() - timedelta(minutes=1)
                                                 ).order_by('created_at')[:limit]:
            self.process_event(event)
            retried += 1
        return retried

    # ------------------------------------------------------------------
    # Built-in handlers
    # ------------------------------------------------------------------

    def _handle_charge(self, event_type, data, event):
        from flutterwave_wallet.services.deposit_service import DepositService

        deposits = DepositService(**self.clients())
        if not wallet_settings.VERIFY_WEBHOOKS_WITH_API:
            return deposits.settle_charge(data, webhook_event=event)
        if not data.get('id'):
            return None
        return deposits.verify_and_settle(data['id'], expected_reference=data.get('tx_ref'), webhook_event=event)

    def _handle_transfer(self, event_type, data, event):
        from flutterwave_wallet.services.withdrawal_service import WithdrawalService

        return WithdrawalService(**self.clients()).handle_transfer_webhook(data, webhook_event=event,
                                                                          event_type=event_type)

    def _handle_bill(self, event_type, data, event):
        from flutterwave_wallet.services.bill_service import BillPaymentService

        return BillPaymentService(**self.clients()).handle_bill_webhook(data, webhook_event=event)

    def _handle_refund(self, event_type, data, event):
        from flutterwave_wallet.services.refund_service import RefundService

        return RefundService(**self.clients()).handle_refund_webhook(data, webhook_event=event)

    def _handle_bvn(self, event_type, data, event):
        from flutterwave_wallet.services.kyc_service import KYCService

        return KYCService(**self.clients()).handle_bvn_webhook(data, webhook_event=event)

    @staticmethod
    def _handle_subscription(event_type, data, event):
        send_on_commit(subscription_cancelled, sender=WebhookEvent, data=data, webhook_event=event)
        return data

    @staticmethod
    def _handle_virtual_card(event_type, data, event):
        send_on_commit(virtual_card_event, sender=WebhookEvent, data=data, webhook_event=event)
        return data

    # ------------------------------------------------------------------
    # Forwarding to your own endpoints
    # ------------------------------------------------------------------

    def _forward(self, event):
        for endpoint in WebhookEndpoint.objects.filter(is_active=True):
            if not endpoint.accepts(event.event_type):
                continue
            if wallet_settings.USE_CELERY:
                from flutterwave_wallet.tasks import deliver_webhook_task
                event_pk, endpoint_pk = str(event.pk), str(endpoint.pk)
                db_transaction.on_commit(lambda e=event_pk, p=endpoint_pk: deliver_webhook_task.delay(e, p))
            else:
                try:
                    self.deliver(event, endpoint)
                except Exception:
                    logger.exception("Forwarding webhook %s to %s failed", event.pk, endpoint.url)

    @staticmethod
    def sign_payload(body, secret, timestamp):
        message = f"{timestamp}.".encode('utf-8') + body
        return 'sha256=' + hmac.new((secret or '').encode('utf-8'), message, hashlib.sha256).hexdigest()

    def deliver(self, event, endpoint):
        attempt_number = WebhookDeliveryAttempt.objects.filter(webhook_event=event,
                                                               webhook_endpoint=endpoint).count() + 1
        body = json.dumps({'id': str(event.pk), 'event': event.event_type, 'data': event.payload},
                          separators=(',', ':'), sort_keys=True, default=str).encode('utf-8')
        timestamp = str(int(time.time()))
        headers = {str(k): str(v) for k, v in (endpoint.headers or {}).items()}
        headers.update({
            'Content-Type': 'application/json',
            'User-Agent': f'django-flutterwave-wallet/{__version__}',
            'X-Wallet-Event': event.event_type,
            'X-Wallet-Event-Id': str(event.pk),
            'X-Wallet-Delivery-Attempt': str(attempt_number),
            'X-Wallet-Timestamp': timestamp,
            'X-Wallet-Signature': self.sign_payload(body, endpoint.secret, timestamp),
        })
        attempt = WebhookDeliveryAttempt(webhook_event=event, webhook_endpoint=endpoint, attempt_number=attempt_number)
        try:
            response = requests.post(endpoint.url, data=body, headers=headers, timeout=endpoint.timeout or 10,
                                     allow_redirects=False)
            attempt.response_code = response.status_code
            attempt.response_body = (response.text or '')[:2000]
            attempt.is_success = 200 <= response.status_code < 300
        except requests.RequestException as exc:
            attempt.response_body = f"{type(exc).__name__}"[:2000]
        attempt.save()
        return attempt

    def retry_failed_deliveries(self, limit=None):
        retried = 0
        seen = set()
        window_start = timezone.now() - timedelta(hours=int(wallet_settings.WEBHOOK_RETRY_WINDOW_HOURS))
        failed = WebhookDeliveryAttempt.objects.filter(is_success=False, created_at__gte=window_start).select_related(
            'webhook_event', 'webhook_endpoint').order_by('-created_at')
        for attempt in failed:
            pair = (attempt.webhook_event_id, attempt.webhook_endpoint_id)
            if pair in seen:
                continue
            seen.add(pair)
            deliveries = WebhookDeliveryAttempt.objects.filter(webhook_event_id=pair[0], webhook_endpoint_id=pair[1])
            endpoint = attempt.webhook_endpoint
            if (deliveries.filter(is_success=True).exists() or deliveries.count() >= endpoint.max_attempts
                    or not endpoint.is_active):
                continue
            self.deliver(attempt.webhook_event, endpoint)
            retried += 1
            if limit and retried >= limit:
                break
        return retried

    def retry_delivery(self, delivery_attempt):
        if delivery_attempt.is_success:
            raise WalletError("Cannot retry a successful delivery")
        return self.deliver(delivery_attempt.webhook_event, delivery_attempt.webhook_endpoint)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    @staticmethod
    def prune(retention_days=None):
        """
        Delete old operational data: processed webhook events and delivery attempts older than
        ``FLW_WALLET_WEBHOOK_RETENTION_DAYS`` and expired idempotency records. Ledger data is never pruned.
        """
        from flutterwave_wallet.models import IdempotencyRecord

        result = {'webhook_events': 0, 'delivery_attempts': 0, 'idempotency_records': 0}
        days = retention_days if retention_days is not None else wallet_settings.WEBHOOK_RETENTION_DAYS
        if days is not None:
            cutoff = timezone.now() - timedelta(days=int(days))
            result['delivery_attempts'] = WebhookDeliveryAttempt.objects.filter(created_at__lt=cutoff).delete()[0]
            result['webhook_events'] = WebhookEvent.objects.filter(
                processed=True, created_at__lt=cutoff).delete()[1].get('flutterwave_wallet.WebhookEvent', 0)
        expired = timezone.now() - timedelta(hours=int(wallet_settings.IDEMPOTENCY_TTL_HOURS))
        result['idempotency_records'] = IdempotencyRecord.objects.filter(created_at__lt=expired).delete()[0]
        return result
