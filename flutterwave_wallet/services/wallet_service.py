"""
``WalletService`` - one entry point for everything a wallet can do::

    from flutterwave_wallet.services import WalletService

    service = WalletService()
    wallet = service.get_wallet(request.user)                 # default currency
    usd = service.get_wallet(request.user, 'USD')             # one wallet per currency
    checkout = service.initialize_checkout(wallet, '5000')    # -> checkout['link']
    service.transfer(wallet, '08031234567', '1500')           # by phone number
    service.withdraw(wallet, '2000', bank_account)
    service.pay_bill(wallet, 'BIL099', 'AT099', '08031234567', '500')
"""
import logging
import re

from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    CURRENCY_CODES,
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_ADJUSTMENT,
)
from flutterwave_wallet.exceptions import (
    InvalidPhoneNumber,
    InvalidPin,
    RecipientError,
    UnsupportedCurrency,
    WalletError,
)
from flutterwave_wallet.models import Transaction, Wallet, WalletProfile
from flutterwave_wallet.services.bank_account_service import BankAccountService
from flutterwave_wallet.services.bill_service import BillPaymentService
from flutterwave_wallet.services.card_service import CardService
from flutterwave_wallet.services.deposit_service import DepositService
from flutterwave_wallet.services.kyc_service import KYCService
from flutterwave_wallet.services.ledger import verify_wallet_ledger
from flutterwave_wallet.services.refund_service import RefundService
from flutterwave_wallet.services.transfer_service import TransferService
from flutterwave_wallet.services.virtual_account_service import VirtualAccountService
from flutterwave_wallet.services.withdrawal_service import WithdrawalService
from flutterwave_wallet.signals import send_on_commit, wallet_created, wallet_locked, wallet_unlocked
from flutterwave_wallet.utils.phone import normalize_phone_number
from flutterwave_wallet.utils.references import generate_reference, generate_wallet_tag, random_string

logger = logging.getLogger('flutterwave_wallet')

_TAG_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{2,29}$')
_PIN_RE = re.compile(r'^\d+$')
WEAK_PINS = {'0000', '1111', '1234', '4321', '2222', '3333', '4444', '5555', '6666', '7777', '8888', '9999',
             '000000', '111111', '123456', '654321', '121212', '123123'}


def _resolve_attr(obj, dotted):
    for part in dotted.split('.'):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


