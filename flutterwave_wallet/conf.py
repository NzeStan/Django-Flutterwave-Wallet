"""
Settings for django-flutterwave-wallet.

Every setting can be supplied in three ways, checked in this order:

1. Your Django settings module, e.g. ``FLW_WALLET_ENABLE_FEES = True``
2. An environment variable with the same name, e.g. ``FLW_WALLET_ENABLE_FEES=true``
3. The package default defined in ``DEFAULTS`` below

Flutterwave credentials use their conventional names (``FLUTTERWAVE_SECRET_KEY``,
``FLUTTERWAVE_PUBLIC_KEY``, ``FLUTTERWAVE_SECRET_HASH``, ``FLUTTERWAVE_CLIENT_ID``...);
every other setting is prefixed with ``FLW_WALLET_`` so this package can live next
to other wallet packages without clashing.

Settings are resolved lazily on every access, so ``override_settings`` in tests
and environment changes are always honoured::

    from flutterwave_wallet.conf import wallet_settings
    wallet_settings.CURRENCY
"""
import json
import os
from decimal import Decimal, InvalidOperation

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

PREFIX = 'FLW_WALLET_'

# Settings read from their bare name (no prefix)
UNPREFIXED = {
    'FLUTTERWAVE_SECRET_KEY',
    'FLUTTERWAVE_PUBLIC_KEY',
    'FLUTTERWAVE_SECRET_HASH',
    'FLUTTERWAVE_API_URL',
    'FLUTTERWAVE_TIMEOUT',
    'FLUTTERWAVE_MAX_RETRIES',
    'FLUTTERWAVE_CLIENT_ID',
    'FLUTTERWAVE_CLIENT_SECRET',
    'FLUTTERWAVE_V4_ENVIRONMENT',
    'FLUTTERWAVE_V4_API_URL',
    'FLUTTERWAVE_V4_TOKEN_URL',
}

# Settings whose values must never be printed
SECRET_SETTINGS = {
    'FLUTTERWAVE_SECRET_KEY',
    'FLUTTERWAVE_SECRET_HASH',
    'FLUTTERWAVE_CLIENT_SECRET',
    'FIELD_ENCRYPTION_KEYS',
}


