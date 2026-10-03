"""
Refunds of deposits back to the payer's card / account (``POST /transactions/:id/refund``).

The wallet is debited when the refund is requested (so the money can't be spent while
the refund is in flight). A definitive rejection or a verified failed refund puts it back.
"""
import logging
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.constants import (
    ACTIVE_STATUSES,
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    TRANSACTION_STATUS_FAILED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_STATUS_PROCESSING,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_REFUND,
    TRANSACTION_TYPE_REVERSAL,
)
from flutterwave_wallet.exceptions import FlutterwaveAPIError, InvalidAmount, InvalidTransactionState
from flutterwave_wallet.models import Transaction
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.signals import refund_completed, refund_failed, refund_initiated, send_on_commit
from flutterwave_wallet.utils.money import to_decimal

logger = logging.getLogger('flutterwave_wallet')

REFUND_SUCCESS = {'completed', 'successful', 'success', 'succeeded'}
REFUND_FAILED = {'failed', 'declined', 'cancelled'}


class RefundService(BaseService):

    @staticmethod
    def refundable_amount(transaction):
        refunded = Transaction.objects.filter(
            related_transaction=transaction, transaction_type=TRANSACTION_TYPE_REFUND,
            status__in=(*ACTIVE_STATUSES, TRANSACTION_STATUS_SUCCESS),
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        return max(transaction.total_amount.amount - refunded, Decimal('0'))

    def refund_deposit(self, transaction, amount=None, reason=None, performed_by=None):
        """Refund (part of) a deposit to the payer. Returns the refund Transaction."""
        self.require_feature('REFUNDS')
        self.require_no_outer_transaction('refund_deposit()')
        if not transaction.can_be_refunded():
            raise InvalidTransactionState(_("Only successful Flutterwave deposits can be refunded"))

        with db_transaction.atomic():
            original = self.lock_transaction(transaction)
            refundable = self.refundable_amount(original)
            value = to_decimal(amount) if amount is not None else refundable
            if value <= 0:
                raise InvalidAmount(_("Nothing left to refund"))
            if value > refundable:
                raise InvalidAmount(_("You can refund at most {amount}").format(amount=refundable))
            refund_money = Money(value, original.amount.currency)
            wallet = self.lock_wallet(original.wallet_id)
            refund = Transaction(
                wallet=wallet, amount=refund_money, total_amount=refund_money, transaction_type=TRANSACTION_TYPE_REFUND,
                api_version=original.api_version,
                direction=DIRECTION_DEBIT, status=TRANSACTION_STATUS_PENDING, reference=f"{original.reference}-RF"
                f"{Transaction.objects.filter(related_transaction=original, transaction_type=TRANSACTION_TYPE_REFUND).count() + 1}",
                related_transaction=original, payment_method=original.payment_method,
                description=reason or str(_("Refund of {reference}").format(reference=original.reference)),
                metadata={'reason': reason or '', **self.actor(performed_by)},
            )
            self.post(wallet, refund, force=True, enforce_minimum_balance=False)
        self.audit('refund.create', performed_by, transaction=original.reference, refund=refund.reference,
                   amount=value, reason=reason or '')

        try:
            data = self.gateway(original.api_version).create_refund(original, refund, value, reason)
        except FlutterwaveAPIError as exc:
            if exc.is_definitive:
                self._fail_refund(refund, exc.flutterwave_message or exc.message)
                raise
            self.flag_for_review(refund, f"Refund outcome unknown: {exc.message}")
            return refund
        send_on_commit(refund_initiated, sender=Transaction, transaction=refund, wallet=refund.wallet,
                       original_transaction=original)
        return self.apply_refund_state(refund, data or {})

    def apply_refund_state(self, refund, data):
        status = str(data.get('status') or '').lower()
        with db_transaction.atomic():
            refund = self.lock_transaction(refund)
            if data.get('id'):
                refund.flw_id = str(data['id'])
            refund.flw_response = data
            refund.save(update_fields=['flw_id', 'flw_response', 'updated_at'])
            if refund.is_final:
                return refund
            if status in REFUND_SUCCESS:
                self.set_status(refund, TRANSACTION_STATUS_SUCCESS)
                send_on_commit(refund_completed, sender=Transaction, transaction=refund, wallet=refund.wallet,
                               original_transaction=refund.related_transaction)
                return refund
            if status in REFUND_FAILED:
                return self._fail_refund(refund, data.get('comments') or data.get('message') or status)
            if refund.status == TRANSACTION_STATUS_PENDING:
                self.set_status(refund, TRANSACTION_STATUS_PROCESSING)
        return refund

    def verify_refund(self, refund):
        if not refund.flw_id:
            return refund
        return self.apply_refund_state(refund, self.gateway(refund.api_version).fetch_refund(refund.flw_id) or {})

    def handle_refund_webhook(self, data, webhook_event=None):
        refund_id = data.get('id')
        refund = Transaction.objects.filter(transaction_type=TRANSACTION_TYPE_REFUND, flw_id=str(refund_id)).first() \
            if refund_id else None
        if refund is None:
            return None
        if webhook_event is not None and webhook_event.transaction_id is None:
            webhook_event.transaction = refund
            webhook_event.save(update_fields=['transaction', 'updated_at'])
        return self.verify_refund(refund)

    def _fail_refund(self, refund, reason):
        with db_transaction.atomic():
            refund = self.lock_transaction(refund)
            if refund.is_final:
                return refund
            wallet = self.lock_wallet(refund.wallet_id)
            self.post(wallet, Transaction(
                wallet=wallet, amount=refund.total_amount, total_amount=refund.total_amount,
                transaction_type=TRANSACTION_TYPE_REVERSAL, direction=DIRECTION_CREDIT,
                status=TRANSACTION_STATUS_SUCCESS, completed_at=timezone.now(), reference=f"{refund.reference}-REV",
                related_transaction=refund,
                description=str(_("Failed refund {reference} returned to wallet").format(reference=refund.reference)),
                metadata={'reason': str(reason)[:500]},
            ), force=True)
            self.set_status(refund, TRANSACTION_STATUS_FAILED, reason=reason)
            send_on_commit(refund_failed, sender=Transaction, transaction=refund, wallet=wallet,
                           original_transaction=refund.related_transaction, reason=str(reason))
        return refund
