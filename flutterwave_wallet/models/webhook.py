from django.db import models
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.models.base import BaseModel


class WebhookEvent(BaseModel):
    """A webhook received from Flutterwave. Stored before processing so it can be replayed."""

    event_type = models.CharField(max_length=100, db_index=True, verbose_name=_('Event type'))
    payload = models.JSONField(verbose_name=_('Payload'))
    reference = models.CharField(max_length=150, blank=True, null=True, db_index=True, verbose_name=_('Reference'))
    flw_id = models.CharField(max_length=64, blank=True, null=True, db_index=True, verbose_name=_('Flutterwave id'))
    idempotency_key = models.CharField(
        max_length=64, unique=True, verbose_name=_('Idempotency key'),
        help_text=_('SHA-256 of the raw body; Flutterwave retries of the same event share it'),
    )
    processed = models.BooleanField(default=False, db_index=True, verbose_name=_('Processed'))
    processed_at = models.DateTimeField(blank=True, null=True, verbose_name=_('Processed at'))
    processing_attempts = models.PositiveSmallIntegerField(default=0)
    processing_error = models.TextField(blank=True, default='', verbose_name=_('Processing error'))
    source_ip = models.GenericIPAddressField(blank=True, null=True, verbose_name=_('Source IP'))
    transaction = models.ForeignKey('flutterwave_wallet.Transaction', on_delete=models.SET_NULL,
                                    related_name='webhook_events', blank=True, null=True,
                                    verbose_name=_('Transaction'))

    class Meta:
        verbose_name = _('Webhook event')
        verbose_name_plural = _('Webhook events')
        ordering = ['-created_at']
        indexes = [models.Index(fields=['processed', 'created_at'], name='flw_webhook_pending_idx')]

    def __str__(self):
        return f"{self.event_type} - {self.reference or '-'}"

    @property
    def data(self):
        data = (self.payload or {}).get('data')
        return data if isinstance(data, dict) else {}


class WebhookEndpoint(BaseModel):
    """
    An HTTP endpoint that receives a signed copy of processed Flutterwave events
    (``FLW_WALLET_ENABLE_WEBHOOK_FORWARDING``). In-process code should use signals instead.

    ``X-Wallet-Signature`` is ``sha256=<hex HMAC-SHA256 of "<timestamp>.<body>">`` with
    ``secret``, and ``X-Wallet-Timestamp`` lets receivers reject replays.
    """

    name = models.CharField(max_length=255, verbose_name=_('Name'))
    url = models.URLField(max_length=500, verbose_name=_('URL'))
    secret = models.CharField(max_length=255, verbose_name=_('Signing secret'))
    event_types = models.JSONField(default=list, blank=True, verbose_name=_('Event types'),
                                   help_text=_('Empty = all events'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is active'))
    headers = models.JSONField(default=dict, blank=True, verbose_name=_('Extra headers'))
    max_attempts = models.PositiveSmallIntegerField(default=5, verbose_name=_('Max attempts'))
    timeout = models.PositiveSmallIntegerField(default=10, verbose_name=_('Timeout (seconds)'))

    class Meta:
        verbose_name = _('Webhook endpoint')
        verbose_name_plural = _('Webhook endpoints')
        ordering = ['name']

    def __str__(self):
        return self.name

    def accepts(self, event_type):
        return not self.event_types or event_type in self.event_types


class WebhookDeliveryAttempt(BaseModel):
    webhook_event = models.ForeignKey(WebhookEvent, on_delete=models.CASCADE, related_name='delivery_attempts')
    webhook_endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name='delivery_attempts')
    response_code = models.PositiveSmallIntegerField(blank=True, null=True)
    response_body = models.TextField(blank=True, default='')
    is_success = models.BooleanField(default=False, db_index=True)
    attempt_number = models.PositiveSmallIntegerField(default=1)

    class Meta:
        verbose_name = _('Webhook delivery attempt')
        verbose_name_plural = _('Webhook delivery attempts')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.webhook_endpoint.name} - {self.webhook_event.event_type} - #{self.attempt_number}"