DEFAULTS = {
    # ------------------------------------------------------------------
    # Flutterwave credentials (Dashboard -> Settings -> API Keys / Webhooks)
    # ------------------------------------------------------------------
    'FLUTTERWAVE_SECRET_KEY': '',
    'FLUTTERWAVE_PUBLIC_KEY': '',
    # The "secret hash" you set on the dashboard webhook page. Required to accept webhooks.
    'FLUTTERWAVE_SECRET_HASH': '',
    'FLUTTERWAVE_API_URL': 'https://api.flutterwave.com/v3',
    'FLUTTERWAVE_TIMEOUT': 30,
    # Retries apply to connection errors and 429/5xx on GET requests only. Requests that
    # move money (POST/PUT) are never retried automatically - their outcome is reconciled.
    'FLUTTERWAVE_MAX_RETRIES': 2,

    # ------------------------------------------------------------------
    # Flutterwave v4 (OAuth 2.0 client credentials: Dashboard -> Settings -> API keys, v4)
    # ------------------------------------------------------------------
    'FLUTTERWAVE_CLIENT_ID': '',
    'FLUTTERWAVE_CLIENT_SECRET': '',
    # 'sandbox' or 'production' - picks the v4 base URL
    'FLUTTERWAVE_V4_ENVIRONMENT': 'sandbox',
    # Override the v4 base URL (empty = derived from the environment)
    'FLUTTERWAVE_V4_API_URL': '',
    'FLUTTERWAVE_V4_TOKEN_URL': 'https://idp.flutterwave.com/realms/flutterwave/protocol/openid-connect/token',

    # ------------------------------------------------------------------
    # Core wallet behaviour
    # ------------------------------------------------------------------
    # Currency of a user's default wallet
    'CURRENCY': 'NGN',
    # Currencies users may hold wallets in (one wallet per user per currency)
    'ALLOWED_CURRENCIES': ['NGN'],
    # ISO 3166 alpha-2 country of your Flutterwave business
    'COUNTRY': 'NG',
    'AUTO_CREATE_WALLET': True,
    'USE_CELERY': False,
    # Flutterwave API used for direct charges, virtual accounts, withdrawals, refunds, bank lookups
    # and balances: 'v3' (secret key) or 'v4' (OAuth client credentials). Hosted checkout, saved-card
    # top-ups, bills and BVN checks only exist on v3 and always use FLUTTERWAVE_SECRET_KEY.
    'API_VERSION': 'v3',

    # ------------------------------------------------------------------
    # Feature switches - turn off anything you do not need
    # ------------------------------------------------------------------
    'ENABLE_DEPOSITS': True,
    'ENABLE_DIRECT_CHARGES': True,
    'ENABLE_WITHDRAWALS': True,
    'ENABLE_INTERNAL_TRANSFERS': True,
    'ENABLE_PAYMENTS': True,
    'ENABLE_CARDS': True,
    'ENABLE_REFUNDS': True,
    'ENABLE_VIRTUAL_ACCOUNTS': True,
    'ENABLE_BILL_PAYMENTS': True,
    'ENABLE_KYC': True,
    'ENABLE_WEBHOOK_FORWARDING': False,
    'ENABLE_RECIPIENT_LOOKUP': True,
    # Save tokenized cards after successful card deposits (for one-tap top-ups)
    'SAVE_CARDS': True,

    # ------------------------------------------------------------------
    # Virtual accounts (bank-transfer funding)
    # ------------------------------------------------------------------
    # Text shown as the account name when a payer resolves the account number
    'VIRTUAL_ACCOUNT_NARRATION': None,
    # Lifetime (seconds) of one-off virtual accounts used for a single bank-transfer deposit
    'DYNAMIC_ACCOUNT_EXPIRY_SECONDS': 3600,

    # ------------------------------------------------------------------
    # Limits (None means unlimited). Amounts are in major units (e.g. naira).
    # ------------------------------------------------------------------
    'MINIMUM_BALANCE': 0,
    'MINIMUM_TRANSACTION_AMOUNT': None,
    'MAXIMUM_TRANSACTION_AMOUNT': None,
    # Maximum total of outgoing money (withdrawals, transfers, payments, bills) per day
    'MAXIMUM_DAILY_TRANSACTION': None,
    'MINIMUM_DEPOSIT_AMOUNT': 100,
    'MINIMUM_WITHDRAWAL_AMOUNT': 100,
    # Deposits whose verified amount differs from the expected amount by more than this are
    # credited for what was actually paid and flagged for review
    'AMOUNT_TOLERANCE': Decimal('0.01'),

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    'REQUIRE_TRANSACTION_PIN': False,
    'PIN_LENGTH': 4,
    'PIN_MAX_ATTEMPTS': 5,
    'PIN_LOCKOUT_MINUTES': 30,
    # Never trust a webhook body: re-fetch the transaction/transfer from Flutterwave
    # before moving money. Leave this on.
    'VERIFY_WEBHOOKS_WITH_API': True,
    # 'auto' accepts a valid `flutterwave-signature` (HMAC-SHA256) or `verif-hash` header;
    # 'hmac' accepts only the signature; 'hash' accepts only verif-hash
    'WEBHOOK_SIGNATURE_MODE': 'auto',
    'WEBHOOK_VERIFY_IP': False,
    'WEBHOOK_ALLOWED_IPS': [],
    # Trust X-Forwarded-For when reading the client IP (only behind a proxy you control)
    'TRUST_X_FORWARDED_FOR': False,
    # Keys (Fernet, urlsafe base64, 32 bytes) used to encrypt card tokens at rest. The first
    # key encrypts; all keys decrypt (rotate by prepending a new key). Empty = derived from
    # Django's SECRET_KEY.
    'FIELD_ENCRYPTION_KEYS': [],
    # Tamper-evident hash chain over every wallet's ledger entries
    'LEDGER_HASH_CHAIN': True,
    # Rate limits per user (or IP when anonymous), using Django's cache. Use a shared
    # cache (Redis/Memcached) when you run more than one server.
    'ENABLE_THROTTLING': True,
    'THROTTLE_RATES': {
        'lookup': '20/min',
        'pin': '5/min',
        'money': '30/min',
        'otp': '10/min',
        'resolve': '20/min',
        'kyc': '5/min',
    },
    # Idempotency: clients send this header on POSTs that move money; retries with the
    # same key replay the first response instead of moving money twice.
    'IDEMPOTENCY_HEADER': 'Idempotency-Key',
    'REQUIRE_IDEMPOTENCY_KEY': False,
    'IDEMPOTENCY_TTL_HOURS': 24,
    'AUDIT_LOGGER': 'flutterwave_wallet.audit',

    # ------------------------------------------------------------------
    # Phone numbers & internal transfer recipient lookup
    # ------------------------------------------------------------------
    # Dotted attribute path on your user model that holds the phone number
    'USER_PHONE_FIELD': None,
    'PHONE_DEFAULT_COUNTRY_CODE': '234',
    # Dotted path to a callable(str) -> str replacing the built-in normaliser
    'PHONE_NUMBER_NORMALIZER': None,
    'TRANSFER_RECIPIENT_LOOKUP_FIELDS': ['id', 'tag', 'phone_number', 'email'],

    # ------------------------------------------------------------------
    # Fees (your platform's own pricing; Flutterwave's charges are separate)
    # ------------------------------------------------------------------
    'ENABLE_FEES': False,
    # Dotted path to a FeeCalculator subclass for fully custom pricing
    'FEE_CALCULATOR': 'flutterwave_wallet.services.fee_service.FeeCalculator',
    # Per operation: percentage, flat, cap (None = no cap), minimum, bearer.
    # Bearers: customer (payer pays on top), merchant (receiver absorbs),
    # platform (nobody is charged), split (FEE_SPLIT_CUSTOMER_PERCENTAGE).
    # For withdrawals, "source": "flutterwave" uses Flutterwave's transfer fee quote.
    'FEE_RULES': {
        'deposit': {'percentage': '0', 'flat': '0', 'cap': None, 'bearer': 'merchant'},
        'withdrawal': {'percentage': '0', 'flat': '0', 'cap': None, 'bearer': 'customer'},
        'transfer': {'percentage': '0', 'flat': '0', 'cap': None, 'bearer': 'customer'},
        'payment': {'percentage': '0', 'flat': '0', 'cap': None, 'bearer': 'merchant'},
        'bill_payment': {'percentage': '0', 'flat': '0', 'cap': None, 'bearer': 'customer'},
    },
    'FEE_SPLIT_CUSTOMER_PERCENTAGE': Decimal('50'),
    # Who bears Flutterwave's own collection fee (app_fee) on deposits:
    # 'platform' credits the full amount; 'customer' deducts any part of app_fee the
    # payer did not already pay on top at checkout.
    'PROCESSING_FEE_BEARER': 'platform',
    # Platform fees are accrued and swept in batches into this wallet (UUID). Batching
    # keeps one hot fee wallet from serialising every transaction. None = just record them.
    'FEE_COLLECTION_WALLET_ID': None,

    # ------------------------------------------------------------------
    # Checkout (Flutterwave Standard)
    # ------------------------------------------------------------------
    # Where Flutterwave sends the customer after paying (our callback view by default)
    'DEFAULT_REDIRECT_URL': None,
    # Where our callback view redirects after verifying (your frontend). ``?tx_ref=&status=``
    # is appended. Empty = the callback answers with JSON.
    'CALLBACK_REDIRECT_URL': None,
    # Comma separated payment options, e.g. 'card,banktransfer,ussd' (None = dashboard defaults)
    'PAYMENT_OPTIONS': None,
    'CHECKOUT_TITLE': None,
    'CHECKOUT_LOGO': None,
    # Minutes a checkout link stays valid (max 1440)
    'CHECKOUT_SESSION_DURATION': 30,
    'CHECKOUT_MAX_RETRY_ATTEMPTS': 5,

    # ------------------------------------------------------------------
    # Reconciliation (the safety net for missed webhooks)
    # ------------------------------------------------------------------
    'RECONCILE_AFTER_MINUTES': 10,
    'RECONCILE_BATCH_SIZE': 500,
    # Unpaid checkout deposits older than this are cancelled
    'ABANDON_DEPOSIT_AFTER_HOURS': 24,

    # ------------------------------------------------------------------
    # Data retention
    # ------------------------------------------------------------------
    'WEBHOOK_RETENTION_DAYS': 90,
    'WEBHOOK_RETRY_WINDOW_HOURS': 72,

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------
    'API_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
    'API_PAGE_SIZE': 20,
    'API_MAX_PAGE_SIZE': 100,

    # ------------------------------------------------------------------
    # Banks
    # ------------------------------------------------------------------
    'AUTO_SYNC_BANKS': False,
    'BANK_COUNTRIES': ['NG'],

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    'NOTIFICATION_BACKENDS': [],
    'EMAIL_SENDER': None,
}


