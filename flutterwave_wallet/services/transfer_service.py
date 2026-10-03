"""
Wallet-to-wallet transfers, wallet payments (with optional escrow) and staff reversals.

Recipients can be addressed by wallet ID, tag, phone number or email
(``FLW_WALLET_TRANSFER_RECIPIENT_LOOKUP_FIELDS`` controls which are allowed)::

    service.transfer(sender_wallet, '08031234567', '5000')          # auto-detected phone
    service.transfer(sender_wallet, '@ada', '5000')
    service.transfer(sender_wallet, recipient_wallet, '5000')

Every movement writes two ledger rows (a debit and a credit) inside one database
transaction with both wallets locked in primary-key order. No money leaves the
platform, so these never call Flutterwave.
"""
import logging
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    LOOKUP_EMAIL,
    LOOKUP_ID,
    LOOKUP_PHONE,
    LOOKUP_TAG,
    PAYMENT_METHOD_WALLET,
    TRANSACTION_STATUS_CANCELLED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_STATUS_REVERSED,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_PAYMENT,
    TRANSACTION_TYPE_REVERSAL,
    TRANSACTION_TYPE_TRANSFER,
)
from flutterwave_wallet.exceptions import (
    CurrencyMismatchError,
    InvalidPhoneNumber,
    InvalidTransactionState,
    RecipientError,
    RecipientNotFound,
    WalletError,
)
from flutterwave_wallet.models import Transaction, Wallet, WalletProfile
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.fee_service import get_fee_calculator
from flutterwave_wallet.services.fee_sweep_service import FeeService
from flutterwave_wallet.signals import (
    payment_cancelled,
    payment_completed,
    payment_released,
    send_on_commit,
    transaction_reversed,
    transfer_completed,
)
from flutterwave_wallet.utils.phone import looks_like_phone_number, mask_phone_number, normalize_phone_number
from flutterwave_wallet.utils.references import generate_reference

logger = logging.getLogger('flutterwave_wallet')


