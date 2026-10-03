"""
Model fields.

``WalletMoneyField`` is django-money's ``MoneyField`` whose currency default
comes from ``FLW_WALLET_CURRENCY`` at runtime while migrations always serialise
a fixed value - so changing the setting never generates migrations inside this
package.

``EncryptedTextField`` stores values encrypted (Fernet) and decrypts on read.
"""
from django.db import models
from djmoney.models.fields import CurrencyField, MoneyField
from djmoney.money import Money

from flutterwave_wallet.constants import SUPPORTED_CURRENCIES

MIGRATION_CURRENCY = 'NGN'


def default_currency():
    from flutterwave_wallet.conf import wallet_settings
    return wallet_settings.CURRENCY


class WalletCurrencyField(CurrencyField):
    def deconstruct(self):
        name, _path, args, kwargs = super().deconstruct()
        kwargs['default'] = MIGRATION_CURRENCY
        kwargs.pop('price_field', None)
        return name, 'flutterwave_wallet.models.fields.WalletCurrencyField', args, kwargs

    def get_default(self):
        return default_currency()


class WalletMoneyField(MoneyField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 19)
        kwargs.setdefault('decimal_places', 2)
        kwargs.setdefault('default_currency', MIGRATION_CURRENCY)
        kwargs.setdefault('currency_choices', SUPPORTED_CURRENCIES)
        super().__init__(*args, **kwargs)

    def add_currency_field(self, cls, name):
        from djmoney.models.fields import get_currency_field_name

        currency_field = WalletCurrencyField(
            price_field=self,
            max_length=self.currency_max_length,
            default=self.default_currency,
            editable=False,
            choices=self.currency_choices,
            null=self.null,
        )
        currency_field.creation_counter = self.creation_counter - 1
        cls.add_to_class(get_currency_field_name(name, self), currency_field)
        self._currency_field = currency_field

    def get_default(self):
        default = super().get_default()
        if isinstance(default, Money):
            return Money(default.amount, default_currency())
        return default

    def deconstruct(self):
        name, _path, args, kwargs = super().deconstruct()
        kwargs['default_currency'] = MIGRATION_CURRENCY
        if isinstance(kwargs.get('default'), Money):
            kwargs['default'] = kwargs['default'].amount
        kwargs.pop('currency_choices', None)
        return name, 'flutterwave_wallet.models.fields.WalletMoneyField', args, kwargs


class EncryptedTextField(models.TextField):
    """Text encrypted at rest with Fernet (see :mod:`flutterwave_wallet.utils.crypto`)."""

    def from_db_value(self, value, expression, connection):
        from flutterwave_wallet.utils.crypto import decrypt_value
        return decrypt_value(value)

    def to_python(self, value):
        from flutterwave_wallet.utils.crypto import decrypt_value
        return decrypt_value(value)

    def get_prep_value(self, value):
        from flutterwave_wallet.utils.crypto import ENCRYPTED_PREFIX, encrypt_value
        value = super().get_prep_value(value)
        if value in (None, '') or str(value).startswith(ENCRYPTED_PREFIX):
            return value
        return encrypt_value(value)
