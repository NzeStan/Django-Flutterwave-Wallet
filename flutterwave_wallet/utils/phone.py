"""
Phone number normalisation for phone-number based wallet transfers.

The built-in normaliser converts local formats to E.164 using
``FLW_WALLET_PHONE_DEFAULT_COUNTRY_CODE`` (234 by default)::

    08031234567       -> +2348031234567
    +234 803 123 4567 -> +2348031234567

Point ``FLW_WALLET_PHONE_NUMBER_NORMALIZER`` at your own callable (e.g. one built
on the ``phonenumbers`` library) for full international validation.
"""
import re

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.exceptions import InvalidPhoneNumber

_STRIP = re.compile(r'[\s\-().]')


def default_normalize_phone_number(value, country_code=None):
    if value is None:
        raise InvalidPhoneNumber()
    raw = _STRIP.sub('', str(value))
    if not raw:
        raise InvalidPhoneNumber()

    country_code = str(country_code or wallet_settings.PHONE_DEFAULT_COUNTRY_CODE).lstrip('+')
    if raw.startswith('+'):
        digits = raw[1:]
    elif raw.startswith('00'):
        digits = raw[2:]
    elif raw.startswith(country_code) and len(raw) > len(country_code) + 6:
        digits = raw
    elif raw.startswith('0'):
        digits = country_code + raw[1:]
    else:
        digits = country_code + raw

    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise InvalidPhoneNumber(f"'{value}' is not a valid phone number")
    return f'+{digits}'


def normalize_phone_number(value):
    normalizer = wallet_settings.import_from('PHONE_NUMBER_NORMALIZER')
    if normalizer is not None:
        normalized = normalizer(value)
        if not normalized:
            raise InvalidPhoneNumber()
        return normalized
    return default_normalize_phone_number(value)


def looks_like_phone_number(value):
    if not value:
        return False
    raw = _STRIP.sub('', str(value))
    if raw.startswith('+'):
        raw = raw[1:]
    return raw.isdigit() and 7 <= len(raw) <= 15


def mask_phone_number(value):
    """+2348031234567 -> +234803****567"""
    if not value or len(value) < 8:
        return value
    return f'{value[:7]}****{value[-3:]}'
