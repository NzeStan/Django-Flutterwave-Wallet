"""
Exceptions raised by the package.

Every exception derives from :class:`WalletError` and carries an HTTP
``status_code`` and a machine-readable ``code`` so API layers (ours or yours)
can turn them into consistent responses.
"""
from django.utils.translation import gettext_lazy as _


class WalletError(Exception):
    """Base exception for all wallet errors."""

    status_code = 400
    code = 'wallet_error'
    default_message = _("A wallet error occurred")

    def __init__(self, message=None, **extra):
        self.message = str(message) if message is not None else str(self.default_message)
        self.extra = extra
        super().__init__(self.message)

    @property
    def http_status(self):
        return type(self).status_code

    def as_dict(self):
        payload = {'detail': self.message, 'code': self.code}
        if self.extra:
            payload.update({k: str(v) if v is not None else None for k, v in self.extra.items()})
        return payload


class InsufficientFunds(WalletError):
    code = 'insufficient_funds'
    default_message = _("Insufficient funds in wallet")

    def __init__(self, wallet=None, amount=None, message=None):
        if message is None and wallet is not None and amount is not None:
            message = _("Insufficient funds. Available balance: {balance}, required: {amount}").format(
                balance=getattr(wallet, 'balance', None), amount=amount,
            )
        super().__init__(message)


class WalletLocked(WalletError):
    status_code = 403
    code = 'wallet_locked'
    default_message = _("Wallet is locked")


class WalletInactive(WalletLocked):
    code = 'wallet_inactive'
    default_message = _("Wallet is inactive")


class WalletNotFound(WalletError):
    status_code = 404
    code = 'wallet_not_found'
    default_message = _("Wallet not found")


class CurrencyMismatchError(WalletError):
    code = 'currency_mismatch'
    default_message = _("Currency mismatch")


class UnsupportedCurrency(WalletError):
    code = 'unsupported_currency'
    default_message = _("This currency is not enabled on this platform")


class InvalidAmount(WalletError):
    code = 'invalid_amount'
    default_message = _("Invalid amount")


class InvalidReference(WalletError):
    code = 'invalid_reference'
    default_message = _("Invalid reference")


class TransactionLimitExceeded(WalletError):
    code = 'limit_exceeded'
    default_message = _("Transaction limit exceeded")


class MinimumBalanceViolation(WalletError):
    code = 'minimum_balance'
    default_message = _("This transaction would take the wallet below its minimum balance")


class InvalidTransactionState(WalletError):
    status_code = 409
    code = 'invalid_transaction_state'
    default_message = _("The transaction cannot be changed in its current state")


class DuplicateReference(WalletError):
    status_code = 409
    code = 'duplicate_reference'
    default_message = _("A transaction with this reference already exists")


class FeatureDisabled(WalletError):
    status_code = 403
    code = 'feature_disabled'
    default_message = _("This feature is disabled")


class RecipientNotFound(WalletError):
    status_code = 404
    code = 'recipient_not_found'
    default_message = _("Recipient wallet not found")


class RecipientError(WalletError):
    code = 'recipient_error'
    default_message = _("Recipient error")


class InvalidPhoneNumber(WalletError):
    code = 'invalid_phone_number'
    default_message = _("Invalid phone number")


class PinError(WalletError):
    code = 'pin_error'
    default_message = _("Transaction PIN error")


class PinRequired(PinError):
    code = 'pin_required'
    default_message = _("Transaction PIN is required")


class PinNotSet(PinError):
    code = 'pin_not_set'
    default_message = _("Set a transaction PIN first")


class InvalidPin(PinError):
    status_code = 403
    code = 'invalid_pin'
    default_message = _("Incorrect transaction PIN")


class PinLocked(PinError):
    status_code = 403
    code = 'pin_locked'
    default_message = _("Too many incorrect PIN attempts. Try again later")


class CardError(WalletError):
    code = 'card_error'
    default_message = _("Card error")


class CardDataNotAllowed(WalletError):
    status_code = 400
    code = 'card_data_not_allowed'
    default_message = _(
        "Card details must never reach this server. Use hosted checkout for card payments, "
        "or charge a saved card by its token"
    )


class BankAccountError(WalletError):
    code = 'bank_account_error'
    default_message = _("Bank account error")


class VirtualAccountError(WalletError):
    code = 'virtual_account_error'
    default_message = _("Virtual account error")


class KYCError(WalletError):
    code = 'kyc_error'
    default_message = _("Identity verification error")


class LedgerIntegrityError(WalletError):
    status_code = 500
    code = 'ledger_integrity_error'
    default_message = _("Ledger integrity check failed")


class FlutterwaveAPIError(WalletError):
    """
    A Flutterwave API call failed.

    ``status_code`` is Flutterwave's HTTP status (None for network errors).
    ``is_definitive`` is True when Flutterwave rejected the request (4xx other than
    408/409/425/429): the operation definitely did not happen. For network errors,
    timeouts and 5xx responses the outcome is unknown and must be reconciled.
    """

    code = 'flutterwave_error'
    default_message = _("Flutterwave API error")

    _UNCERTAIN_4XX = {408, 409, 425, 429}

    def __init__(self, message=None, status_code=None, response=None):
        text = _("Flutterwave API error: {message}").format(message=message) if message else None
        super().__init__(text)
        self.flutterwave_message = str(message) if message else ''
        self.status_code = status_code
        self.response = response

    @property
    def is_definitive(self):
        return (self.status_code is not None and 400 <= self.status_code < 500
                and self.status_code not in self._UNCERTAIN_4XX)

    _NOT_FOUND_HINTS = ('not found', 'no transaction', 'does not exist', 'no record', 'was not found')

    @property
    def is_not_found(self):
        message = (self.flutterwave_message or '').lower()
        return self.status_code == 404 or (self.is_definitive and any(h in message for h in self._NOT_FOUND_HINTS))

    @property
    def http_status(self):
        """400 when Flutterwave rejected the request; 502 when it was unreachable or failed."""
        return 400 if self.is_definitive else 502

    @property
    def api_code(self):
        return 'flutterwave_rejected' if self.is_definitive else self.code

    def as_dict(self):
        return {'detail': self.message, 'code': self.api_code}


class InvalidFlutterwaveResponse(FlutterwaveAPIError):
    """Flutterwave returned something that is not JSON (e.g. a gateway error page)."""

    code = 'invalid_flutterwave_response'
    default_message = _("Invalid response from Flutterwave")

    @property
    def is_definitive(self):
        return False


class FlutterwaveConfigurationError(WalletError):
    status_code = 500
    code = 'flutterwave_not_configured'
    default_message = _("Flutterwave is not configured. Set FLUTTERWAVE_SECRET_KEY.")


class InvalidWebhookSignature(WalletError):
    status_code = 401
    code = 'invalid_signature'
    default_message = _("Invalid webhook signature")


class IdempotencyKeyRequired(WalletError):
    code = 'idempotency_key_required'
    default_message = _("An Idempotency-Key header is required for this request")


class IdempotencyKeyMismatch(WalletError):
    status_code = 422
    code = 'idempotency_key_reused'
    default_message = _("This Idempotency-Key was already used with a different request")


class IdempotencyInProgress(WalletError):
    status_code = 409
    code = 'idempotency_in_progress'
    default_message = _(
        "A request with this Idempotency-Key is still being processed (or was interrupted). "
        "Check the transaction status before retrying with a new key"
    )
