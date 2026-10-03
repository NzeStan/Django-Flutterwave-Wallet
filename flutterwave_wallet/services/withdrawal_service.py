"""
Withdrawals from a wallet to a bank account or mobile money wallet (Flutterwave Transfers).

Money safety rules:

1. The wallet is debited *before* Flutterwave is called, in the same DB transaction
   that records the withdrawal - the money can't be spent twice while in flight.
2. If Flutterwave definitively rejects the transfer (4xx), the debit is reversed at once.
3. If the outcome is unknown (timeout, 5xx), nothing is reversed: the transfer may have
   gone out. It stays pending, is flagged for review, and is settled by the
   ``transfer.completed`` webhook or by reconciliation.
4. A ``FAILED`` status is only acted on after re-fetching the transfer from Flutterwave,
   and the money goes back exactly once.
5. Never use "retry transfer" on the Flutterwave dashboard for a wallet withdrawal that
   already failed here: the user has already been refunded.
"""
import logging
from datetime import timedelta

from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    BENEFICIARY_TYPE_MOBILE_MONEY,
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_MOBILE_MONEY,
    TRANSACTION_STATUS_FAILED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_STATUS_PROCESSING,
    TRANSACTION_STATUS_REVERSED,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_REVERSAL,
    TRANSACTION_TYPE_WITHDRAWAL,
)
from flutterwave_wallet.exceptions import BankAccountError, FlutterwaveAPIError, InvalidTransactionState
from flutterwave_wallet.models import Transaction
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.fee_service import get_fee_calculator
from flutterwave_wallet.services.fee_sweep_service import FeeService
from flutterwave_wallet.signals import send_on_commit, withdrawal_completed, withdrawal_failed, withdrawal_initiated
from flutterwave_wallet.utils.references import generate_reference

logger = logging.getLogger('flutterwave_wallet')

TRANSFER_SUCCESS = {'SUCCESSFUL', 'SUCCESS'}
TRANSFER_FAILED = {'FAILED', 'CANCELLED'}
TRANSFER_REVERSED = {'REVERSED'}


