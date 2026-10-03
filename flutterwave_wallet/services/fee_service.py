"""
Fee calculation.

Pricing is pluggable:

* settings-based pricing via ``FLW_WALLET_FEE_RULES`` (percentage + flat, optional
  minimum and cap, per operation)
* withdrawals can use Flutterwave's own transfer fee quote (``"source": "flutterwave"``)
* your own calculator: subclass :class:`FeeCalculator`, override :meth:`get_fee`
  (and/or :meth:`get_default_bearer`) and point ``FLW_WALLET_FEE_CALCULATOR`` at it

Bearer semantics are the same for every operation:

============  ===========================================================
customer      the payer pays the fee on top of the amount
merchant      the receiver absorbs the fee (deducted from what they get)
platform      the platform absorbs the fee; nobody is charged
split         shared (FLW_WALLET_FEE_SPLIT_CUSTOMER_PERCENTAGE)
============  ===========================================================

For each operation the payer/receiver are: deposit (card/bank payer -> wallet),
withdrawal (wallet -> bank), transfer (sender -> recipient wallet), payment
(buyer -> merchant), bill_payment (wallet -> biller).
"""
import logging
from decimal import Decimal

from djmoney.money import Money

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    FEE_BEARER_CUSTOMER,
    FEE_BEARER_MERCHANT,
    FEE_BEARER_PLATFORM,
    FEE_BEARER_SPLIT,
    VALID_BEARERS,
)
from flutterwave_wallet.exceptions import InvalidAmount
from flutterwave_wallet.utils.money import ZERO, quantize, to_decimal

logger = logging.getLogger('flutterwave_wallet')

SOURCE_DISABLED = 'disabled'
SOURCE_SETTINGS = 'settings'
SOURCE_FLUTTERWAVE = 'flutterwave'
SOURCE_CUSTOM = 'custom'

OPERATIONS = ('deposit', 'withdrawal', 'transfer', 'payment', 'bill_payment')


def _dec(value, default='0'):
    return Decimal(str(value if value not in (None, '') else default))


class FeeCalculationResult:
    """
    Outcome of a fee calculation.

    ``customer_pays`` leaves the payer; ``merchant_receives`` reaches the receiver;
    ``platform_revenue`` is what the platform keeps (fee charged to someone).
    """

    def __init__(self, original_amount, fee_amount, bearer, operation, source=SOURCE_SETTINGS, details=None):
        currency = original_amount.currency
        self.original_amount = original_amount
        self.fee_amount = Money(quantize(fee_amount.amount), currency)
        self.bearer = bearer
        self.operation = operation
        self.source = source
        self.details = details or {}

        fee = self.fee_amount.amount
        customer_fee = merchant_fee = ZERO
        if bearer == FEE_BEARER_CUSTOMER:
            customer_fee = fee
        elif bearer == FEE_BEARER_MERCHANT:
            merchant_fee = fee
        elif bearer == FEE_BEARER_SPLIT:
            customer_fee = quantize(fee * _dec(wallet_settings.FEE_SPLIT_CUSTOMER_PERCENTAGE) / 100)
            merchant_fee = fee - customer_fee

        self.customer_fee = Money(customer_fee, currency)
        self.merchant_fee = Money(merchant_fee, currency)
        self.customer_pays = original_amount + self.customer_fee
        self.merchant_receives = original_amount - self.merchant_fee
        self.platform_revenue = Money(customer_fee + merchant_fee, currency)

        if self.merchant_receives.amount <= 0:
            raise InvalidAmount(f"Amount {original_amount} is too small to cover the fee of {self.fee_amount}")

    @property
    def has_fee(self):
        return self.fee_amount.amount > 0

    def to_dict(self):
        return {
            'operation': self.operation,
            'amount': str(self.original_amount.amount),
            'currency': str(self.original_amount.currency),
            'fee': str(self.fee_amount.amount),
            'bearer': self.bearer,
            'customer_fee': str(self.customer_fee.amount),
            'merchant_fee': str(self.merchant_fee.amount),
            'customer_pays': str(self.customer_pays.amount),
            'merchant_receives': str(self.merchant_receives.amount),
            'source': self.source,
        }

    def __repr__(self):
        return f"<FeeCalculationResult {self.operation} amount={self.original_amount} fee={self.fee_amount} " \
               f"bearer={self.bearer}>"