_TRUE = {'1', 'true', 'yes', 'on', 'y', 't'}
_FALSE = {'0', 'false', 'no', 'off', 'n', 'f', ''}


def setting_name(name):
    """Return the Django settings / environment variable name for a wallet setting."""
    return name if name in UNPREFIXED else f'{PREFIX}{name}'


def _parse_env(raw, default):
    """Coerce an environment variable string to the type of the default value."""
    value = raw.strip()
    if value == '':
        return default

    if isinstance(default, bool):
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ImproperlyConfigured(f"Expected a boolean value, got {raw!r}")

    if isinstance(default, int):
        return int(value)

    if isinstance(default, Decimal):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ImproperlyConfigured(f"Expected a decimal value, got {raw!r}") from exc

    if isinstance(default, (list, tuple, dict)):
        if value.startswith(('[', '{')):
            return json.loads(value)
        if isinstance(default, dict):
            raise ImproperlyConfigured(f"Expected a JSON object, got {raw!r}")
        return [item.strip() for item in value.split(',') if item.strip()]

    if value.lower() in ('none', 'null'):
        return None

    if default is None:
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, bool):
            return parsed
        if isinstance(parsed, (int, float)):
            return Decimal(value)
        return parsed
    return value


def mask(value):
    """Mask a secret for display."""
    if not value:
        return value
    value = str(value)
    return f"{value[:8]}...{value[-4:]}" if len(value) > 16 else '***'


