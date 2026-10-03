import json
from decimal import Decimal

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from flutterwave_wallet.conf import wallet_settings


class MoneyAmountField(serializers.Field):
    """Money -> ``"1234.50"`` (string, so clients never lose precision)."""

    def __init__(self, **kwargs):
        kwargs.setdefault('read_only', True)
        super().__init__(**kwargs)

    def to_representation(self, value):
        if value is None:
            return None
        return f"{Decimal(value.amount):.2f}"


def amount_field(**kwargs):
    """Positive amount with 2 decimals, max 17 integer digits. Accepts strings or numbers."""
    kwargs.setdefault('max_digits', 19)
    kwargs.setdefault('decimal_places', 2)
    kwargs.setdefault('min_value', Decimal('0.01'))
    return serializers.DecimalField(**kwargs)


class MetadataField(serializers.JSONField):
    """Free-form JSON object; limited in size and depth so it can't be used to bloat the DB."""

    max_bytes = 4096

    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError(_("metadata must be a JSON object"))
        if len(json.dumps(value, default=str)) > self.max_bytes:
            raise serializers.ValidationError(_("metadata is too large"))
        return value


class PinField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('write_only', True)
        kwargs.setdefault('max_length', 12)
        kwargs.setdefault('trim_whitespace', True)
        kwargs.setdefault('style', {'input_type': 'password'})
        super().__init__(**kwargs)


def validate_pin_format(value):
    length = int(wallet_settings.PIN_LENGTH)
    if not value or not str(value).isdigit() or len(str(value)) != length:
        raise serializers.ValidationError(_("The PIN must be {length} digits").format(length=length))
    return str(value)


class ReferenceField(serializers.RegexField):
    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        super().__init__(r'^[A-Za-z0-9_-]{8,80}$', max_length=80, **kwargs)