class WalletService(
    DepositService,
    WithdrawalService,
    TransferService,
    BillPaymentService,
    RefundService,
    VirtualAccountService,
    KYCService,
    BankAccountService,
    CardService,
):
    """Facade over all wallet operations."""

    # ------------------------------------------------------------------
    # Wallets
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_currency(currency):
        currency = str(currency or wallet_settings.CURRENCY).upper()
        allowed = {c.upper() for c in (wallet_settings.ALLOWED_CURRENCIES or [wallet_settings.CURRENCY])}
        if currency not in CURRENCY_CODES or currency not in allowed:
            raise UnsupportedCurrency(_("{currency} wallets are not available").format(currency=currency))
        return currency

    def get_wallet(self, user, currency=None):
        """Return the user's wallet in ``currency`` (default currency if omitted), creating it if needed."""
        currency = self.normalize_currency(currency)
        wallet = Wallet.objects.select_related('user').filter(user=user, balance_currency=currency).first()
        return wallet if wallet is not None else self.create_wallet(user, currency)

    def get_wallets(self, user):
        return Wallet.objects.filter(user=user).order_by('created_at')

    def create_wallet(self, user, currency=None):
        currency = self.normalize_currency(currency)
        self.get_profile(user)
        try:
            with db_transaction.atomic():
                wallet = Wallet.objects.create(user=user, balance=Money(0, currency))
        except IntegrityError:
            return Wallet.objects.select_related('user').get(user=user, balance_currency=currency)
        send_on_commit(wallet_created, sender=Wallet, wallet=wallet)
        return wallet

    def get_profile(self, user):
        profile = WalletProfile.objects.filter(user=user).first()
        if profile is not None:
            return profile
        try:
            with db_transaction.atomic():
                return WalletProfile.objects.create(
                    user=user, tag=self._unique_tag(generate_wallet_tag(user)),
                    phone_number=self._available_phone(self._phone_from_user(user)),
                )
        except IntegrityError:
            return WalletProfile.objects.get(user=user)

    @staticmethod
    def _unique_tag(base):
        base = re.sub(r'[^a-z0-9_.-]', '', str(base).lower())[:24] or 'wallet'
        if len(base) < 3:
            base = f"{base}{random_string(4).lower()}"
        candidate = base
        for _attempt in range(20):
            if not WalletProfile.objects.filter(tag__iexact=candidate).exists():
                return candidate
            candidate = f"{base}{random_string(4, '0123456789')}"
        return f"{base}{random_string(8).lower()}"

    @staticmethod
    def _phone_from_user(user):
        field = wallet_settings.USER_PHONE_FIELD
        return _resolve_attr(user, field) if field else None

    @staticmethod
    def _available_phone(phone_number):
        if not phone_number:
            return None
        try:
            normalized = normalize_phone_number(phone_number)
        except InvalidPhoneNumber:
            logger.warning("Ignoring invalid phone number on user profile")
            return None
        return None if WalletProfile.objects.filter(phone_number=normalized).exists() else normalized

    # ------------------------------------------------------------------
    # Identity (tag & phone used for receiving transfers) and PIN
    # ------------------------------------------------------------------

    def set_phone_number(self, user, phone_number):
        profile = self.get_profile(user)
        if not phone_number:
            profile.phone_number = None
        else:
            normalized = normalize_phone_number(phone_number)
            if WalletProfile.objects.filter(phone_number=normalized).exclude(pk=profile.pk).exists():
                raise RecipientError(_("This phone number is already linked to another account"))
            profile.phone_number = normalized
        profile.save(update_fields=['phone_number', 'updated_at'])
        return profile

    def set_tag(self, user, tag):
        tag = str(tag or '').strip().lstrip('@').lower()
        if not _TAG_RE.match(tag):
            raise RecipientError(_("Tags are 3-30 characters: letters, digits, '.', '-' or '_'"))
        profile = self.get_profile(user)
        if WalletProfile.objects.filter(tag__iexact=tag).exclude(pk=profile.pk).exists():
            raise RecipientError(_("This tag is already taken"))
        profile.tag = tag
        try:
            profile.save(update_fields=['tag', 'updated_at'])
        except IntegrityError as exc:
            raise RecipientError(_("This tag is already taken")) from exc
        return profile

    def set_pin(self, user, pin, current_pin=None):
        """Set or change the transaction PIN (changing requires the current PIN)."""
        profile = self.get_profile(user)
        pin = str(pin or '')
        if len(pin) != int(wallet_settings.PIN_LENGTH) or not _PIN_RE.match(pin):
            raise InvalidPin(_("The PIN must be {length} digits").format(length=wallet_settings.PIN_LENGTH))
        if pin in WEAK_PINS:
            raise InvalidPin(_("This PIN is too easy to guess"))
        if profile.has_pin:
            profile.verify_pin(current_pin)
        profile.set_pin(pin)
        self.audit('pin.set', user)
        return profile

    def verify_pin(self, user, pin):
        return self.get_profile(user).verify_pin(pin)

    # ------------------------------------------------------------------
    # Status (staff)
    # ------------------------------------------------------------------

    def lock_wallet_account(self, wallet, reason='', performed_by=None):
        wallet.lock(reason)
        self.audit('wallet.lock', performed_by, wallet=wallet.pk, reason=reason)
        send_on_commit(wallet_locked, sender=Wallet, wallet=wallet, reason=reason)
        return wallet

    def unlock_wallet_account(self, wallet, performed_by=None):
        wallet.unlock()
        self.audit('wallet.unlock', performed_by, wallet=wallet.pk)
        send_on_commit(wallet_unlocked, sender=Wallet, wallet=wallet)
        return wallet

    # ------------------------------------------------------------------
    # Manual adjustments (staff, audited) - bonuses, cash-back, corrections
    # ------------------------------------------------------------------

    def credit_wallet(self, wallet, amount, description=None, reference=None, metadata=None, performed_by=None):
        return self._adjust(wallet, amount, DIRECTION_CREDIT, description, reference, metadata, performed_by)

    def debit_wallet(self, wallet, amount, description=None, reference=None, metadata=None, performed_by=None):
        return self._adjust(wallet, amount, DIRECTION_DEBIT, description, reference, metadata, performed_by)

    def _adjust(self, wallet, amount, direction, description, reference, metadata, performed_by):
        amount = wallet.validate_amount(amount)
        reference = self.new_reference(reference, lambda: generate_reference('ADJ'))
        with db_transaction.atomic():
            locked = self.lock_wallet(wallet)
            txn = Transaction(
                wallet=locked, amount=amount, total_amount=amount, transaction_type=TRANSACTION_TYPE_ADJUSTMENT,
                direction=direction, status=TRANSACTION_STATUS_SUCCESS, completed_at=timezone.now(),
                reference=reference, description=description or str(_("Manual adjustment")),
                metadata={**(metadata or {}), **self.actor(performed_by)},
            )
            self.post(locked, txn, force=direction == DIRECTION_CREDIT, enforce_minimum_balance=False)
        wallet.balance = locked.balance
        self.audit(f'wallet.{direction}', performed_by, wallet=wallet.pk, amount=amount, reference=reference)
        return txn

    # ------------------------------------------------------------------
    # History & integrity
    # ------------------------------------------------------------------

    @staticmethod
    def get_transactions(wallet, transaction_type=None, status=None, direction=None, start_date=None,
                         end_date=None):
        queryset = Transaction.objects.filter(wallet=wallet).in_date_range(start_date, end_date)
        if transaction_type:
            queryset = queryset.filter(transaction_type=transaction_type)
        if status:
            queryset = queryset.filter(status=status)
        if direction:
            queryset = queryset.filter(direction=direction)
        return queryset.order_by('-created_at')

    @staticmethod
    def get_statement(wallet, start_date=None, end_date=None):
        """Posted ledger entries in the exact order they changed the balance."""
        return Transaction.objects.filter(wallet=wallet).posted().in_date_range(start_date, end_date).order_by(
            'ledger_sequence')

    @staticmethod
    def verify_ledger(wallet):
        """Return a list of integrity problems for ``wallet`` (empty when the books are clean)."""
        return verify_wallet_ledger(wallet)

    def get_balance(self, wallet):
        return wallet.refresh_balance()

    def flutterwave_balances(self):
        """Your Flutterwave account balances (compare against the sum of wallet balances)."""
        return self.gateway().balances()


__all__ = ['WalletService', 'WalletError']
