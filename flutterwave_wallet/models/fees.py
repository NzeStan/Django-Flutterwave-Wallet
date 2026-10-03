from django.db import models
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.models.base import BaseModel
from flutterwave_wallet.models.fields import WalletMoneyField


class PlatformFee(BaseModel):
    """
    A platform fee earned on a transaction.

    Fees are accrued here and swept into ``FLW_WALLET_FEE_COLLECTION_WALLET_ID`` in
    batches (``sweep_platform_fees``) instead of crediting a single fee wallet on every
    transaction - which would make that one wallet row a global lock bottleneck.

    When a transaction is reversed its fee is voided; if the fee had already been swept,
    a negative "clawback" entry is accrued so the next sweep takes it back.
    """

    transaction = models.ForeignKey('flutterwave_wallet.Transaction', on_delete=models.PROTECT,
                                    related_name='platform_fees', verbose_name=_('Transaction'))
    amount = WalletMoneyField(verbose_name=_('Amount'))
    operation = models.CharField(max_length=20, db_index=True, verbose_name=_('Operation'))
    bearer = models.CharField(max_length=20, blank=True, default='', verbose_name=_('Bearer'))
    swept = models.BooleanField(default=False, db_index=True, verbose_name=_('Swept'))
    voided = models.BooleanField(default=False, db_index=True, verbose_name=_('Voided'),
                                 help_text=_('The transaction was reversed and the fee returned to the payer'))
    sweep_transaction = models.ForeignKey('flutterwave_wallet.Transaction', on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='swept_fees', verbose_name=_('Sweep entry'))
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _('Platform fee')
        verbose_name_plural = _('Platform fees')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['transaction', 'operation'], name='flw_fee_once_per_transaction'),
        ]

    def __str__(self):
        return f"{self.amount} ({self.operation})"
