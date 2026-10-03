"""Shared building blocks for the wallet services."""
import json
import logging
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.conf import is_feature_enabled, wallet_settings
from flutterwave_wallet.constants import FINAL_TRANSACTION_STATUSES
from flutterwave_wallet.exceptions import (
    DuplicateReference,
    FeatureDisabled,
    InvalidAmount,
    TransactionLimitExceeded,
)
from flutterwave_wallet.flutterwave import get_flutterwave_client
from flutterwave_wallet.models import PlatformFee, Transaction
from flutterwave_wallet.services import ledger
from flutterwave_wallet.signals import (
    platform_fee_recorded,
    send_on_commit,
    transaction_needs_review,
    transaction_status_changed,
)
from flutterwave_wallet.utils.money import to_decimal
from flutterwave_wallet.utils.references import validate_reference

logger = logging.getLogger('flutterwave_wallet')


class BaseService:
    """Gives every service a Flutterwave client and the common guards."""

    def __init__(self, flutterwave=None, flutterwave_v4=None):
        self._flutterwave = flutterwave
        self._flutterwave_v4 = flutterwave_v4

    @property
    def flutterwave(self):
        """The v3 client (hosted checkout, saved cards, bills, KYC - and everything when API_VERSION='v3')."""
        if self._flutterwave is None:
            self._flutterwave = get_flutterwave_client()
        return self._flutterwave

    @flutterwave.setter
    def flutterwave(self, client):
        self._flutterwave = client

    @property
    def flutterwave_v4(self):
        if self._flutterwave_v4 is None:
            from flutterwave_wallet.flutterwave.v4 import get_flutterwave_v4_client
            self._flutterwave_v4 = get_flutterwave_v4_client()
        return self._flutterwave_v4

    @property
    def api_version(self):
        return str(wallet_settings.API_VERSION or 'v3').lower()

    def gateway(self, version=None):
        """Gateway for ``version`` (default: ``FLW_WALLET_API_VERSION``)."""
        from flutterwave_wallet.gateways import V3Gateway, V4Gateway
        version = (version or self.api_version).lower()
        return V4Gateway(self.flutterwave_v4) if version == 'v4' else V3Gateway(self.flutterwave)

    def gateway_for_id(self, flw_id):
        from flutterwave_wallet.gateways import version_for_id
        return self.gateway(version_for_id(flw_id))

    def clients(self):
        return {'flutterwave': self._flutterwave, 'flutterwave_v4': self._flutterwave_v4}

    # Ledger shortcuts
    lock_wallet = staticmethod(ledger.lock_wallet)
    lock_wallets = staticmethod(ledger.lock_wallets)
    lock_transaction = staticmethod(ledger.lock_transaction)
    post = staticmethod(ledger.post)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    @staticmethod
    def require_feature(feature):
        if not is_feature_enabled(feature):
            raise FeatureDisabled(_("{feature} are disabled on this platform").format(
                feature=feature.replace('_', ' ').capitalize()))

    @staticmethod
    def new_reference(reference, generator):
        """Validate a caller-supplied reference (format + unused) or generate one."""
        if reference is None:
            return generator()
        reference = validate_reference(reference)
        if Transaction.objects.filter(reference=reference).exists():
            raise DuplicateReference()
        return reference

    @staticmethod
    def check_amount_limits(amount, minimum_setting=None):
        """Per-transaction minimum/maximum checks on the principal amount."""
        amount = to_decimal(amount)
        for limit in (wallet_settings.get(minimum_setting) if minimum_setting else None,
                      wallet_settings.MINIMUM_TRANSACTION_AMOUNT):
            if limit is not None and amount < Decimal(str(limit)):
                raise InvalidAmount(_("The minimum amount is {limit}").format(limit=limit))
        maximum = wallet_settings.MAXIMUM_TRANSACTION_AMOUNT
        if maximum is not None and amount > Decimal(str(maximum)):
            raise TransactionLimitExceeded(_("The maximum amount per transaction is {limit}").format(limit=maximum))
        return amount

    @staticmethod
    def check_daily_limit(locked_wallet, outgoing_total):
        """Call with the wallet row locked so concurrent requests can't race past the limit."""
        limit = locked_wallet.get_daily_limit()
        if limit is None:
            return
        spent = locked_wallet.get_daily_outgoing_total()
        if spent + to_decimal(outgoing_total) > limit:
            remaining = max(limit - spent, Decimal('0'))
            raise TransactionLimitExceeded(
                _("Daily limit of {limit} exceeded. You can still send {remaining} today").format(
                    limit=limit, remaining=remaining))

    @staticmethod
    def require_no_outer_transaction(operation):
        """
        Money-out operations debit the wallet, *commit*, then call Flutterwave. Inside an outer
        ``transaction.atomic()`` (or ``ATOMIC_REQUESTS``) the debit would only be a savepoint: if
        that outer transaction later rolled back, the payout would have left with no debit.
        """
        connection = db_transaction.get_connection()
        outer = [block for block in getattr(connection, 'atomic_blocks', [])
                 if not getattr(block, '_from_testcase', False)]
        if outer:
            raise RuntimeError(
                f"{operation} must not run inside transaction.atomic() or ATOMIC_REQUESTS: the wallet debit "
                "has to be committed before Flutterwave is called. Call it outside your transaction."
            )

    @staticmethod
    def create_transaction(**fields):
        """Insert a ledger row, turning a reference collision into DuplicateReference."""
        try:
            with db_transaction.atomic():
                return Transaction.objects.create(**fields)
        except IntegrityError as exc:
            if Transaction.objects.filter(reference=fields.get('reference')).exists():
                raise DuplicateReference() from exc
            raise

    # ------------------------------------------------------------------
    # Status & review
    # ------------------------------------------------------------------

    @staticmethod
    def set_status(txn, new_status, reason=None, extra_fields=()):
        """Change a transaction's status (saves), stamping completion and emitting signals after commit."""
        old_status = txn.status
        txn.status = new_status
        fields = {'status', 'updated_at', *extra_fields}
        if new_status in FINAL_TRANSACTION_STATUSES:
            txn.completed_at = txn.completed_at or timezone.now()
            fields.add('completed_at')
        if reason is not None:
            txn.failed_reason = str(reason)[:2000]
            fields.add('failed_reason')
        txn.save(update_fields=list(fields))
        if old_status != new_status:
            send_on_commit(transaction_status_changed, sender=Transaction, transaction=txn, wallet=txn.wallet,
                           old_status=old_status, new_status=new_status)
        return txn

    @staticmethod
    def flag_for_review(txn, reason):
        """Mark a transaction for a human to look at (unknown outcome, amount mismatch, ...)."""
        txn.needs_review = True
        txn.metadata = {**(txn.metadata or {}), 'review_reason': str(reason)[:500]}
        txn.save(update_fields=['needs_review', 'metadata', 'updated_at'])
        logger.warning("Transaction %s needs review: %s", txn.reference, reason)
        send_on_commit(transaction_needs_review, sender=Transaction, transaction=txn, wallet=txn.wallet,
                       reason=str(reason))
        return txn

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------

    @staticmethod
    def record_fee(txn, fee, amount=None):
        """Accrue the platform's revenue on ``txn`` (swept later into the fee wallet)."""
        revenue = amount if amount is not None else fee.platform_revenue
        if not isinstance(revenue, Money):
            revenue = Money(to_decimal(revenue), txn.amount.currency)
        if revenue.amount <= 0:
            return None
        record, created = PlatformFee.objects.get_or_create(
            transaction=txn, operation=fee.operation,
            defaults={'amount': revenue, 'bearer': fee.bearer, 'details': fee.to_dict()},
        )
        if created:
            send_on_commit(platform_fee_recorded, sender=PlatformFee, fee=record, transaction=txn)
        return record

    # ------------------------------------------------------------------
    # Concurrency helpers
    # ------------------------------------------------------------------

    @staticmethod
    def claim(key, seconds=300):
        """Best-effort cross-process claim (cache ``add``) so workers don't duplicate API calls."""
        try:
            return cache.add(f'flw-wallet-claim:{key}', 1, seconds)
        except Exception:  # cache down: correctness never depends on this, only efficiency
            logger.warning("Cache unavailable for claim %s", key)
            return True

    @staticmethod
    def release(key):
        try:
            cache.delete(f'flw-wallet-claim:{key}')
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    @staticmethod
    def audit(action, performed_by=None, **details):
        """Record a sensitive action on the ``FLW_WALLET_AUDIT_LOGGER`` log (route it to durable storage)."""
        actor = getattr(performed_by, 'pk', performed_by)
        payload = {'action': action, 'performed_by': str(actor) if actor is not None else None,
                   'at': timezone.now().isoformat(), **{key: str(value) for key, value in details.items()}}
        logging.getLogger(wallet_settings.AUDIT_LOGGER).info(json.dumps(payload, sort_keys=True))

    @staticmethod
    def actor(performed_by):
        actor = getattr(performed_by, 'pk', performed_by)
        return {'performed_by': str(actor)} if actor is not None else {}

    @staticmethod
    def request_context(ip_address=None, user_agent=None):
        return {'ip_address': ip_address or None, 'user_agent': (user_agent or '')[:1000]}
