"""Read serializers for the models (what the API returns)."""
from rest_framework import serializers

from flutterwave_wallet.models import (
    Bank,
    BankAccount,
    Card,
    Transaction,
    VirtualAccount,
    Wallet,
    WalletProfile,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)
from flutterwave_wallet.serializers.fields import MoneyAmountField

# Metadata keys that are internal bookkeeping and never shown to end users
_PRIVATE_METADATA = {'fee', 'performed_by', 'review_reason', 'checkout_link'}


class WalletSerializer(serializers.ModelSerializer):
    balance = MoneyAmountField()
    currency = serializers.CharField(read_only=True)
    is_operational = serializers.BooleanField(read_only=True)
    virtual_account = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ['id', 'currency', 'balance', 'is_active', 'is_locked', 'is_operational', 'virtual_account',
                  'last_transaction_date', 'created_at', 'updated_at']
        read_only_fields = fields

    def get_virtual_account(self, obj):
        account = next((a for a in obj.virtual_accounts.all() if a.kind == 'static' and a.is_active), None) \
            if hasattr(obj, '_prefetched_objects_cache') else \
            obj.virtual_accounts.filter(kind='static', is_active=True).first()
        return VirtualAccountSerializer(account).data if account else None


class WalletDetailSerializer(WalletSerializer):
    daily_limit = serializers.SerializerMethodField()
    daily_spent = serializers.SerializerMethodField()

    class Meta(WalletSerializer.Meta):
        fields = WalletSerializer.Meta.fields + ['daily_limit', 'daily_spent']
        read_only_fields = fields

    def get_daily_limit(self, obj):
        limit = obj.get_daily_limit()
        return f"{limit:.2f}" if limit is not None else None

    def get_daily_spent(self, obj):
        return f"{obj.get_daily_outgoing_total():.2f}"


class ProfileSerializer(serializers.ModelSerializer):
    has_pin = serializers.BooleanField(read_only=True)

    class Meta:
        model = WalletProfile
        fields = ['tag', 'phone_number', 'has_pin', 'kyc_status', 'kyc_verified_at']
        read_only_fields = fields


class TransactionSerializer(serializers.ModelSerializer):
    amount = MoneyAmountField()
    fees = MoneyAmountField()
    total_amount = MoneyAmountField()
    balance_after = MoneyAmountField()
    currency = serializers.SerializerMethodField()
    counterparty = serializers.SerializerMethodField()
    metadata = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = ['id', 'reference', 'transaction_type', 'direction', 'status', 'amount', 'fees', 'total_amount',
                  'balance_after', 'currency', 'payment_method', 'description', 'counterparty', 'metadata',
                  'failed_reason', 'created_at', 'completed_at']
        read_only_fields = fields

    def get_currency(self, obj):
        return str(obj.amount.currency)

    def get_counterparty(self, obj):
        wallet = obj.counterparty_wallet
        if wallet is None:
            return None
        profile = getattr(wallet.user, 'wallet_profile', None)
        return {'wallet_id': str(wallet.pk), 'tag': getattr(profile, 'tag', None)}

    def get_metadata(self, obj):
        return {k: v for k, v in (obj.metadata or {}).items() if k not in _PRIVATE_METADATA}


class TransactionDetailSerializer(TransactionSerializer):
    related_transaction = serializers.PrimaryKeyRelatedField(read_only=True)
    bank_account = serializers.SerializerMethodField()
    card = serializers.SerializerMethodField()
    fee_breakdown = serializers.SerializerMethodField()

    class Meta(TransactionSerializer.Meta):
        fields = TransactionSerializer.Meta.fields + ['fee_bearer', 'flw_ref', 'related_transaction',
                                                      'bank_account', 'card', 'fee_breakdown', 'ledger_sequence']
        read_only_fields = fields

    def get_bank_account(self, obj):
        account = obj.bank_account
        if account is None:
            return None
        return {'id': str(account.pk), 'bank_name': account.bank_name, 'account_name': account.account_name,
                'account_number': account.masked_account_number}

    def get_card(self, obj):
        return {'id': str(obj.card.pk), 'masked': obj.card.masked} if obj.card_id else None

    def get_fee_breakdown(self, obj):
        return (obj.metadata or {}).get('fee')


class StaffTransactionSerializer(TransactionDetailSerializer):
    wallet = serializers.PrimaryKeyRelatedField(read_only=True)
    raw_metadata = serializers.JSONField(source='metadata', read_only=True)

    class Meta(TransactionDetailSerializer.Meta):
        fields = TransactionDetailSerializer.Meta.fields + ['wallet', 'flw_id', 'needs_review', 'raw_metadata',
                                                            'ip_address', 'entry_hash']
        read_only_fields = fields


class BankSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bank
        fields = ['id', 'name', 'code', 'country']
        read_only_fields = fields


class BankAccountSerializer(serializers.ModelSerializer):
    account_number = serializers.CharField(source='masked_account_number', read_only=True)

    class Meta:
        model = BankAccount
        fields = ['id', 'beneficiary_type', 'account_bank', 'bank_name', 'account_number', 'account_name',
                  'currency', 'country', 'is_verified', 'is_default', 'created_at']
        read_only_fields = fields


class CardSerializer(serializers.ModelSerializer):
    class Meta:
        model = Card
        fields = ['id', 'first_6digits', 'last_4digits', 'issuer', 'card_type', 'country', 'expiry', 'is_default',
                  'is_expired', 'last_used_at', 'created_at']
        read_only_fields = fields


class VirtualAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = VirtualAccount
        fields = ['id', 'kind', 'account_number', 'bank_name', 'account_name', 'currency', 'amount', 'expires_at',
                  'is_active', 'created_at']
        read_only_fields = fields


class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = ['id', 'event_type', 'reference', 'flw_id', 'payload', 'processed', 'processed_at',
                  'processing_attempts', 'processing_error', 'transaction', 'source_ip', 'created_at']
        read_only_fields = fields


class WebhookEndpointSerializer(serializers.ModelSerializer):
    secret = serializers.CharField(write_only=True, min_length=24, max_length=255)

    class Meta:
        model = WebhookEndpoint
        fields = ['id', 'name', 'url', 'secret', 'event_types', 'is_active', 'headers', 'max_attempts', 'timeout',
                  'created_at']
        read_only_fields = ['id', 'created_at']

    def validate_url(self, value):
        if not value.startswith('https://'):
            from django.conf import settings
            if not getattr(settings, 'DEBUG', False):
                raise serializers.ValidationError("Webhook endpoints must use https")
        return value


class WebhookDeliveryAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDeliveryAttempt
        fields = ['id', 'webhook_event', 'webhook_endpoint', 'attempt_number', 'response_code', 'response_body',
                  'is_success', 'created_at']
        read_only_fields = fields