class WalletSettings:
    """Lazy accessor: Django settings, then environment variables, then defaults - on every access."""

    def __init__(self, defaults=None):
        self.defaults = defaults or DEFAULTS

    def __getattr__(self, name):
        if name.startswith('_') or name == 'defaults':
            raise AttributeError(name)
        return self.get(name)

    def get(self, name):
        if name not in self.defaults:
            raise AttributeError(f"Unknown flutterwave wallet setting: {name}")

        full_name = setting_name(name)
        default = self.defaults[name]

        if hasattr(django_settings, full_name):
            return getattr(django_settings, full_name)

        raw = os.environ.get(full_name)
        if raw is not None:
            return _parse_env(raw, default)

        # Copy mutable defaults so callers can't mutate the module-level dict
        if isinstance(default, list):
            return list(default)
        if isinstance(default, dict):
            return {key: dict(value) if isinstance(value, dict) else value for key, value in default.items()}
        return default

    def as_dict(self):
        """Every resolved setting with secrets masked - useful for debugging."""
        resolved = {}
        for name in self.defaults:
            value = self.get(name)
            if name in SECRET_SETTINGS:
                value = ['***'] * len(value) if isinstance(value, (list, tuple)) else mask(value)
            resolved[name] = value
        return resolved

    def import_from(self, name):
        """Import the object referenced by a dotted-path setting (None if unset)."""
        path = self.get(name)
        if not path:
            return None
        if not isinstance(path, str):
            return path
        try:
            return import_string(path)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"Could not import {path!r} for setting {setting_name(name)}: {exc}"
            ) from exc

    def import_list(self, name):
        return [import_string(path) if isinstance(path, str) else path for path in (self.get(name) or [])]


wallet_settings = WalletSettings()


def get_wallet_setting(name):
    try:
        return wallet_settings.get(name)
    except AttributeError as exc:
        raise ValueError(str(exc)) from exc


def load_env_file(path='.env', override=False):
    """
    Load ``KEY=VALUE`` lines from a ``.env`` file into ``os.environ``.

    Safe to call at the top of ``settings.py``. Existing environment variables win
    unless ``override=True``. Returns the number of variables set; a missing file is ignored.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return 0

    loaded = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[len('export '):]
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        elif ' #' in value:
            value = value.split(' #', 1)[0].rstrip()
        if not key or (key in os.environ and not override):
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def is_feature_enabled(feature):
    """True if ``FLW_WALLET_ENABLE_<FEATURE>`` is on, e.g. ``is_feature_enabled('WITHDRAWALS')``."""
    return bool(wallet_settings.get(f'ENABLE_{feature.upper()}'))
