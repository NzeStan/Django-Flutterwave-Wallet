from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.models.base import BaseModel
from flutterwave_wallet.models.fields import EncryptedTextField


class CardQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)


class Card(BaseModel):
    """
    A tokenized card saved after a successful card deposit, for one-tap top-ups.

    Only the Flutterwave token is stored - never the card number or CVV - and the
    token itself is encrypted at rest. A token can only be charged together with the
    email it was issued for (``email``).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='flutterwave_cards', verbose_name=_('User'))
    token = EncryptedTextField(verbose_name=_('Token (encrypted)'))
    fingerprint = models.CharField(max_length=64, db_index=True, verbose_name=_('Fingerprint'),
                                   help_text=_('Keyed hash identifying the card without storing it'))
    first_6digits = models.CharField(max_length=6, blank=True, default='', verbose_name=_('BIN'))
    last_4digits = models.CharField(max_length=4, blank=True, default='', verbose_name=_('Last 4'))
    issuer = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Issuer'))
    card_type = models.CharField(max_length=50, blank=True, default='', verbose_name=_('Card type'))
    country = models.CharField(max_length=100, blank=True, default='', verbose_name=_('Country'))
    expiry = models.CharField(max_length=7, blank=True, default='', verbose_name=_('Expiry (MM/YY)'))
    email = models.EmailField(verbose_name=_('Token email'))
    currency = models.CharField(max_length=3, blank=True, default='', verbose_name=_('Currency'))
    is_default = models.BooleanField(default=False, verbose_name=_('Default'))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('Active'))
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Last used at'))

    objects = CardQuerySet.as_manager()

    class Meta:
        verbose_name = _('Card')
        verbose_name_plural = _('Cards')
        ordering = ['-is_default', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'fingerprint'], name='flw_card_unique_per_user'),
        ]

    def __str__(self):
        return f"{self.card_type or 'Card'} **** {self.last_4digits}"

    @property
    def masked(self):
        return f"{self.first_6digits}******{self.last_4digits}"

    @property
    def is_expired(self):
        try:
            month, year = self.expiry.split('/')
            month, year = int(month), int(year)
        except (ValueError, AttributeError):
            return False
        year += 2000 if year < 100 else 0
        now = timezone.now()
        return (year, month) < (now.year, now.month)
