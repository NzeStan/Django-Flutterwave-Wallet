from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.exceptions import (
    CurrencyMismatchError,
    InsufficientFunds,
    InvalidAmount,
    MinimumBalanceViolation,
    WalletInactive,
    WalletLocked,
)
from flutterwave_wallet.models.base import BaseModel
from flutterwave_wallet.models.compat import check_constraint
from flutterwave_wallet.models.fields import WalletMoneyField


class WalletQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True, is_locked=False)

    def locked(self):
        return self.filter(is_locked=True)

    def for_currency(self, currency):
        return self.filter(balance_currency=str(currency).upper())


class WalletManager(models.Manager.from_queryset(WalletQuerySet)):
    pass


class Wallet(BaseModel):
    """
    A user's balance in one currency (a user has at most one wallet per currency).

    The balance must only ever change through :meth:`credit` / :meth:`debit`, which
    the ledger service (:mod:`flutterwave_wallet.services.ledger`) calls under a
    ``SELECT ... FOR UPDATE`` row lock, always together with a ledger entry.
    A database constraint guarantees the balance never goes negative.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='flutterwave_wallets', verbose_name=_('User'),
    )
    balance = WalletMoneyField(default=0, verbose_name=_('Balance'))

    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('Is active'))
    is_locked = models.BooleanField(default=False, db_index=True, verbose_name=_('Is locked'))
    locked_reason = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Locked reason'))
    last_transaction_date = models.DateTimeField(null=True, blank=True, verbose_name=_('Last transaction date'))

    ledger_sequence = models.PositiveBigIntegerField(
        default=0, editable=False, verbose_name=_('Ledger sequence'),
        help_text=_('Incremented on every balance change; orders the statement deterministically'),
    )
    ledger_head_hash = models.CharField(
        max_length=64, blank=True, default='', editable=False, verbose_name=_('Ledger head hash'),
        help_text=_('Hash of the latest ledger entry (tamper-evident chain)'),
    )
    daily_limit = models.DecimalField(
        max_digits=19, decimal_places=2, null=True, blank=True, verbose_name=_('Daily limit'),
        help_text=_('Overrides FLW_WALLET_MAXIMUM_DAILY_TRANSACTION for this wallet'),
    )
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_('Metadata'))

    objects = WalletManager()

    class Meta:
        verbose_name = _('Wallet')
        verbose_name_plural = _('Wallets')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'balance_currency'], name='flw_wallet_one_per_currency'),
            check_constraint(Q(balance__gte=0), 'flw_wallet_balance_non_negative'),
        ]
        indexes = [
            models.Index(fields=['is_active', 'is_locked'], name='flw_wallet_status_idx'),
        ]

    def __str__(self):
        user_display = getattr(self.user, 'email', None) or str(self.user)
        return f"{self.currency} wallet ({user_display})"

    def __repr__(self):
        return f"<Wallet id={self.id} user_id={self.user_id} balance={self.balance}>"

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def currency(self):
        return str(self.balance.currency)

    @property
    def is_operational(self):
        return self.is_active and not self.is_locked

    @property
    def profile(self):
        from flutterwave_wallet.models.profile import WalletProfile
        profile, _created = WalletProfile.objects.get_or_create(user_id=self.user_id)
        return profile

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def check_active(self):
        if not self.is_active:
            raise WalletInactive()
        if self.is_locked:
            raise WalletLocked(_("Wallet is locked: {reason}").format(reason=self.locked_reason)
                               if self.locked_reason else None)

    def validate_amount(self, amount):
        """Normalise ``amount`` to a positive Money in the wallet's currency."""
        from flutterwave_wallet.utils.money import to_decimal

        if isinstance(amount, Money):
            if str(amount.currency) != self.currency:
                raise CurrencyMismatchError(_("Currency mismatch: wallet uses {wallet}, got {got}").format(
                    wallet=self.currency, got=amount.currency))
            value = to_decimal(amount.amount)
        elif isinstance(amount, (Decimal, int, str)) and not isinstance(amount, bool):
            value = to_decimal(amount)
        elif isinstance(amount, float):
            raise InvalidAmount(_("Pass money amounts as Decimal or str, never float"))
        else:
            raise InvalidAmount(f"Unsupported amount type {type(amount).__name__}")
        if value <= 0:
            raise InvalidAmount(_("Amount must be greater than zero"))
        return Money(value, self.currency)

    # ------------------------------------------------------------------
    # Balance primitives - call only through the ledger service
    # ------------------------------------------------------------------

    def _require_lock(self):
        if not transaction.get_connection().in_atomic_block:
            raise RuntimeError("Wallet balances can only change inside a transaction holding the row lock")

    def apply_credit(self, amount, force=False):
        """Add ``amount`` to this (row-locked) wallet. ``force`` credits a locked wallet (money already arrived)."""
        self._require_lock()
        amount = self.validate_amount(amount)
        if not force:
            self.check_active()
        self.balance = self.balance + amount
        self._touch()
        return self.balance

    def apply_debit(self, amount, enforce_minimum_balance=True, force=False):
        """Remove ``amount`` from this (row-locked) wallet. Never goes below zero, even with ``force``."""
        self._require_lock()
        amount = self.validate_amount(amount)
        if not force:
            self.check_active()
        if self.balance < amount:
            raise InsufficientFunds(self, amount)
        if enforce_minimum_balance:
            minimum = Decimal(str(wallet_settings.MINIMUM_BALANCE or 0))
            if self.balance.amount - amount.amount < minimum:
                raise MinimumBalanceViolation(
                    _("This transaction would leave less than the minimum balance of {minimum}").format(
                        minimum=Money(minimum, self.currency))
                )
        self.balance = self.balance - amount
        self._touch()
        return self.balance

    def _touch(self):
        self.last_transaction_date = timezone.now()
        self.ledger_sequence += 1

    def save_balance(self):
        self.save(update_fields=['balance', 'balance_currency', 'last_transaction_date', 'ledger_sequence',
                                 'ledger_head_hash', 'updated_at'])

    def refresh_balance(self):
        self.refresh_from_db(fields=['balance', 'balance_currency', 'ledger_sequence', 'ledger_head_hash'])
        return self.balance

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def lock(self, reason=''):
        self.is_locked = True
        self.locked_reason = (reason or '')[:255]
        self.save(update_fields=['is_locked', 'locked_reason', 'updated_at'])

    def unlock(self):
        self.is_locked = False
        self.locked_reason = ''
        self.save(update_fields=['is_locked', 'locked_reason', 'updated_at'])

    # ------------------------------------------------------------------
    # Limits & stats
    # ------------------------------------------------------------------

    def get_daily_limit(self):
        if self.daily_limit is not None:
            return self.daily_limit
        limit = wallet_settings.MAXIMUM_DAILY_TRANSACTION
        return Decimal(str(limit)) if limit is not None else None

    def get_daily_outgoing_total(self):
        """Money that left this wallet today (pending or successful outgoing debits)."""
        from flutterwave_wallet.constants import (
            DIRECTION_DEBIT,
            OUTGOING_TYPES,
            TRANSACTION_STATUS_PENDING,
            TRANSACTION_STATUS_PROCESSING,
            TRANSACTION_STATUS_SUCCESS,
        )

        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        total = self.transactions.filter(
            direction=DIRECTION_DEBIT, transaction_type__in=OUTGOING_TYPES,
            status__in=[TRANSACTION_STATUS_PENDING, TRANSACTION_STATUS_PROCESSING, TRANSACTION_STATUS_SUCCESS],
            created_at__gte=start,
        ).aggregate(total=Sum('total_amount'))['total']
        return total or Decimal('0')