class WithdrawalService(BaseService):

    def withdraw(self, wallet, amount, bank_account, narration=None, reference=None, metadata=None,
                 ip_address=None, user_agent=None):
        """
        Send ``amount`` from ``wallet`` to ``bank_account``. Returns the withdrawal Transaction
        (PENDING until Flutterwave confirms; SUCCESS or FAILED+refunded afterwards).
        """
        self.require_feature('WITHDRAWALS')
        self.require_no_outer_transaction('withdraw()')
        if bank_account is None:
            raise BankAccountError(_("A payout account is required"))
        if bank_account.user_id != wallet.user_id:
            raise BankAccountError(_("This payout account belongs to another user"))
        if not bank_account.is_active:
            raise BankAccountError(_("This payout account has been removed"))
        if bank_account.currency != wallet.currency:
            raise BankAccountError(_("This payout account receives {currency}").format(currency=bank_account.currency))

        wallet.check_active()
        amount = wallet.validate_amount(amount)
        self.check_amount_limits(amount, 'MINIMUM_WITHDRAWAL_AMOUNT')
        reference = self.new_reference(reference, lambda: generate_reference('WDR'))
        mobile = bank_account.beneficiary_type == BENEFICIARY_TYPE_MOBILE_MONEY
        fee = get_fee_calculator(wallet, self.flutterwave).calculate(
            amount, 'withdrawal', context={'transfer_type': 'mobilemoney' if mobile else 'account'})
        payout = fee.merchant_receives
        narration = (narration or str(_("Wallet withdrawal")))[:100]

        with db_transaction.atomic():
            locked = self.lock_wallet(wallet)
            locked.check_active()
            self.check_daily_limit(locked, fee.customer_pays.amount)
            txn = Transaction(
                wallet=locked, amount=amount, fees=fee.fee_amount, total_amount=fee.customer_pays,
                api_version=self.api_version,
                fee_bearer=fee.bearer, transaction_type=TRANSACTION_TYPE_WITHDRAWAL, direction=DIRECTION_DEBIT,
                status=TRANSACTION_STATUS_PENDING, reference=reference, bank_account=bank_account,
                payment_method=PAYMENT_METHOD_MOBILE_MONEY if mobile else PAYMENT_METHOD_BANK_TRANSFER,
                description=narration,
                metadata={**(metadata or {}), 'fee': fee.to_dict(), 'payout_amount': str(payout.amount),
                          'account_bank': bank_account.account_bank,
                          'account_number': bank_account.masked_account_number},
                **self.request_context(ip_address, user_agent),
            )
            self.post(locked, txn)
            self.record_fee(txn, fee)
        wallet.balance = locked.balance

        try:
            data = self.gateway(txn.api_version).create_transfer(txn, bank_account, payout.amount, narration)
        except FlutterwaveAPIError as exc:
            if exc.is_definitive:
                self.reverse_withdrawal(txn, reason=exc.flutterwave_message or exc.message,
                                        final_status=TRANSACTION_STATUS_FAILED)
                raise
            self.flag_for_review(txn, f"Transfer outcome unknown, will reconcile: {exc.message}")
            send_on_commit(withdrawal_initiated, sender=Transaction, transaction=txn, wallet=txn.wallet)
            return txn

        txn = self.apply_transfer_state(txn, data or {})
        send_on_commit(withdrawal_initiated, sender=Transaction, transaction=txn, wallet=txn.wallet)
        return txn

    # ------------------------------------------------------------------
    # State handling
    # ------------------------------------------------------------------

    def apply_transfer_state(self, transaction, data, webhook_event=None):
        """Apply a Flutterwave transfer object (create/fetch/webhook data) to a withdrawal."""
        status = str(data.get('status') or '').upper()
        with db_transaction.atomic():
            txn = self.lock_transaction(transaction)
            if webhook_event is not None and webhook_event.transaction_id is None:
                webhook_event.transaction = txn
                webhook_event.save(update_fields=['transaction', 'updated_at'])
            if data.get('reference') and data['reference'] != txn.reference:
                logger.error("Transfer data for %s carries reference %s", txn.reference, data.get('reference'))
                return txn
            if data.get('id'):
                txn.flw_id = str(data['id'])
            txn.flw_response = data
            txn.save(update_fields=['flw_id', 'flw_response', 'updated_at'])

            if status in TRANSFER_REVERSED and txn.status == TRANSACTION_STATUS_SUCCESS:
                # The money came back after a successful payout (v4 transfer.reversal)
                return self.reverse_withdrawal(txn, reason=data.get('complete_message') or str(_("Transfer reversed")),
                                               final_status=TRANSACTION_STATUS_REVERSED)
            if txn.is_final:
                if status in TRANSFER_SUCCESS and txn.status in (TRANSACTION_STATUS_FAILED, TRANSACTION_STATUS_REVERSED):
                    # We refunded the user but the money left after all (e.g. a manual dashboard retry).
                    self.flag_for_review(txn, "Transfer succeeded after the withdrawal was refunded")
                return txn

            if status in TRANSFER_SUCCESS:
                self.set_status(txn, TRANSACTION_STATUS_SUCCESS)
                send_on_commit(withdrawal_completed, sender=Transaction, transaction=txn, wallet=txn.wallet)
                return txn
            if status in TRANSFER_FAILED or status in TRANSFER_REVERSED:
                reason = data.get('complete_message') or str(_("Transfer failed"))
                return self.reverse_withdrawal(txn, reason=reason, final_status=TRANSACTION_STATUS_FAILED)

            txn.metadata = {**(txn.metadata or {}), 'transfer_status': status or 'UNKNOWN',
                            'requires_approval': data.get('requires_approval')}
            txn.save(update_fields=['metadata', 'updated_at'])
            if txn.status == TRANSACTION_STATUS_PENDING and status in ('NEW', 'PENDING'):
                self.set_status(txn, TRANSACTION_STATUS_PROCESSING)
            return txn

    def handle_transfer_webhook(self, data, webhook_event=None, event_type=None):
        """
        ``transfer.completed`` (v3), ``transfer.disburse`` / ``transfer.reversal`` (v4): re-fetch the
        transfer from Flutterwave, then apply it.
        """
        txn = self._find_withdrawal(data)
        if txn is None:
            logger.info("Transfer webhook for unknown reference %s", data.get('reference'))
            return None
        if wallet_settings.VERIFY_WEBHOOKS_WITH_API and data.get('id'):
            data = self.gateway_for_id(data['id']).fetch_transfer(data['id']) or {}
            if event_type == 'transfer.reversal' and str(data.get('status') or '').upper() == 'SUCCESSFUL':
                self.flag_for_review(txn, "Flutterwave sent transfer.reversal but still reports the transfer "
                                          "as successful")
        elif event_type == 'transfer.reversal':
            data = {**data, 'status': 'REVERSED'}
        return self.apply_transfer_state(txn, data, webhook_event=webhook_event)

    def verify_withdrawal(self, transaction):
        """Fetch the transfer from Flutterwave and settle the local state."""
        gateway = self.gateway(transaction.api_version)
        if transaction.flw_id:
            return self.apply_transfer_state(transaction, gateway.fetch_transfer(transaction.flw_id) or {})
        found = gateway.find_transfer(transaction)
        if found:
            return self.apply_transfer_state(transaction, found)
        if transaction.created_at < timezone.now() - timedelta(hours=1) and not transaction.needs_review:
            self.flag_for_review(transaction, "Flutterwave has no transfer with this reference; confirm on the "
                                              "dashboard, then use force_reverse_withdrawal if it never left")
        return transaction

    # ------------------------------------------------------------------
    # Reversal
    # ------------------------------------------------------------------

    def reverse_withdrawal(self, transaction, reason, final_status=TRANSACTION_STATUS_FAILED):
        """Credit a withdrawal's debit back exactly once and mark it failed/reversed."""
        with db_transaction.atomic():
            txn = self.lock_transaction(transaction)
            if txn.status in (TRANSACTION_STATUS_FAILED, TRANSACTION_STATUS_REVERSED):
                return txn
            if txn.status == TRANSACTION_STATUS_SUCCESS and final_status != TRANSACTION_STATUS_REVERSED:
                raise InvalidTransactionState(_("A successful withdrawal can't be marked failed"))
            locked = self.lock_wallet(txn.wallet_id)
            refund = Transaction(
                wallet=locked, amount=txn.total_amount, total_amount=txn.total_amount,
                transaction_type=TRANSACTION_TYPE_REVERSAL, direction=DIRECTION_CREDIT,
                status=TRANSACTION_STATUS_SUCCESS, completed_at=timezone.now(), reference=f"{txn.reference}-REV",
                related_transaction=txn, payment_method=txn.payment_method,
                description=str(_("Reversal of withdrawal {reference}").format(reference=txn.reference)),
                metadata={'reason': str(reason)[:500]},
            )
            self.post(locked, refund, force=True)
            FeeService.void_fees(txn)
            self.set_status(txn, final_status, reason=reason)
            send_on_commit(withdrawal_failed, sender=Transaction, transaction=txn, wallet=locked, reason=str(reason))
        return txn

    def force_reverse_withdrawal(self, transaction, reason, performed_by=None):
        """
        Staff action for a withdrawal flagged for review that you have confirmed on the
        Flutterwave dashboard never left. Audited.
        """
        if not transaction.needs_review or not transaction.is_pending:
            raise InvalidTransactionState(_("Only pending withdrawals flagged for review can be force-reversed"))
        txn = self.reverse_withdrawal(transaction, reason=reason, final_status=TRANSACTION_STATUS_FAILED)
        self.audit('withdrawal.force_reverse', performed_by, transaction=txn.reference, reason=reason)
        return txn

    @staticmethod
    def _find_withdrawal(data):
        queryset = Transaction.objects.filter(transaction_type=TRANSACTION_TYPE_WITHDRAWAL)
        if data.get('reference'):
            txn = queryset.filter(reference=data['reference']).first()
            if txn:
                return txn
        if data.get('id'):
            return queryset.filter(flw_id=str(data['id'])).first()
        return None
