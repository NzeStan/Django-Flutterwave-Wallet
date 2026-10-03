from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.constants import BENEFICIARY_TYPE_BANK, BENEFICIARY_TYPES
from flutterwave_wallet.models.base import BaseModel


class BankQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def for_country(self, country):
        return self.filter(country=str(country).upper())


class Bank(BaseModel):
    """A bank (or mobile money operator) Flutterwave can pay out to. Synced with ``sync_banks``."""

    name = models.CharField(max_length=255, verbose_name=_('Name'))
    code = models.CharField(max_length=30, db_index=True, verbose_name=_('Code'))
    country = models.CharField(max_length=2, db_index=True, verbose_name=_('Country'))
    flw_id = models.CharField(max_length=64, null=True, blank=True, verbose_name=_('Flutterwave bank id'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is active'))

    objects = BankQuerySet.as_manager()

    class Meta:
        verbose_name = _('Bank')
        verbose_name_plural = _('Banks')
        ordering = ['country', 'name']
        constraints = [
            models.UniqueConstraint(fields=['code', 'country'], name='flw_bank_unique_code_country'),
        ]

    def __str__(self):
        return f"{self.name} ({self.country})"


class BankAccount(BaseModel):
    """
    A payout destination owned by a user: a bank account or a mobile money wallet
    (``account_bank`` is then the operator code, e.g. ``MPS`` for M-Pesa).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='flutterwave_bank_accounts', verbose_name=_('User'))
    beneficiary_type = models.CharField(max_length=20, choices=BENEFICIARY_TYPES, default=BENEFICIARY_TYPE_BANK)
    bank = models.ForeignKey(Bank, on_delete=models.SET_NULL, null=True, blank=True, related_name='accounts')
    account_bank = models.CharField(max_length=30, verbose_name=_('Bank / operator code'))
    bank_name = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Bank name'))
    account_number = models.CharField(max_length=50, verbose_name=_('Account number'))
    account_name = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Account name'))
    currency = models.CharField(max_length=3, default='NGN', verbose_name=_('Currency'))
    country = models.CharField(max_length=2, blank=True, default='', verbose_name=_('Country'))
    branch_code = models.CharField(max_length=50, blank=True, default='', verbose_name=_('Branch code'))
    flw_beneficiary_id = models.CharField(max_length=64, null=True, blank=True,
                                          verbose_name=_('Flutterwave beneficiary / recipient id'))
    is_verified = models.BooleanField(default=False, verbose_name=_('Name verified'))
    is_default = models.BooleanField(default=False, verbose_name=_('Default'))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('Active'))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_('Metadata'))

    class Meta:
        verbose_name = _('Payout account')
        verbose_name_plural = _('Payout accounts')
        ordering = ['-is_default', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'account_bank', 'account_number', 'currency'],
                                    name='flw_bank_account_unique'),
        ]

    def __str__(self):
        return f"{self.account_name or self.account_number} - {self.bank_name or self.account_bank}"

    @property
    def masked_account_number(self):
        number = self.account_number or ''
        return f"{'*' * max(len(number) - 4, 0)}{number[-4:]}"
