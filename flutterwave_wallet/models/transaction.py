from django.db import models
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.constants import (
    ACTIVE_STATUSES,
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    FEE_BEARERS,
    FINAL_TRANSACTION_STATUSES,
    PAYMENT_METHODS,
    TRANSACTION_DIRECTIONS,
    TRANSACTION_STATUS_CANCELLED,
    TRANSACTION_STATUS_FAILED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_STATUS_REVERSED,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_STATUSES,
    TRANSACTION_TYPE_DEPOSIT,
    TRANSACTION_TYPE_PAYMENT,
    TRANSACTION_TYPE_TRANSFER,
    TRANSACTION_TYPES,
)
from flutterwave_wallet.models.base import BaseModel
from flutterwave_wallet.models.compat import check_constraint
from flutterwave_wallet.models.fields import WalletMoneyField


class TransactionQuerySet(models.QuerySet):
    def successful(self):
        return self.filter(status=TRANSACTION_STATUS_SUCCESS)

    def pending(self):
        return self.filter(status__in=ACTIVE_STATUSES)

    def failed(self):
        return self.filter(status=TRANSACTION_STATUS_FAILED)

    def credits(self):
        return self.filter(direction=DIRECTION_CREDIT)

    def debits(self):
        return self.filter(direction=DIRECTION_DEBIT)

    def posted(self):
        """Entries that changed a balance (they have a ledger position)."""
        return self.filter(ledger_sequence__isnull=False)

    def needing_review(self):
        return self.filter(needs_review=True)

    def with_full_details(self):
        return self.select_related(
            'wallet', 'wallet__user', 'counterparty_wallet', 'counterparty_wallet__user',
            'bank_account', 'card', 'related_transaction',
        )

    def recent(self, days=30):
        return self.filter(created_at__gte=timezone.now() - timezone.timedelta(days=days))

    def in_date_range(self, start_date=None, end_date=None):
        queryset = self
        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__lte=end_date)
        return queryset

    def statistics(self):
        success = Q(status=TRANSACTION_STATUS_SUCCESS)
        return self.aggregate(
            total_count=Count('id'),
            successful_count=Count('id', filter=success),
            pending_count=Count('id', filter=Q(status__in=ACTIVE_STATUSES)),
            failed_count=Count('id', filter=Q(status=TRANSACTION_STATUS_FAILED)),
            total_credits=Sum('total_amount', filter=success & Q(direction=DIRECTION_CREDIT)),
            total_debits=Sum('total_amount', filter=success & Q(direction=DIRECTION_DEBIT)),
            total_fees=Sum('fees', filter=success),
            average_amount=Avg('amount', filter=success),
        )


class TransactionManager(models.Manager.from_queryset(TransactionQuerySet)):
    pass


