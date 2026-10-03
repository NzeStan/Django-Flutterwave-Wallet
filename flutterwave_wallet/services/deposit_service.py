"""
Deposits: Flutterwave hosted checkout (the only way cards are accepted), non-card direct
charges (bank transfer, USSD, mobile money, M-Pesa, ...), saved-card token top-ups and
virtual-account transfers.

Flow::

    initialize_checkout() / direct_charge() / charge_saved_card()
        -> PENDING deposit (our reference is Flutterwave's tx_ref)
    (customer pays)
    charge.completed webhook / verify_deposit() / reconciliation
        -> GET /transactions/:id/verify   (never trust the webhook body)
        -> settle_charge(): wallet credited exactly once, deposit SUCCESS

Money safety:

* a wallet is only credited from *verified* data: status, tx_ref, currency and amount
  are checked against what we expected;
* the deposit row is locked while it is settled, so duplicate webhooks, a webhook
  racing a manual verify, or two reconciliation workers can never credit twice;
* money that really arrived is always credited - even if the deposit had been marked
  failed/cancelled after an earlier failed attempt on the same checkout;
* a different currency or an amount mismatch is flagged for review, never guessed.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    CARD_CHARGE_TYPES,
    CHARGE_TYPES,
    DIRECTION_CREDIT,
    FEE_BEARER_CUSTOMER,
    FEE_BEARER_MERCHANT,
    PAYMENT_METHOD_CARD,
    PAYMENT_METHOD_VIRTUAL_ACCOUNT,
    TRANSACTION_STATUS_CANCELLED,
    TRANSACTION_STATUS_FAILED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_DEPOSIT,
    VIRTUAL_ACCOUNT_STATIC,
    payment_method_for,
)
from flutterwave_wallet.exceptions import (
    CardDataNotAllowed,
    FlutterwaveAPIError,
    InvalidAmount,
    InvalidTransactionState,
    WalletError,
)
from flutterwave_wallet.models import Transaction, VirtualAccount
from flutterwave_wallet.gateways import V4_CHARGE_TYPES
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.card_service import CardService
from flutterwave_wallet.services.fee_service import FeeCalculationResult, get_fee_calculator
from flutterwave_wallet.signals import (
    charge_requires_action,
    deposit_completed,
    deposit_failed,
    deposit_initialized,
    send_on_commit,
)
from flutterwave_wallet.utils.money import ZERO, api_amount, from_api, quantize
from flutterwave_wallet.utils.references import generate_reference

logger = logging.getLogger('flutterwave_wallet')

PACKAGE_MARKER = 'django-flutterwave-wallet'
SUCCESS_STATUSES = {'successful', 'success', 'completed'}
FAILED_STATUSES = {'failed', 'cancelled', 'canceled', 'error', 'declined', 'abandoned'}


def charge_status(data):
    return str((data or {}).get('status') or '').strip().lower()


class DepositService(BaseService):

    # ------------------------------------------------------------------
    # Common
    # ------------------------------------------------------------------

    def _prepare(self, wallet, amount, feature='DEPOSITS'):
        self.require_feature('DEPOSITS')
        if feature != 'DEPOSITS':
            self.require_feature(feature)
        wallet.check_active()
        amount = wallet.validate_amount(amount)
        self.check_amount_limits(amount, 'MINIMUM_DEPOSIT_AMOUNT')
        return amount

    def _create_pending_deposit(self, wallet, amount, reference, fee, description, metadata, payment_method=None,
                                card=None, ip_address=None, user_agent=None, extra_metadata=None, api_version='v3'):
        return self.create_transaction(
            wallet=wallet, amount=amount, fees=fee.fee_amount, total_amount=fee.merchant_receives, api_version=api_version,
            fee_bearer=fee.bearer, transaction_type=TRANSACTION_TYPE_DEPOSIT, direction=DIRECTION_CREDIT,
            status=TRANSACTION_STATUS_PENDING, reference=reference, payment_method=payment_method, card=card,
            description=description or str(_("Wallet funding")),
            metadata={**(metadata or {}), **(extra_metadata or {}), 'fee': fee.to_dict(),
                      'charge_amount': str(fee.customer_pays.amount),
                      'expected_credit': str(fee.merchant_receives.amount)},
            **self.request_context(ip_address, user_agent),
        )

    @staticmethod
    def _customer(wallet, email=None, name=None, phone=None):
        user = wallet.user
        full_name = name or (user.get_full_name() if hasattr(user, 'get_full_name') else '') or ''
        profile_phone = getattr(getattr(user, 'wallet_profile', None), 'phone_number', None)
        return {
            'email': email or getattr(user, 'email', None),
            'name': full_name or None,
            'phonenumber': phone or profile_phone,
        }

    def _flutterwave_meta(self, wallet, txn, metadata=None):
        return {**{str(k): v for k, v in (metadata or {}).items()},
                'wallet_id': str(wallet.pk), 'transaction_id': str(txn.pk), 'source': PACKAGE_MARKER}

    def _fail_initialization(self, txn, exc):
        reason = exc.flutterwave_message or exc.message
        self.set_status(txn, TRANSACTION_STATUS_FAILED, reason=f"Flutterwave rejected the request: {reason}")
        send_on_commit(deposit_failed, sender=Transaction, transaction=txn, wallet=txn.wallet, reason=reason)

    # ------------------------------------------------------------------
    # Flutterwave Standard (hosted checkout)
    # ------------------------------------------------------------------

    def initialize_checkout(self, wallet, amount, email=None, redirect_url=None, reference=None, metadata=None,
                            payment_options=None, description=None, customer_name=None, phone_number=None,
                            subaccounts=None, ip_address=None, user_agent=None):
        """
        Start a hosted checkout that funds ``wallet``. Returns ``link`` - send the customer there.

        The wallet is credited only when Flutterwave confirms payment (webhook, the
        callback view, or reconciliation).
        """
        amount = self._prepare(wallet, amount)
        reference = self.new_reference(reference, lambda: generate_reference('DEP'))
        customer = self._customer(wallet, email, customer_name, phone_number)
        if not customer['email']:
            raise WalletError(_("An email address is required for checkout"))

        fee = get_fee_calculator(wallet, self.flutterwave).calculate(amount, 'deposit')
        txn = self._create_pending_deposit(wallet, amount, reference, fee, description, metadata,
                                           ip_address=ip_address, user_agent=user_agent)
        customizations = {k: v for k, v in {
            'title': wallet_settings.CHECKOUT_TITLE, 'logo': wallet_settings.CHECKOUT_LOGO,
            'description': description,
        }.items() if v}
        try:
            data = self.flutterwave.payments.create(
                tx_ref=reference, amount=api_amount(fee.customer_pays.amount), currency=wallet.currency,
                redirect_url=redirect_url or wallet_settings.DEFAULT_REDIRECT_URL,
                customer={k: v for k, v in customer.items() if v},
                customizations=customizations or None, meta=self._flutterwave_meta(wallet, txn, metadata),
                payment_options=payment_options or wallet_settings.PAYMENT_OPTIONS,
                session_duration=wallet_settings.CHECKOUT_SESSION_DURATION,
                max_retry_attempt=wallet_settings.CHECKOUT_MAX_RETRY_ATTEMPTS,
                subaccounts=subaccounts,
            )
        except FlutterwaveAPIError as exc:
            # No link reached the customer, so nothing can be paid; a late success would still be credited.
            self._fail_initialization(txn, exc)
            raise

        link = (data or {}).get('link')
        txn.metadata = {**txn.metadata, 'checkout_link': link}
        txn.save(update_fields=['metadata', 'updated_at'])
        send_on_commit(deposit_initialized, sender=Transaction, transaction=txn, wallet=wallet, link=link)
        return {
            'link': link,
            'reference': reference,
            'transaction_id': str(txn.pk),
            'amount': str(amount.amount),
            'charge_amount': str(fee.customer_pays.amount),
            'currency': wallet.currency,
            'public_key': wallet_settings.FLUTTERWAVE_PUBLIC_KEY,
            'fee_breakdown': fee.to_dict(),
        }

    # ------------------------------------------------------------------
    # Direct charges (card, bank_transfer, ussd, mpesa, mobile money, ...)
    # ------------------------------------------------------------------

    def direct_charge(self, wallet, amount, charge_type, payload=None, reference=None, email=None, metadata=None,
                      description=None, ip_address=None, user_agent=None):
        """
        Charge the customer directly through ``POST /charges?type=<charge_type>``.

        ``payload`` holds the type-specific fields (``phone_number``, ``account_bank``,
        card fields for ``card``...). The response tells you what happens next:

        * ``status='success'`` - paid and credited;
        * ``status='pending'`` + ``action`` - the customer must act (enter a PIN/OTP,
          open a redirect URL, dial a USSD code, transfer to the account shown...).
          For an OTP, call :meth:`validate_charge`; for an extra step, call again with the same
          ``reference`` and ``payload['authorization']``.

        Card payments are never accepted here (``CardDataNotAllowed``): cards are paid on hosted
        checkout only, so no card data ever reaches your server.
        """
        if str(charge_type).lower() in CARD_CHARGE_TYPES:
            raise CardDataNotAllowed()
        payload = dict(payload or {})
        txn = self._continuation(wallet, reference) if reference else None
        version = txn.api_version if txn is not None else self.api_version
        allowed = V4_CHARGE_TYPES if version == 'v4' else CHARGE_TYPES
        if charge_type not in allowed:
            raise InvalidAmount(_("Unsupported charge type {type} on Flutterwave {version}").format(
                type=charge_type, version=version))
        if txn is None:
            amount = self._prepare(wallet, amount, 'DIRECT_CHARGES')
            reference = self.new_reference(reference, lambda: generate_reference('CHG'))
            fee = get_fee_calculator(wallet, self.flutterwave).calculate(amount, 'deposit')
            txn = self._create_pending_deposit(
                wallet, amount, reference, fee, description, metadata,
                payment_method=payment_method_for(charge_type.replace('mobile_money_', 'mobilemoney')),
                ip_address=ip_address, user_agent=user_agent, extra_metadata={'charge_type': charge_type},
                api_version=version,
            )
        customer = self._customer(wallet, email)
        try:
            response = self.gateway(version).direct_charge(txn, charge_type, payload, customer,
                                                           self._flutterwave_meta(wallet, txn, metadata))
        except FlutterwaveAPIError as exc:
            if exc.is_definitive:
                if not txn.metadata.get('next_action'):
                    self._fail_initialization(txn, exc)
                raise
            self.flag_for_review(txn, f"Charge outcome unknown: {exc.message}")
            return self._charge_result(txn)
        return self._apply_charge_response(txn, response or {})

    def _continuation(self, wallet, reference):
        txn = Transaction.objects.filter(reference=reference, wallet=wallet,
                                         transaction_type=TRANSACTION_TYPE_DEPOSIT).first()
        if txn is None:
            return None
        if not txn.is_pending:
            raise InvalidTransactionState(_("This charge is already {status}").format(status=txn.status))
        return txn

    def _apply_charge_response(self, txn, response):
        data = response.get('data') if isinstance(response.get('data'), dict) else {}
        meta = response.get('meta') if isinstance(response.get('meta'), dict) else {}
        authorization = meta.get('authorization') if isinstance(meta.get('authorization'), dict) else {}
        status = charge_status(data)

        if data.get('id'):
            txn.flw_id = str(data['id'])
        if data.get('flw_ref'):
            txn.flw_ref = data['flw_ref']
        if status in SUCCESS_STATUSES and data.get('id'):
            txn.save(update_fields=['flw_id', 'flw_ref', 'updated_at'])
            return self._charge_result(self.verify_and_settle(data['id']) or txn)
        if status in FAILED_STATUSES:
            txn.save(update_fields=['flw_id', 'flw_ref', 'updated_at'])
            self._fail_pending(txn, data.get('processor_response') or response.get('message') or status, data)
            return self._charge_result(txn)

        action = None
        if authorization or data.get('auth_model') or data.get('processor_response'):
            action = {
                'mode': authorization.get('mode') or (data.get('auth_model') or '').lower() or 'otp',
                'fields': authorization.get('fields'),
                'redirect': authorization.get('redirect'),
                'instruction': (authorization.get('note') or authorization.get('validate_instructions')
                                or data.get('processor_response')),
            }
            for key in ('transfer_account', 'transfer_bank', 'transfer_amount', 'transfer_reference',
                        'account_expiration', 'transfer_note'):
                if authorization.get(key) is not None:
                    action[key] = authorization[key]
            action = {k: v for k, v in action.items() if v is not None}
        txn.metadata = {**txn.metadata, 'next_action': action, 'charge_message': response.get('message')}
        txn.save(update_fields=['flw_id', 'flw_ref', 'metadata', 'updated_at'])
        if action:
            send_on_commit(charge_requires_action, sender=Transaction, transaction=txn, wallet=txn.wallet,
                           action=action)
        return self._charge_result(txn)

    @staticmethod
    def _charge_result(txn):
        txn.refresh_from_db()
        if txn.is_successful:
            state = 'success'
        elif txn.is_pending:
            state = 'pending'
        else:
            state = 'failed'
        return {'status': state, 'reference': txn.reference, 'transaction_id': str(txn.pk),
                'action': (txn.metadata or {}).get('next_action') if txn.is_pending else None,
                'message': txn.failed_reason or (txn.metadata or {}).get('charge_message') or '',
                'transaction': txn}

    def validate_charge(self, transaction, otp):
        """Submit the OTP for a pending direct charge (``POST /validate-charge``)."""
        txn = Transaction.objects.select_related('wallet').get(pk=transaction.pk)
        if txn.transaction_type != TRANSACTION_TYPE_DEPOSIT or not txn.is_pending or not txn.flw_ref:
            raise InvalidTransactionState(_("This charge is not waiting for an OTP"))
        response = self.gateway(txn.api_version).validate_charge(txn, otp)
        return self._apply_charge_response(txn, response or {})

    # ------------------------------------------------------------------
    # Saved card (tokenized charge)
    # ------------------------------------------------------------------

    def charge_saved_card(self, card, amount, wallet=None, reference=None, metadata=None, description=None,
                          ip_address=None, user_agent=None):
        """Top up a wallet from a saved card. Returns the deposit Transaction."""
        from flutterwave_wallet.services.wallet_service import WalletService

        self.require_feature('CARDS')
        CardService.check_card(card)
        wallet = wallet or WalletService(**self.clients()).get_wallet(card.user)
        if wallet.user_id != card.user_id:
            raise InvalidTransactionState(_("This card belongs to another user"))
        amount = self._prepare(wallet, amount)
        reference = self.new_reference(reference, lambda: generate_reference('TOK'))
        fee = get_fee_calculator(wallet, self.flutterwave).calculate(amount, 'deposit')
        txn = self._create_pending_deposit(wallet, amount, reference, fee, description or str(_("Card top-up")),
                                           metadata, payment_method=PAYMENT_METHOD_CARD, card=card,
                                           ip_address=ip_address, user_agent=user_agent)
        user = wallet.user
        try:
            data = self.flutterwave.tokenized_charges.charge(
                token=card.token, email=card.email, amount=api_amount(fee.customer_pays.amount),
                currency=wallet.currency, tx_ref=reference, country=wallet_settings.COUNTRY,
                first_name=getattr(user, 'first_name', None) or None,
                last_name=getattr(user, 'last_name', None) or None,
                narration=description, ip=ip_address,
            )
        except FlutterwaveAPIError as exc:
            if exc.is_definitive:
                self._fail_initialization(txn, exc)
                raise
            # The card may have been charged: keep pending, reconciliation settles it.
            self.flag_for_review(txn, f"Card charge outcome unknown: {exc.message}")
            return txn
        card.last_used_at = timezone.now()
        card.save(update_fields=['last_used_at', 'updated_at'])
        data = data or {}
        if data.get('id') and charge_status(data) in SUCCESS_STATUSES | FAILED_STATUSES:
            return self.verify_and_settle(data['id']) or txn
        return txn

    # ------------------------------------------------------------------
    # Verification & settlement
    # ------------------------------------------------------------------

    def verify_and_settle(self, transaction_id, expected_reference=None, webhook_event=None):
        """Fetch the charge from Flutterwave (the source of truth: v3 or v4 by id) and settle it."""
        data = self.gateway_for_id(transaction_id).verify_charge(transaction_id) or {}
        if expected_reference and data.get('tx_ref') != expected_reference:
            logger.error("Verified charge %s has tx_ref %r, expected %r", transaction_id, data.get('tx_ref'),
                         expected_reference)
            return None
        return self.settle_charge(data, webhook_event=webhook_event)

    def verify_deposit(self, reference):
        """Ask Flutterwave about a deposit by our reference and settle it (idempotent)."""
        txn = Transaction.objects.filter(reference=reference).first()
        if txn is not None and txn.transaction_type != TRANSACTION_TYPE_DEPOSIT:
            raise InvalidTransactionState(_("Transaction {reference} is not a deposit").format(reference=reference))
        if txn is not None and txn.is_successful:
            return txn
        data = self.gateway(txn.api_version if txn is not None else None).verify_charge_by_reference(reference) or {}
        return self.settle_charge(data) or txn

    def settle_charge(self, data, webhook_event=None):
        """
        Settle a charge from **verified** Flutterwave data. Returns the deposit Transaction,
        or None when the charge does not belong to a wallet.
        """
        data = data or {}
        reference = data.get('tx_ref')
        if not reference and not data.get('customer_id'):
            return None
        status = charge_status(data)

        with db_transaction.atomic():
            txn = Transaction.objects.select_for_update().filter(reference=reference).first() if reference else None
            if txn is None:
                if status not in SUCCESS_STATUSES:
                    return None
                txn = self._deposit_for_virtual_account(data)
                if txn is None:
                    return None
            elif txn.transaction_type != TRANSACTION_TYPE_DEPOSIT:
                return None

            if webhook_event is not None and webhook_event.transaction_id is None:
                webhook_event.transaction = txn
                webhook_event.save(update_fields=['transaction', 'updated_at'])

            if txn.is_successful or txn.is_posted:
                return txn
            if status in SUCCESS_STATUSES:
                return self._complete_deposit(txn, data)
            if status in FAILED_STATUSES and txn.is_pending:
                self._fail_pending(txn, data.get('processor_response') or status, data)
            elif txn.is_pending:
                txn.flw_response = data
                txn.save(update_fields=['flw_response', 'updated_at'])
        return txn

    def _fail_pending(self, txn, reason, data=None):
        with db_transaction.atomic():
            txn = self.lock_transaction(txn)
            if not txn.is_pending:
                return txn
            if data:
                txn.flw_response = data
                txn.save(update_fields=['flw_response', 'updated_at'])
            self.set_status(txn, TRANSACTION_STATUS_FAILED, reason=reason)
            send_on_commit(deposit_failed, sender=Transaction, transaction=txn, wallet=txn.wallet, reason=str(reason))
        return txn

    def _complete_deposit(self, txn, data):
        """Credit the wallet for a verified successful charge. Runs under the deposit row lock."""
        wallet = txn.wallet
        currency = str(data.get('currency') or '').upper()
        paid = from_api(data.get('amount'))
        charged = from_api(data.get('charged_amount')) or paid
        app_fee = from_api(data.get('app_fee'))
        payment_type = str(data.get('payment_type') or '')

        txn.flw_id = str(data['id']) if data.get('id') else txn.flw_id
        txn.flw_ref = data.get('flw_ref') or txn.flw_ref
        txn.flw_response = data
        txn.channel = payment_type[:50]
        txn.payment_method = txn.payment_method or payment_method_for(payment_type)

        if currency != wallet.currency:
            reason = f"Currency mismatch: received {currency or '?'}, wallet is {wallet.currency}"
            txn.save()
            self.set_status(txn, TRANSACTION_STATUS_FAILED, reason=reason)
            self.flag_for_review(txn, reason)
            send_on_commit(deposit_failed, sender=Transaction, transaction=txn, wallet=wallet, reason=reason)
            return txn
        if paid <= 0:
            txn.save()
            self.flag_for_review(txn, "Flutterwave reported a successful charge with no amount")
            return txn

        calculator = get_fee_calculator(wallet, self.flutterwave)
        meta = txn.metadata or {}
        expected_charge = Decimal(meta['charge_amount']) if meta.get('charge_amount') else None
        tolerance = Decimal(str(wallet_settings.AMOUNT_TOLERANCE))
        review_reason = None

        if expected_charge is not None and abs(paid - expected_charge) <= tolerance:
            fee = FeeCalculationResult(txn.amount, txn.fees, txn.fee_bearer or FEE_BEARER_MERCHANT, 'deposit')
            credit = txn.total_amount.amount
        else:
            # Virtual-account transfers (any amount) or a checkout paid with a different amount:
            # price what actually arrived; the receiver absorbs the fee since nothing was added on top.
            bearer = calculator.resolve_bearer(None, 'deposit')
            bearer = FEE_BEARER_MERCHANT if bearer == FEE_BEARER_CUSTOMER else bearer
            try:
                fee = calculator.calculate(Money(paid, currency), 'deposit', bearer=bearer)
            except InvalidAmount:
                # The fee would swallow the whole deposit: credit it in full, let a human decide
                fee = FeeCalculationResult(Money(paid, currency), Money(ZERO, currency), bearer, 'deposit')
                review_reason = f"Deposit of {paid} {currency} is smaller than its fee; credited without a fee"
            credit = fee.merchant_receives.amount
            txn.amount = Money(paid, currency)
            if expected_charge is not None:
                review_reason = f"Expected {expected_charge} {currency}, Flutterwave collected {paid}"

        processing_fee = ZERO
        if wallet_settings.PROCESSING_FEE_BEARER == FEE_BEARER_CUSTOMER and app_fee > 0:
            paid_on_top = max(charged - paid, ZERO)
            processing_fee = min(max(app_fee - paid_on_top, ZERO), credit)
            credit -= processing_fee

        credit = quantize(max(credit, ZERO))
        txn.fees = Money(quantize(fee.platform_revenue.amount + processing_fee), currency)
        txn.total_amount = Money(credit, currency)
        txn.metadata = {**meta, 'paid_amount': str(paid), 'charged_amount': str(charged), 'app_fee': str(app_fee),
                        'processing_fee_deducted': str(processing_fee), 'next_action': None}

        locked_wallet = self.lock_wallet(wallet)
        self.post(locked_wallet, txn, force=True)
        txn.wallet = locked_wallet
        self.set_status(txn, TRANSACTION_STATUS_SUCCESS, reason='' if txn.failed_reason else None)
        self.record_fee(txn, fee)
        if review_reason:
            self.flag_for_review(txn, review_reason)
        if credit <= 0:
            self.flag_for_review(txn, "Fees consumed the whole deposit")

        if payment_type.lower() == 'card':
            card = CardService(**self.clients()).save_card_from_charge(wallet.user, data)
            if card is not None and txn.card_id is None:
                txn.card = card
                txn.save(update_fields=['card', 'updated_at'])

        send_on_commit(deposit_completed, sender=Transaction, transaction=txn, wallet=locked_wallet)
        return txn

    @staticmethod
    def _static_account_for(data):
        """
        The static virtual account a transfer arrived in: by tx_ref (v3), or - on v4, where
        each transfer is its own charge - by the v4 customer and currency.
        """
        accounts = VirtualAccount.objects.select_related('wallet').filter(kind=VIRTUAL_ACCOUNT_STATIC)
        if data.get('tx_ref'):
            account = accounts.filter(tx_ref=data['tx_ref']).first()
            if account is not None:
                return account
        if data.get('api_version') == 'v4' and data.get('customer_id') and \
                str(data.get('payment_type') or '').lower() in ('bank_transfer', 'banktransfer'):
            return accounts.filter(
                api_version='v4', is_active=True, wallet__user__wallet_profile__flw_customer_id=data['customer_id'],
                currency=str(data.get('currency') or '').upper(),
            ).first()
        return None

    def _deposit_for_virtual_account(self, data):
        """A transfer into a static virtual account: create its deposit row (idempotently)."""
        account = self._static_account_for(data)
        if account is None or not data.get('id'):
            logger.info("Charge %s (%s) does not belong to a wallet", data.get('id'), data.get('tx_ref'))
            return None
        if not wallet_settings.ENABLE_VIRTUAL_ACCOUNTS:
            logger.warning("Virtual account deposit %s received while virtual accounts are disabled", data.get('id'))
        wallet = account.wallet
        reference = f"VA-{data['id']}"
        paid = from_api(data.get('amount'))
        customer = data.get('customer') if isinstance(data.get('customer'), dict) else {}
        try:
            with db_transaction.atomic():
                return Transaction.objects.create(
                    wallet=wallet, amount=Money(paid, wallet.currency), total_amount=Money(paid, wallet.currency),
                    transaction_type=TRANSACTION_TYPE_DEPOSIT, direction=DIRECTION_CREDIT,
                    status=TRANSACTION_STATUS_PENDING, reference=reference, flw_id=str(data['id']),
                    api_version=account.api_version, payment_method=PAYMENT_METHOD_VIRTUAL_ACCOUNT,
                    description=str(_("Bank transfer to {account}").format(account=account.account_number)),
                    metadata={'virtual_account_id': str(account.pk), 'virtual_account_tx_ref': account.tx_ref,
                              'narration': data.get('narration'), 'payer_name': customer.get('name')},
                )
        except IntegrityError:
            # A duplicate webhook created it first
            return Transaction.objects.select_for_update().get(reference=reference)

    # ------------------------------------------------------------------
    # Cancellation & reconciliation
    # ------------------------------------------------------------------

    def cancel_deposit(self, txn, reason=None, performed_by=None):
        """Cancel a deposit nobody paid. No money moves (a late payment would still be credited)."""
        with db_transaction.atomic():
            txn = self.lock_transaction(txn)
            if txn.transaction_type != TRANSACTION_TYPE_DEPOSIT or not txn.is_pending:
                raise InvalidTransactionState(_("Only pending deposits can be cancelled"))
            self.set_status(txn, TRANSACTION_STATUS_CANCELLED, reason=reason or str(_("Cancelled")))
        self.audit('deposit.cancel', performed_by, transaction=txn.reference, reason=reason or '')
        return txn

    def reconcile_deposit(self, txn):
        """Verify one pending deposit with Flutterwave and settle it."""
        try:
            if txn.flw_id:
                return self.verify_and_settle(txn.flw_id, expected_reference=txn.reference) or txn
            return self.verify_deposit(txn.reference)
        except FlutterwaveAPIError as exc:
            age = timezone.now() - txn.created_at
            if exc.is_not_found and age > timedelta(hours=int(wallet_settings.ABANDON_DEPOSIT_AFTER_HOURS)):
                # Flutterwave never saw a payment for this reference: the checkout was abandoned.
                return self.cancel_deposit(txn, reason="Abandoned: no payment found on Flutterwave")
            raise
