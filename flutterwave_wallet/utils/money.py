"""
Money helpers.

Flutterwave amounts are in **major units** (``100`` means NGN 100.00), unlike
gateways that use kobo/cents. Everything is a :class:`~decimal.Decimal`; floats
never touch money.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from djmoney.money import Money

from flutterwave_wallet.exceptions import InvalidAmount

TWO_PLACES = Decimal('0.01')
ZERO = Decimal('0.00')


def to_decimal(value):
    """Convert int/str/Decimal/Money (or a JSON number) to a Decimal rounded to 2 places."""
    if isinstance(value, Money):
        value = value.amount
    if value is None or isinstance(value, bool):
        raise InvalidAmount("Amount is required")
    try:
        result = Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidAmount(f"Invalid amount: {value!r}") from exc
    if not result.is_finite():
        raise InvalidAmount(f"Invalid amount: {value!r}")
    return result


def quantize(value):
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_money(value, currency):
    if isinstance(value, Money):
        return Money(quantize(value.amount), value.currency)
    return Money(to_decimal(value), currency)


def api_amount(value):
    """Amount as sent to Flutterwave (a JSON number without float rounding surprises)."""
    amount = to_decimal(value)
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def from_api(value):
    """Parse an amount returned by Flutterwave (number or string); None/'' -> 0."""
    if value in (None, ''):
        return ZERO
    return to_decimal(value)


def money_to_dict(money):
    if money is None:
        return None
    return {'amount': str(quantize(money.amount)), 'currency': str(money.currency)}