class Transaction(BaseModel):
    """
    One balance movement on one wallet (a ledger entry).

    * ``amount`` - the face value of the operation (what was requested)
    * ``fees`` - the platform fee attached to the operation
    * ``total_amount`` - what actually moved on *this* wallet's balance
    * ``direction`` - credit or debit on this wallet
    * ``balance_after`` / ``ledger_sequence`` - this wallet's balance and position right
      after the posting (null until the entry changes the balance)
    * ``previous_hash`` / ``entry_hash`` - tamper-evident hash chain per wallet

    Transfers and payments between wallets produce two linked rows (a debit and a
    credit), so every wallet has a complete statement of its own.
    """

    wallet = models.ForeignKey(
        'flutterwave_wallet.Wallet', on_delete=models.PROTECT, related_name='transactions', verbose_name=_('Wallet'),
    )
    amount = WalletMoneyField(verbose_name=_('Amount'))
    fees = WalletMoneyField(default=0, verbose_name=_('Fees'))
    total_amount = WalletMoneyField(default=0, verbose_name=_('Total amount'),
                                    help_text=_("Amount that moved on this wallet's balance"))
    balance_after = WalletMoneyField(null=True, blank=True, verbose_name=_('Balance after'))
    ledger_sequence = models.PositiveBigIntegerField(null=True, blank=True, editable=False,
                                                     verbose_name=_('Ledger sequence'))
    previous_hash = models.CharField(max_length=64, blank=True, default='', editable=False)
    entry_hash = models.CharField(max_length=64, blank=True, default='', editable=False)
    fee_bearer = models.CharField(max_length=20, choices=FEE_BEARERS, blank=True, null=True,
                                  verbose_name=_('Fee bearer'))

    reference = models.CharField(max_length=100, unique=True, verbose_name=_('Reference'),
                                 help_text=_('Our reference; sent to Flutterwave as tx_ref / transfer reference'))
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, default=TRANSACTION_TYPE_DEPOSIT,
                                        db_index=True, verbose_name=_('Transaction type'))
    direction = models.CharField(max_length=10, choices=TRANSACTION_DIRECTIONS, default=DIRECTION_CREDIT,
                                 db_index=True, verbose_name=_('Direction'))
    status = models.CharField(max_length=20, choices=TRANSACTION_STATUSES, default=TRANSACTION_STATUS_PENDING,
                              db_index=True, verbose_name=_('Status'))
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, blank=True, null=True,
                                      verbose_name=_('Payment method'))
    channel = models.CharField(max_length=50, blank=True, default='', verbose_name=_('Channel'),
                               help_text=_("Flutterwave's raw payment_type"))
    description = models.TextField(blank=True, default='', verbose_name=_('Description'))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_('Metadata'))

    # Flutterwave identifiers (v3 ids are numbers, v4 ids look like chg_..., trf_...)
    api_version = models.CharField(max_length=4, default='v3', db_index=True, verbose_name=_('API version'),
                                   help_text=_('Flutterwave API version that handled this transaction'))
    flw_id = models.CharField(max_length=64, blank=True, null=True, db_index=True,
                              help_text=_('Flutterwave transaction / transfer / refund id'))
    flw_ref = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    flw_response = models.JSONField(blank=True, null=True)

    # Relationships
    counterparty_wallet = models.ForeignKey(
        'flutterwave_wallet.Wallet', on_delete=models.SET_NULL, related_name='counterparty_transactions',
        blank=True, null=True, verbose_name=_('Counterparty wallet'),
    )
    bank_account = models.ForeignKey(
        'flutterwave_wallet.BankAccount', on_delete=models.SET_NULL, related_name='transactions',
        blank=True, null=True, verbose_name=_('Payout account'),
    )
    card = models.ForeignKey(
        'flutterwave_wallet.Card', on_delete=models.SET_NULL, related_name='transactions', blank=True, null=True,
        verbose_name=_('Card'),
    )
    related_transaction = models.ForeignKey(
        'self', on_delete=models.SET_NULL, related_name='related_transactions', blank=True, null=True,
        verbose_name=_('Related transaction'),
        help_text=_('The other leg of a transfer, or the original of a refund/reversal'),
    )

    # Audit & operations
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name=_('IP address'))
    user_agent = models.TextField(blank=True, default='', verbose_name=_('User agent'))
    completed_at = models.DateTimeField(blank=True, null=True, db_index=True, verbose_name=_('Completed at'))
    failed_reason = models.TextField(blank=True, default='', verbose_name=_('Failed reason'))
    needs_review = models.BooleanField(default=False, db_index=True, verbose_name=_('Needs review'),
                                       help_text=_('Outcome unknown or amounts differ: a human should look'))

    objects = TransactionManager()

    class Meta:
        verbose_name = _('Transaction')
        verbose_name_plural = _('Transactions')
        ordering = ['-created_at']
        constraints = [
            # Two entries can never claim the same position in a wallet's ledger
            models.UniqueConstraint(fields=['wallet', 'ledger_sequence'], name='flw_txn_unique_ledger_position',
                                    condition=Q(ledger_sequence__isnull=False)),
            check_constraint(Q(amount__gte=0) & Q(fees__gte=0) & Q(total_amount__gte=0), 'flw_txn_amounts_positive'),
        ]
        indexes = [
            models.Index(fields=['wallet', 'created_at'], name='flw_txn_wallet_created_idx'),
            models.Index(fields=['wallet', 'status'], name='flw_txn_wallet_status_idx'),
            models.Index(fields=['transaction_type', 'status', 'created_at'], name='flw_txn_type_status_idx'),
            models.Index(fields=['status', 'created_at'], name='flw_txn_status_created_idx'),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} {self.amount} ({self.get_status_display()})"

    def __repr__(self):
        return (f"<Transaction id={self.id} type={self.transaction_type} direction={self.direction} "
                f"status={self.status} amount={self.amount} reference={self.reference}>")

    def save(self, *args, **kwargs):
        if not self.reference:
            from flutterwave_wallet.utils.references import generate_reference
            self.reference = generate_reference('TXN')
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_final(self):
        return self.status in FINAL_TRANSACTION_STATUSES

    @property
    def is_successful(self):
        return self.status == TRANSACTION_STATUS_SUCCESS

    @property
    def is_failed(self):
        return self.status == TRANSACTION_STATUS_FAILED

    @property
    def is_pending(self):
        return self.status in ACTIVE_STATUSES

    @property
    def is_cancelled(self):
        return self.status == TRANSACTION_STATUS_CANCELLED

    @property
    def is_reversed(self):
        return self.status == TRANSACTION_STATUS_REVERSED

    @property
    def is_credit(self):
        return self.direction == DIRECTION_CREDIT

    @property
    def is_debit(self):
        return self.direction == DIRECTION_DEBIT

    @property
    def is_posted(self):
        return self.ledger_sequence is not None

    @property
    def requires_action(self):
        """A direct charge waiting for the customer (OTP, PIN, redirect, ...)."""
        return bool((self.metadata or {}).get('next_action')) and self.is_pending

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def can_be_refunded(self):
        """Successful Flutterwave deposits can be refunded to the payer."""
        return self.is_successful and self.transaction_type == TRANSACTION_TYPE_DEPOSIT and bool(self.flw_id)

    def can_be_reversed(self):
        """Internal movements (transfers, payments) can be reversed by staff."""
        return (self.is_successful and self.is_debit
                and self.transaction_type in (TRANSACTION_TYPE_TRANSFER, TRANSACTION_TYPE_PAYMENT))