class FeeCalculator:
    """Default calculator. Subclass and override :meth:`get_fee` for custom pricing."""

    def __init__(self, wallet=None, flutterwave=None):
        self.wallet = wallet
        self._flutterwave = flutterwave

    def calculate(self, amount, operation, bearer=None, context=None):
        """``amount`` is a Money; ``context`` carries extras (e.g. ``{'transfer_type': 'mobilemoney'}``)."""
        if operation not in OPERATIONS:
            raise InvalidAmount(f"Unknown fee operation {operation!r}")
        currency = amount.currency
        rule = self.rule(operation)
        chosen_bearer = self.resolve_bearer(bearer, operation, rule)
        if not self.is_enabled(operation):
            return FeeCalculationResult(amount, Money(ZERO, currency), chosen_bearer, operation, SOURCE_DISABLED)
        fee, source, details = self.get_fee(amount.amount, str(currency), operation, rule, context or {})
        return FeeCalculationResult(amount, Money(quantize(max(fee, ZERO)), currency), chosen_bearer, operation,
                                    source, details)

    # ------------------------------------------------------------------
    # Extension points
    # ------------------------------------------------------------------

    def is_enabled(self, operation):
        return bool(wallet_settings.ENABLE_FEES)

    def rule(self, operation):
        return dict((wallet_settings.FEE_RULES or {}).get(operation) or {})

    def get_fee(self, amount, currency, operation, rule, context):
        """Return ``(fee: Decimal, source: str, details: dict)``. Override for custom pricing."""
        if operation == 'withdrawal' and rule.get('source') == SOURCE_FLUTTERWAVE:
            return self.flutterwave_transfer_fee(amount, currency, context), SOURCE_FLUTTERWAVE, {}
        return self.rule_fee(amount, rule), SOURCE_SETTINGS, {}

    def get_default_bearer(self, operation, rule=None):
        rule = rule if rule is not None else self.rule(operation)
        return rule.get('bearer') or FEE_BEARER_PLATFORM

    def resolve_bearer(self, bearer, operation, rule=None):
        # ``bearer`` comes from your own code only - the API never lets end users choose it
        chosen = bearer or self.get_default_bearer(operation, rule)
        if chosen not in VALID_BEARERS:
            raise InvalidAmount(f"Unknown fee bearer {chosen!r}")
        return chosen

    # ------------------------------------------------------------------
    # Built-in pricing
    # ------------------------------------------------------------------

    @staticmethod
    def rule_fee(amount, rule):
        fee = amount * _dec(rule.get('percentage')) / 100 + _dec(rule.get('flat'))
        minimum = rule.get('minimum')
        if minimum not in (None, '') and fee < _dec(minimum):
            fee = _dec(minimum)
        cap = rule.get('cap')
        if cap not in (None, '', 0, '0') and fee > _dec(cap):
            fee = _dec(cap)
        return quantize(max(fee, ZERO))

    def flutterwave_transfer_fee(self, amount, currency, context):
        """Ask Flutterwave what the payout will cost (v3 ``GET /transfers/fee``)."""
        from flutterwave_wallet.gateways import V3, get_gateway
        if str(wallet_settings.API_VERSION).lower() != V3:
            return get_gateway().transfer_fee(amount, currency)  # raises: v4 has no fee quote
        return quantize(get_gateway(V3, v3_client=self._flutterwave).transfer_fee(
            amount, currency, context.get('transfer_type') or 'account'))

def get_fee_calculator(wallet=None, flutterwave=None):
    calculator_class = wallet_settings.import_from('FEE_CALCULATOR') or FeeCalculator
    return calculator_class(wallet=wallet, flutterwave=flutterwave)


def calculate_fee(amount, operation, currency=None, bearer=None, wallet=None):
    if not isinstance(amount, Money):
        amount = Money(to_decimal(amount), currency or wallet_settings.CURRENCY)
    return get_fee_calculator(wallet).calculate(amount, operation, bearer=bearer)
