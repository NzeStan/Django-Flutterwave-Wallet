"""
Bill payments from wallet balance: airtime, data bundles, cable TV, electricity, internet...

    service.bill_categories(country='NG')
    service.billers('AIRTIME')
    service.biller_items('BIL099')
    service.validate_bill_customer('AT099', '08031234567')
    service.pay_bill(wallet, biller_code='BIL099', item_code='AT099', customer_id='08031234567', amount='500')

Same safety rules as withdrawals: the wallet is debited first; a definitive rejection
or a verified failure puts the money back exactly once; an unknown outcome stays pending
and is reconciled (``GET /bills/:reference``).
"""
import logging

from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    PAYMENT_METHOD_WALLET,
    TRANSACTION_STATUS_FAILED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_STATUS_PROCESSING,
    TRANSACTION_STATUS_REVERSED,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_BILL_PAYMENT,
    TRANSACTION_TYPE_REVERSAL,
)
from flutterwave_wallet.exceptions import FlutterwaveAPIError, InvalidTransactionState, WalletError
from flutterwave_wallet.models import Transaction
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.fee_service import get_fee_calculator
from flutterwave_wallet.services.fee_sweep_service import FeeService
from flutterwave_wallet.signals import bill_payment_completed, bill_payment_failed, send_on_commit
from flutterwave_wallet.utils.money import api_amount
from flutterwave_wallet.utils.references import generate_reference

logger = logging.getLogger('flutterwave_wallet')

BILL_SUCCESS = {'success', 'successful', 'completed'}
BILL_FAILED = {'failed', 'reversed', 'error', 'cancelled'}


def bill_status(data):
    data = data or {}
    return str(data.get('status') or data.get('transaction_status') or '').strip().lower()


