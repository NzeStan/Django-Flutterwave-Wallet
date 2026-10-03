from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.constants import VIRTUAL_ACCOUNT_KINDS, VIRTUAL_ACCOUNT_STATIC
from flutterwave_wallet.models.base import BaseModel


class VirtualAccount(BaseModel):
    """
    A Flutterwave virtual account number that funds a wallet by bank transfer.

    * static - permanent, one per wallet (needs the customer's BVN or NIN)
    * dynamic - short-lived, created for a single deposit of a fixed amount
    """

    wallet = models.ForeignKey('flutterwave_wallet.Wallet', on_delete=models.CASCADE,
                               related_name='virtual_accounts', verbose_name=_('Wallet'))
    kind = models.CharField(max_length=10, choices=VIRTUAL_ACCOUNT_KINDS, default=VIRTUAL_ACCOUNT_STATIC)
    account_number = models.CharField(max_length=30, db_index=True, verbose_name=_('Account number'))
    bank_name = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Bank name'))
    account_name = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Account name'))
    currency = models.CharField(max_length=3, default='NGN')
    tx_ref = models.CharField(max_length=100, unique=True, verbose_name=_('tx_ref'),
                              help_text=_('Flutterwave reports deposits into this account with this tx_ref'))
    order_ref = models.CharField(max_length=100, blank=True, default='', db_index=True,
                                 help_text=_('v3 order_ref, or the v4 virtual account id'))
    api_version = models.CharField(max_length=4, default='v3')
    flw_ref = models.CharField(max_length=150, blank=True, default='', db_index=True)
    amount = models.DecimalField(max_digits=19, decimal_places=2, null=True, blank=True,
                                 help_text=_('Expected amount (dynamic accounts)'))
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    transaction = models.OneToOneField('flutterwave_wallet.Transaction', on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='virtual_account',
                                       help_text=_('The deposit a dynamic account was created for'))
    flw_response = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _('Virtual account')
        verbose_name_plural = _('Virtual accounts')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.account_number} {self.bank_name}"

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())