class TransferService(BaseService):

    # ------------------------------------------------------------------
    # Recipient resolution
    # ------------------------------------------------------------------

    @staticmethod
    def allowed_lookups():
        return list(wallet_settings.TRANSFER_RECIPIENT_LOOKUP_FIELDS or [])

    def resolve_recipient(self, identifier, currency, lookup=None):
        """
        Find the ``currency`` wallet identified by ``identifier``.

        ``lookup`` forces one method ('id', 'tag', 'phone_number', 'email'); otherwise it is
        auto-detected: UUID -> id, '@tag' -> tag, contains '@' -> email, digits -> phone, else tag.
        """
        if isinstance(identifier, Wallet):
            return identifier
        identifier = str(identifier or '').strip()
        if not identifier:
            raise RecipientNotFound()
        allowed = self.allowed_lookups()
        if lookup in ('phone', 'phone_number'):
            lookup = LOOKUP_PHONE
        if lookup:
            if lookup not in allowed:
                raise RecipientError(_("Finding recipients by {lookup} is not allowed").format(lookup=lookup))
            candidates = [lookup]
        else:
            candidates = [c for c in self._detect_lookups(identifier) if c in allowed]
        for method in candidates:
            wallet = self._lookup(method, identifier, str(currency).upper())
            if wallet is not None:
                return wallet
        raise RecipientNotFound()

    @staticmethod
    def _detect_lookups(identifier):
        try:
            uuid.UUID(identifier)
            return [LOOKUP_ID]
        except ValueError:
            pass
        if identifier.startswith('@'):
            return [LOOKUP_TAG]
        if '@' in identifier:
            return [LOOKUP_EMAIL]
        if looks_like_phone_number(identifier):
            return [LOOKUP_PHONE, LOOKUP_TAG]
        return [LOOKUP_TAG]

    @staticmethod
    def _wallet_of(user_id, currency):
        if user_id is None:
            return None
        from flutterwave_wallet.services.wallet_service import WalletService
        wallet = Wallet.objects.select_related('user').filter(user_id=user_id, balance_currency=currency).first()
        if wallet is None and currency in [c.upper() for c in wallet_settings.ALLOWED_CURRENCIES]:
            # The recipient has an account but no wallet in this currency yet: open one to receive.
            wallet = WalletService().get_wallet(get_user_model().objects.get(pk=user_id), currency)
        return wallet

    def _lookup(self, method, identifier, currency):
        if method == LOOKUP_ID:
            try:
                wallet = Wallet.objects.select_related('user').filter(pk=uuid.UUID(identifier)).first()
            except ValueError:
                return None
            if wallet is not None and wallet.currency != currency:
                raise CurrencyMismatchError(_("That wallet holds {currency}").format(currency=wallet.currency))
            return wallet
        if method == LOOKUP_TAG:
            user_id = WalletProfile.objects.filter(tag__iexact=identifier.lstrip('@')).values_list(
                'user_id', flat=True).first()
            return self._wallet_of(user_id, currency)
        if method == LOOKUP_PHONE:
            try:
                phone = normalize_phone_number(identifier)
            except InvalidPhoneNumber:
                return None
            user_id = WalletProfile.objects.filter(phone_number=phone).values_list('user_id', flat=True).first()
            return self._wallet_of(user_id, currency)
        if method == LOOKUP_EMAIL:
            users = list(get_user_model().objects.filter(email__iexact=identifier).values_list('pk', flat=True)[:2])
            return self._wallet_of(users[0], currency) if len(users) == 1 else None
        return None

    @staticmethod
    def describe_recipient(wallet):
        """Safe, masked details to show a sender before they confirm."""
        user = wallet.user
        full_name = user.get_full_name() if hasattr(user, 'get_full_name') else ''
        parts = (full_name or '').split()
        masked_name = ' '.join([parts[0]] + [f"{p[0]}." for p in parts[1:]]) if parts else ''
        profile = WalletProfile.objects.filter(user_id=wallet.user_id).first()
        return {
            'wallet_id': str(wallet.pk),
            'tag': profile.tag if profile else None,
            'name': masked_name or (profile.tag if profile else '') or '',
            'phone_number': mask_phone_number(profile.phone_number) if profile and profile.phone_number else None,
            'currency': wallet.currency,
            'can_receive': wallet.is_operational,
        }

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    def transfer(self, source_wallet, destination, amount, description=None, metadata=None, reference=None,
                 lookup=None, ip_address=None, user_agent=None):
        """Move money between two wallets. Returns the sender's (debit) Transaction."""
        self.require_feature('INTERNAL_TRANSFERS')
        recipient_wallet = self.resolve_recipient(destination, source_wallet.currency, lookup)
        if recipient_wallet.pk == source_wallet.pk or recipient_wallet.user_id == source_wallet.user_id:
            raise RecipientError(_("You cannot transfer to yourself"))
        if recipient_wallet.currency != source_wallet.currency:
            raise CurrencyMismatchError(_("Both wallets must use the same currency"))

        amount = source_wallet.validate_amount(amount)
        self.check_amount_limits(amount)
        reference = self.new_reference(reference, lambda: generate_reference('TRF'))
        fee = get_fee_calculator(source_wallet, self.flutterwave).calculate(amount, 'transfer')
        context = self.request_context(ip_address, user_agent)
        now = timezone.now()

        with db_transaction.atomic():
            sender, recipient = self.lock_wallets(source_wallet, recipient_wallet)
            sender.check_active()
            recipient.check_active()
            self.check_daily_limit(sender, fee.customer_pays.amount)
            debit = Transaction(
                wallet=sender, counterparty_wallet=recipient, amount=amount, fees=fee.fee_amount,
                total_amount=fee.customer_pays, fee_bearer=fee.bearer, transaction_type=TRANSACTION_TYPE_TRANSFER,
                direction=DIRECTION_DEBIT, status=TRANSACTION_STATUS_SUCCESS, completed_at=now, reference=reference,
                payment_method=PAYMENT_METHOD_WALLET, description=description or str(_("Transfer sent")),
                metadata={**(metadata or {}), 'fee': fee.to_dict()}, **context,
            )
            self.post(sender, debit)
            credit = Transaction(
                wallet=recipient, counterparty_wallet=sender, amount=amount, fees=fee.fee_amount,
                total_amount=fee.merchant_receives, fee_bearer=fee.bearer, transaction_type=TRANSACTION_TYPE_TRANSFER,
                direction=DIRECTION_CREDIT, status=TRANSACTION_STATUS_SUCCESS, completed_at=now,
                reference=f"{reference}-CR", payment_method=PAYMENT_METHOD_WALLET, related_transaction=debit,
                description=description or str(_("Transfer received")), metadata=dict(metadata or {}),
            )
            self.post(recipient, credit)
            debit.related_transaction = credit
            debit.save(update_fields=['related_transaction', 'updated_at'])
            self.record_fee(debit, fee)

        source_wallet.balance = sender.balance
        send_on_commit(transfer_completed, sender=Transaction, transaction=debit, wallet=sender,
                       recipient_transaction=credit, recipient_wallet=recipient)
        return debit

    # ------------------------------------------------------------------
    # Payments (checkout with wallet balance)
    # ------------------------------------------------------------------

    def pay(self, wallet, amount, merchant_wallet=None, description=None, reference=None, metadata=None,
            escrow=False, ip_address=None, user_agent=None):
        """
        Pay with wallet balance. Returns the buyer's (debit) Transaction.

        * ``merchant_wallet=None`` - the platform keeps the money (your own shop);
        * ``merchant_wallet=<Wallet>`` - a seller is credited (marketplace);
        * ``escrow=True`` - the buyer is debited now, the seller credited on
          :meth:`release_payment`; :meth:`cancel_payment` refunds the buyer.
        """
        self.require_feature('PAYMENTS')
        if merchant_wallet is not None and not isinstance(merchant_wallet, Wallet):
            merchant_wallet = self.resolve_recipient(merchant_wallet, wallet.currency)
        if merchant_wallet is not None and merchant_wallet.pk == wallet.pk:
            raise RecipientError(_("A wallet cannot pay itself"))
        if merchant_wallet is not None and merchant_wallet.currency != wallet.currency:
            raise CurrencyMismatchError(_("Both wallets must use the same currency"))
        if escrow and merchant_wallet is None:
            raise WalletError(_("Escrow payments need a merchant wallet"))

        amount = wallet.validate_amount(amount)
        self.check_amount_limits(amount)
        reference = self.new_reference(reference, lambda: generate_reference('PAY'))
        fee = get_fee_calculator(wallet, self.flutterwave).calculate(amount, 'payment')
        context = self.request_context(ip_address, user_agent)
        now = timezone.now()
        merchant_txn = None

        with db_transaction.atomic():
            locked = self.lock_wallets(*([wallet] + ([merchant_wallet] if merchant_wallet else [])))
            buyer, merchant = locked[0], (locked[1] if merchant_wallet else None)
            buyer.check_active()
            if merchant is not None:
                merchant.check_active()
            self.check_daily_limit(buyer, fee.customer_pays.amount)
            buyer_txn = Transaction(
                wallet=buyer, counterparty_wallet=merchant, amount=amount, fees=fee.fee_amount,
                total_amount=fee.customer_pays, fee_bearer=fee.bearer, transaction_type=TRANSACTION_TYPE_PAYMENT,
                direction=DIRECTION_DEBIT, status=TRANSACTION_STATUS_PENDING if escrow else TRANSACTION_STATUS_SUCCESS,
                completed_at=None if escrow else now, reference=reference, payment_method=PAYMENT_METHOD_WALLET,
                description=description or str(_("Payment")),
                metadata={**(metadata or {}), 'fee': fee.to_dict(), 'escrow': bool(escrow),
                          'merchant_amount': str(fee.merchant_receives.amount)},
                **context,
            )
            self.post(buyer, buyer_txn)
            if merchant is None:
                # The platform is the merchant: everything the buyer paid is platform revenue
                self.record_fee(buyer_txn, fee, amount=fee.customer_pays)
            elif not escrow:
                merchant_txn = self._credit_merchant(buyer_txn, merchant, fee.merchant_receives, now)
                self.record_fee(buyer_txn, fee)

        wallet.balance = buyer.balance
        send_on_commit(payment_completed, sender=Transaction, transaction=buyer_txn, wallet=buyer,
                       merchant_wallet=merchant, escrow=bool(escrow))
        if merchant_txn is not None:
            send_on_commit(payment_released, sender=Transaction, transaction=buyer_txn, wallet=buyer,
                           merchant_wallet=merchant, merchant_transaction=merchant_txn)
        return buyer_txn

    def release_payment(self, transaction, performed_by=None):
        """Release an escrowed payment to the merchant."""
        with db_transaction.atomic():
            txn = self.lock_transaction(transaction)
            self._check_escrow(txn)
            merchant = self.lock_wallet(txn.counterparty_wallet_id)
            merchant_amount = Money(txn.metadata.get('merchant_amount') or txn.amount.amount, txn.amount.currency)
            merchant_txn = self._credit_merchant(txn, merchant, merchant_amount, timezone.now())
            fee = (txn.metadata or {}).get('fee') or {}
            if fee:
                from flutterwave_wallet.services.fee_service import FeeCalculationResult
                self.record_fee(txn, FeeCalculationResult(txn.amount, Money(fee.get('fee') or 0, txn.amount.currency),
                                                          fee.get('bearer') or 'platform', 'payment'))
            txn.metadata = {**txn.metadata, **({'released_by': self.actor(performed_by)['performed_by']}
                                               if performed_by is not None else {})}
            txn.save(update_fields=['metadata', 'updated_at'])
            self.set_status(txn, TRANSACTION_STATUS_SUCCESS)
        self.audit('escrow.release', performed_by, transaction=txn.reference, amount=merchant_amount.amount)
        send_on_commit(payment_released, sender=Transaction, transaction=txn, wallet=txn.wallet,
                       merchant_wallet=merchant, merchant_transaction=merchant_txn)
        return txn

    def cancel_payment(self, transaction, reason=None, performed_by=None):
        """Cancel an escrowed payment and refund the buyer in full."""
        with db_transaction.atomic():
            txn = self.lock_transaction(transaction)
            self._check_escrow(txn)
            buyer = self.lock_wallet(txn.wallet_id)
            refund = Transaction(
                wallet=buyer, amount=txn.total_amount, total_amount=txn.total_amount,
                transaction_type=TRANSACTION_TYPE_REVERSAL, direction=DIRECTION_CREDIT,
                status=TRANSACTION_STATUS_SUCCESS, completed_at=timezone.now(), reference=f"{txn.reference}-CNL",
                related_transaction=txn, payment_method=PAYMENT_METHOD_WALLET,
                description=str(_("Refund of cancelled payment {reference}").format(reference=txn.reference)),
                metadata={'reason': reason or '', **self.actor(performed_by)},
            )
            self.post(buyer, refund, force=True)
            self.set_status(txn, TRANSACTION_STATUS_CANCELLED, reason=reason or str(_("Payment cancelled")))
        self.audit('escrow.cancel', performed_by, transaction=txn.reference, reason=reason or '')
        send_on_commit(payment_cancelled, sender=Transaction, transaction=txn, wallet=buyer)
        return txn

    @staticmethod
    def _check_escrow(txn):
        if not (txn.transaction_type == TRANSACTION_TYPE_PAYMENT and txn.is_debit and txn.is_pending
                and (txn.metadata or {}).get('escrow') and txn.counterparty_wallet_id):
            raise InvalidTransactionState(_("Only pending escrow payments can be released or cancelled"))

    def _credit_merchant(self, buyer_txn, merchant, amount, when):
        merchant_txn = Transaction(
            wallet=merchant, counterparty_wallet=buyer_txn.wallet, amount=buyer_txn.amount, fees=buyer_txn.fees,
            total_amount=amount, fee_bearer=buyer_txn.fee_bearer, transaction_type=TRANSACTION_TYPE_PAYMENT,
            direction=DIRECTION_CREDIT, status=TRANSACTION_STATUS_SUCCESS, completed_at=when,
            reference=f"{buyer_txn.reference}-CR", related_transaction=buyer_txn, payment_method=PAYMENT_METHOD_WALLET,
            description=buyer_txn.description,
            metadata={k: v for k, v in (buyer_txn.metadata or {}).items() if k != 'fee'},
        )
        self.post(merchant, merchant_txn, force=True)
        buyer_txn.related_transaction = merchant_txn
        buyer_txn.save(update_fields=['related_transaction', 'updated_at'])
        return merchant_txn

    # ------------------------------------------------------------------
    # Reversals of internal movements (staff)
    # ------------------------------------------------------------------

    def reverse_transaction(self, transaction, reason=None, performed_by=None):
        """
        Undo a successful transfer or payment: the receiver is debited and the payer credited.
        Raises InsufficientFunds if the receiver has already spent the money.
        """
        if not transaction.can_be_reversed():
            raise InvalidTransactionState(_("Only successful outgoing transfers and payments can be reversed"))
        reason = reason or str(_("Reversed"))

        with db_transaction.atomic():
            debit_leg = self.lock_transaction(transaction)
            if not debit_leg.can_be_reversed():
                raise InvalidTransactionState(_("Transaction already reversed"))
            credit_leg = debit_leg.related_transaction
            wallets = [debit_leg.wallet_id] + ([credit_leg.wallet_id] if credit_leg is not None else [])
            locked = dict(zip(wallets, self.lock_wallets(*wallets)))
            payer = locked[debit_leg.wallet_id]
            now = timezone.now()

            if credit_leg is not None and credit_leg.is_successful and credit_leg.is_credit:
                credit_leg = self.lock_transaction(credit_leg)
                receiver = locked[credit_leg.wallet_id]
                self.post(receiver, Transaction(
                    wallet=receiver, counterparty_wallet=payer, amount=credit_leg.total_amount,
                    total_amount=credit_leg.total_amount, transaction_type=TRANSACTION_TYPE_REVERSAL,
                    direction=DIRECTION_DEBIT, status=TRANSACTION_STATUS_SUCCESS, completed_at=now,
                    reference=f"{credit_leg.reference}-REV", related_transaction=credit_leg, description=reason,
                ), force=True, enforce_minimum_balance=False)
                self.set_status(credit_leg, TRANSACTION_STATUS_REVERSED, reason=reason)

            reversal = Transaction(
                wallet=payer, counterparty_wallet=debit_leg.counterparty_wallet, amount=debit_leg.total_amount,
                total_amount=debit_leg.total_amount, transaction_type=TRANSACTION_TYPE_REVERSAL,
                direction=DIRECTION_CREDIT, status=TRANSACTION_STATUS_SUCCESS, completed_at=now,
                reference=f"{debit_leg.reference}-REV", related_transaction=debit_leg, description=reason,
                metadata=self.actor(performed_by),
            )
            self.post(payer, reversal, force=True)
            FeeService.void_fees(debit_leg)
            self.set_status(debit_leg, TRANSACTION_STATUS_REVERSED, reason=reason)
        self.audit('transaction.reverse', performed_by, transaction=debit_leg.reference, reason=reason)
        send_on_commit(transaction_reversed, sender=Transaction, transaction=reversal, wallet=payer,
                       original_transaction=debit_leg)
        return reversal