class BillPaymentService(BaseService):

    # ------------------------------------------------------------------
    # Catalogue (read-only pass-through)
    # ------------------------------------------------------------------

    def bill_categories(self, country=None):
        return self.flutterwave.bills.categories(country=country or wallet_settings.COUNTRY)

    def billers(self, category, country=None):
        return self.flutterwave.bills.billers(category, country=country or wallet_settings.COUNTRY)

    def biller_items(self, biller_code):
        return self.flutterwave.bills.items(biller_code)

    def validate_bill_customer(self, item_code, customer_id):
        return self.flutterwave.bills.validate_customer(item_code, customer_id)

    # ------------------------------------------------------------------
    # Payment
    # ------------------------------------------------------------------

    def pay_bill(self, wallet, biller_code, item_code, customer_id, amount, reference=None, country=None,
                 validate_customer=True, description=None, metadata=None, ip_address=None, user_agent=None):
        """Pay a bill from ``wallet``. Returns the bill-payment Transaction."""
        self.require_feature('BILL_PAYMENTS')
        self.require_no_outer_transaction('pay_bill()')
        wallet.check_active()
        amount = wallet.validate_amount(amount)
        self.check_amount_limits(amount)
        reference = self.new_reference(reference, lambda: generate_reference('BIL'))
        customer_id = str(customer_id or '').strip()
        if not biller_code or not item_code or not customer_id:
            raise WalletError(_("biller_code, item_code and customer_id are required"))

        customer_name = None
        if validate_customer:
            try:
                details = self.validate_bill_customer(item_code, customer_id) or {}
            except FlutterwaveAPIError as exc:
                if exc.is_definitive:
                    raise WalletError(_("Customer validation failed: {reason}").format(
                        reason=exc.flutterwave_message or exc.message)) from exc
                raise
            customer_name = details.get('name') if isinstance(details, dict) else None

        fee = get_fee_calculator(wallet, self.flutterwave).calculate(amount, 'bill_payment')
        with db_transaction.atomic():
            locked = self.lock_wallet(wallet)
            locked.check_active()
            self.check_daily_limit(locked, fee.customer_pays.amount)
            txn = Transaction(
                wallet=locked, amount=amount, fees=fee.fee_amount, total_amount=fee.customer_pays,
                fee_bearer=fee.bearer, transaction_type=TRANSACTION_TYPE_BILL_PAYMENT, direction=DIRECTION_DEBIT,
                status=TRANSACTION_STATUS_PENDING, reference=reference, payment_method=PAYMENT_METHOD_WALLET,
                description=description or str(_("Bill payment")),
                metadata={**(metadata or {}), 'fee': fee.to_dict(), 'biller_code': biller_code,
                          'item_code': item_code, 'customer_id': customer_id, 'customer_name': customer_name},
                **self.request_context(ip_address, user_agent),
            )
            self.post(locked, txn)
            self.record_fee(txn, fee)
        wallet.balance = locked.balance

        try:
            data = self.flutterwave.bills.pay(
                biller_code=biller_code, item_code=item_code, customer_id=customer_id,
                amount=api_amount(amount.amount), country=country or wallet_settings.COUNTRY, reference=reference,
            )
        except FlutterwaveAPIError as exc:
            if exc.is_definitive:
                self.reverse_bill_payment(txn, reason=exc.flutterwave_message or exc.message)
                raise
            self.flag_for_review(txn, f"Bill payment outcome unknown, will reconcile: {exc.message}")
            return txn
        return self.apply_bill_state(txn, data or {}, assume_processing=True)

    def apply_bill_state(self, transaction, data, webhook_event=None, assume_processing=False):
        status = bill_status(data)
        with db_transaction.atomic():
            txn = self.lock_transaction(transaction)
            if webhook_event is not None and webhook_event.transaction_id is None:
                webhook_event.transaction = txn
                webhook_event.save(update_fields=['transaction', 'updated_at'])
            if data:
                txn.flw_response = data
                txn.flw_ref = data.get('flw_ref') or txn.flw_ref
                token = data.get('recharge_token') or data.get('token') or data.get('extra')
                if token:
                    txn.metadata = {**txn.metadata, 'token': token}
                txn.save(update_fields=['flw_response', 'flw_ref', 'metadata', 'updated_at'])
            if txn.is_final:
                if status in BILL_SUCCESS and txn.status in (TRANSACTION_STATUS_FAILED, TRANSACTION_STATUS_REVERSED):
                    self.flag_for_review(txn, "Bill reported successful after the payment was refunded")
                return txn
            if status in BILL_SUCCESS:
                self.set_status(txn, TRANSACTION_STATUS_SUCCESS)
                send_on_commit(bill_payment_completed, sender=Transaction, transaction=txn, wallet=txn.wallet,
                               data=data)
                return txn
            if status in BILL_FAILED:
                return self.reverse_bill_payment(txn, reason=data.get('message') or str(_("Bill payment failed")))
            if assume_processing and txn.status == TRANSACTION_STATUS_PENDING:
                self.set_status(txn, TRANSACTION_STATUS_PROCESSING)
            return txn

    def verify_bill_payment(self, transaction):
        """Fetch the bill's status from Flutterwave and settle it."""
        data = self.flutterwave.bills.status(transaction.reference, verbose=1) or {}
        return self.apply_bill_state(transaction, data)

    def handle_bill_webhook(self, data, webhook_event=None):
        reference = data.get('tx_ref') or data.get('reference') or data.get('customer_reference')
        txn = Transaction.objects.filter(reference=reference, transaction_type=TRANSACTION_TYPE_BILL_PAYMENT).first()
        if txn is None:
            return None
        if wallet_settings.VERIFY_WEBHOOKS_WITH_API:
            return self.verify_bill_payment(txn)
        return self.apply_bill_state(txn, data, webhook_event=webhook_event)

    def reverse_bill_payment(self, transaction, reason):
        with db_transaction.atomic():
            txn = self.lock_transaction(transaction)
            if txn.status in (TRANSACTION_STATUS_FAILED, TRANSACTION_STATUS_REVERSED):
                return txn
            if txn.status == TRANSACTION_STATUS_SUCCESS:
                raise InvalidTransactionState(_("A successful bill payment can't be reversed automatically"))
            locked = self.lock_wallet(txn.wallet_id)
            self.post(locked, Transaction(
                wallet=locked, amount=txn.total_amount, total_amount=txn.total_amount,
                transaction_type=TRANSACTION_TYPE_REVERSAL, direction=DIRECTION_CREDIT,
                status=TRANSACTION_STATUS_SUCCESS, completed_at=timezone.now(), reference=f"{txn.reference}-REV",
                related_transaction=txn, payment_method=PAYMENT_METHOD_WALLET,
                description=str(_("Refund of failed bill payment {reference}").format(reference=txn.reference)),
                metadata={'reason': str(reason)[:500]},
            ), force=True)
            FeeService.void_fees(txn)
            self.set_status(txn, TRANSACTION_STATUS_FAILED, reason=reason)
            send_on_commit(bill_payment_failed, sender=Transaction, transaction=txn, wallet=locked, reason=str(reason))
        return txn
