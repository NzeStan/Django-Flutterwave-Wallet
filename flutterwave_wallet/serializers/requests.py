"""Input serializers (what the API accepts). Every field is bounded and validated."""
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from flutterwave_wallet.constants import (
    BENEFICIARY_TYPES,
    CHARGE_TYPES,
    CHECKOUT_PAYMENT_OPTIONS,
    CURRENCY_CODES,
    RECIPIENT_LOOKUP_TYPES,
    TRANSACTION_TYPES,
)
from flutterwave_wallet.gateways import V4_CHARGE_TYPES
from flutterwave_wallet.serializers.fields import (
    MetadataField,
    PinField,
    ReferenceField,
    amount_field,
    validate_pin_format,
)


class CurrencyField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('max_length', 3)
        kwargs.setdefault('min_length', 3)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data).upper()
        if value not in CURRENCY_CODES:
            raise serializers.ValidationError(_("Unsupported currency"))
        return value


class OpenWalletSerializer(serializers.Serializer):
    currency = CurrencyField(required=True)


class ProfileUpdateSerializer(serializers.Serializer):
    tag = serializers.CharField(required=False, max_length=30)
    phone_number = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=20)


class CheckoutSerializer(serializers.Serializer):
    amount = amount_field()
    email = serializers.EmailField(required=False)
    redirect_url = serializers.URLField(required=False, max_length=500)
    reference = ReferenceField()
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    payment_options = serializers.ListField(child=serializers.ChoiceField(choices=CHECKOUT_PAYMENT_OPTIONS),
                                            required=False, allow_empty=False, max_length=20)
    metadata = MetadataField()

    def validate_redirect_url(self, value):
        if not value.startswith('https://'):
            from django.conf import settings
            if not getattr(settings, 'DEBUG', False):
                raise serializers.ValidationError(_("redirect_url must use https"))
        return value


class DirectChargeSerializer(serializers.Serializer):
    amount = amount_field(required=False)
    # v3 charge types and v4 payment-method types; the service enforces the configured version's list.
    # Card-based types are not accepted at all: cards are paid on hosted checkout only.
    charge_type = serializers.ChoiceField(choices=sorted(set(CHARGE_TYPES) | set(V4_CHARGE_TYPES)))
    payload = serializers.DictField(required=False, help_text=_('Type-specific fields (phone_number, '
                                                                'account_bank, card fields, authorization...)'))
    reference = ReferenceField(help_text=_('Send the reference of a pending charge to continue it (PIN/AVS step)'))
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    metadata = MetadataField()

    def validate(self, attrs):
        if not attrs.get('reference') and attrs.get('amount') is None:
            raise serializers.ValidationError({'amount': _("This field is required.")})
        payload = attrs.get('payload') or {}
        if len(str(payload)) > 4096:
            raise serializers.ValidationError({'payload': _("payload is too large")})
        # The wallet decides amount, currency, tx_ref and email; the client can't override them
        attrs['payload'] = {k: v for k, v in payload.items()
                            if k not in ('amount', 'currency', 'tx_ref', 'meta', 'redirect_url')}
        return attrs


class ValidateChargeSerializer(serializers.Serializer):
    reference = ReferenceField(required=True)
    otp = serializers.CharField(max_length=12)


class BankTransferDepositSerializer(serializers.Serializer):
    amount = amount_field()
    reference = ReferenceField()
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    metadata = MetadataField()


class VerifyDepositSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=100)


class ChargeCardSerializer(serializers.Serializer):
    card_id = serializers.UUIDField(required=False, help_text=_('Defaults to the default card'))
    amount = amount_field()
    reference = ReferenceField()
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    metadata = MetadataField()
    pin = PinField()


class WithdrawSerializer(serializers.Serializer):
    amount = amount_field()
    bank_account_id = serializers.UUIDField(required=False, help_text=_('Defaults to the default payout account'))
    narration = serializers.CharField(required=False, allow_blank=True, max_length=100)
    reference = ReferenceField()
    metadata = MetadataField()
    pin = PinField()


class TransferSerializer(serializers.Serializer):
    recipient = serializers.CharField(max_length=255, help_text=_('Wallet ID, @tag, phone number or email'))
    recipient_type = serializers.ChoiceField(choices=RECIPIENT_LOOKUP_TYPES, required=False)
    amount = amount_field()
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    reference = ReferenceField()
    metadata = MetadataField()
    pin = PinField()


class PaySerializer(serializers.Serializer):
    amount = amount_field()
    merchant = serializers.CharField(required=False, allow_blank=True, max_length=255,
                                     help_text=_('Merchant wallet ID / tag / phone / email (empty = the platform)'))
    escrow = serializers.BooleanField(required=False, default=False)
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    reference = ReferenceField()
    metadata = MetadataField()
    pin = PinField()


class BillPaymentSerializer(serializers.Serializer):
    biller_code = serializers.CharField(max_length=50)
    item_code = serializers.CharField(max_length=50)
    customer_id = serializers.CharField(max_length=100)
    amount = amount_field()
    reference = ReferenceField()
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    metadata = MetadataField()
    pin = PinField()


class BillValidateSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    customer_id = serializers.CharField(max_length=100)


class RecipientLookupSerializer(serializers.Serializer):
    recipient = serializers.CharField(max_length=255)
    recipient_type = serializers.ChoiceField(choices=RECIPIENT_LOOKUP_TYPES, required=False)


class SetPinSerializer(serializers.Serializer):
    pin = PinField(required=True)
    current_pin = PinField()

    def validate_pin(self, value):
        return validate_pin_format(value)


class VirtualAccountRequestSerializer(serializers.Serializer):
    bvn = serializers.RegexField(r'^\d{11}$', required=False, write_only=True)
    nin = serializers.RegexField(r'^\d{11}$', required=False, write_only=True)


class BVNVerificationSerializer(serializers.Serializer):
    bvn = serializers.RegexField(r'^\d{11}$', write_only=True)
    firstname = serializers.CharField(max_length=100)
    lastname = serializers.CharField(max_length=100)
    redirect_url = serializers.URLField(required=False, max_length=500)


class BankAccountCreateSerializer(serializers.Serializer):
    beneficiary_type = serializers.ChoiceField(choices=BENEFICIARY_TYPES, required=False, default='bank')
    account_bank = serializers.CharField(max_length=30)
    account_number = serializers.CharField(max_length=30)
    account_name = serializers.CharField(required=False, allow_blank=True, max_length=255,
                                         help_text=_('Ignored where Flutterwave can resolve the name'))
    currency = CurrencyField()
    country = serializers.CharField(required=False, max_length=2, min_length=2)
    branch_code = serializers.CharField(required=False, allow_blank=True, max_length=50)
    is_default = serializers.BooleanField(required=False)
    pin = PinField()


class ResolveAccountSerializer(serializers.Serializer):
    account_bank = serializers.CharField(max_length=30)
    account_number = serializers.RegexField(r'^\d{6,20}$')


class FeeQuoteSerializer(serializers.Serializer):
    amount = amount_field()
    operation = serializers.ChoiceField(choices=['deposit', 'withdrawal', 'transfer', 'payment', 'bill_payment'])
    currency = CurrencyField()


class RefundSerializer(serializers.Serializer):
    amount = amount_field(required=False)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)


class TransactionFilterSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TRANSACTION_TYPES, required=False)
    status = serializers.CharField(required=False, max_length=20)
    direction = serializers.ChoiceField(choices=['credit', 'debit'], required=False)
