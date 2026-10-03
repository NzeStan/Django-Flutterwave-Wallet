"""System checks (``manage.py check`` / ``check --deploy``) that catch misconfiguration early."""
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import CURRENCY_CODES, VALID_BEARERS


def _is_live(secret):
    return secret.startswith('FLWSECK-') and '_TEST' not in secret.upper()


@register(Tags.compatibility)
def check_wallet_settings(app_configs, **kwargs):
    messages = []
    secret = wallet_settings.FLUTTERWAVE_SECRET_KEY or ''
    version = str(wallet_settings.API_VERSION or '').lower()

    if version not in ('v3', 'v4'):
        messages.append(Error("FLW_WALLET_API_VERSION must be 'v3' or 'v4'.", id='flutterwave_wallet.E011'))
    if version == 'v4':
        messages.extend(_check_v4(secret))

    if not secret and version != 'v4':
        messages.append(Warning('FLUTTERWAVE_SECRET_KEY is not set.',
                                hint='Set it in settings or the environment; Flutterwave calls will fail.',
                                id='flutterwave_wallet.W001'))
    elif not secret.startswith(('FLWSECK-', 'FLWSECK_TEST-')):
        messages.append(Warning('FLUTTERWAVE_SECRET_KEY does not look like a Flutterwave secret key.',
                                id='flutterwave_wallet.W002'))
    if secret and _is_live(secret) and getattr(settings, 'DEBUG', False):
        messages.append(Warning('A live Flutterwave key is configured while DEBUG=True.', id='flutterwave_wallet.W003'))

    public = wallet_settings.FLUTTERWAVE_PUBLIC_KEY or ''
    if secret and public and ('_TEST' in secret.upper()) != ('_TEST' in public.upper()):
        messages.append(Warning('FLUTTERWAVE_PUBLIC_KEY and FLUTTERWAVE_SECRET_KEY are from different modes.',
                                id='flutterwave_wallet.W004'))

    secret_hash = wallet_settings.FLUTTERWAVE_SECRET_HASH or ''
    if not secret_hash:
        messages.append(Warning('FLUTTERWAVE_SECRET_HASH is not set: every webhook will be rejected.',
                                hint='Set a long random secret hash on the Flutterwave dashboard webhook page '
                                     'and the same value here.', id='flutterwave_wallet.W005'))
    elif len(secret_hash) < 24:
        messages.append(Warning('FLUTTERWAVE_SECRET_HASH is short; use at least 32 random characters.',
                                id='flutterwave_wallet.W006'))

    if not wallet_settings.VERIFY_WEBHOOKS_WITH_API:
        messages.append(Warning('FLW_WALLET_VERIFY_WEBHOOKS_WITH_API is off: webhook bodies will be trusted.',
                                hint='Leave it on in production.', id='flutterwave_wallet.W007'))

    if version == 'v4' and wallet_settings.WEBHOOK_SIGNATURE_MODE == 'hash':
        messages.append(Error("FLW_WALLET_WEBHOOK_SIGNATURE_MODE='hash' rejects v4 webhooks, which are signed with "
                              "flutterwave-signature. Use 'auto' or 'hmac'.", id='flutterwave_wallet.E012'))

    if wallet_settings.WEBHOOK_SIGNATURE_MODE not in ('auto', 'hmac', 'hash'):
        messages.append(Error("FLW_WALLET_WEBHOOK_SIGNATURE_MODE must be 'auto', 'hmac' or 'hash'.",
                              id='flutterwave_wallet.E008'))

    for alias, database in getattr(settings, 'DATABASES', {}).items():
        if database.get('ATOMIC_REQUESTS'):
            messages.append(Warning(
                f"DATABASES[{alias!r}]['ATOMIC_REQUESTS'] is on. The wallet views opt out automatically, but "
                "never call withdraw()/pay_bill()/refund_deposit() from your own atomic views.",
                id='flutterwave_wallet.W010'))

    if 'rest_framework' not in settings.INSTALLED_APPS:
        messages.append(Error("'rest_framework' must be in INSTALLED_APPS to use the wallet API.",
                              id='flutterwave_wallet.E001'))

    if wallet_settings.USE_CELERY:
        try:
            import celery  # noqa: F401
        except ImportError:
            messages.append(Error('FLW_WALLET_USE_CELERY is True but Celery is not installed.',
                                  hint="pip install 'django-flutterwave-wallet[celery]'", id='flutterwave_wallet.E002'))

    currencies = [str(c).upper() for c in (wallet_settings.ALLOWED_CURRENCIES or [])]
    unknown = [c for c in currencies + [str(wallet_settings.CURRENCY).upper()] if c not in CURRENCY_CODES]
    if unknown:
        messages.append(Error(f"Unsupported currencies: {sorted(set(unknown))}", id='flutterwave_wallet.E003'))
    if str(wallet_settings.CURRENCY).upper() not in currencies:
        messages.append(Error('FLW_WALLET_CURRENCY must be listed in FLW_WALLET_ALLOWED_CURRENCIES.',
                              id='flutterwave_wallet.E004'))

    for operation, rule in (wallet_settings.FEE_RULES or {}).items():
        if not isinstance(rule, dict):
            messages.append(Error(f"FLW_WALLET_FEE_RULES[{operation!r}] must be a dict.", id='flutterwave_wallet.E005'))
            continue
        if rule.get('bearer') and rule['bearer'] not in VALID_BEARERS:
            messages.append(Error(f"FLW_WALLET_FEE_RULES[{operation!r}] bearer must be one of {sorted(VALID_BEARERS)}.",
                                  id='flutterwave_wallet.E005'))
        for key in ('percentage', 'flat', 'cap', 'minimum'):
            if rule.get(key) in (None, ''):
                continue
            try:
                Decimal(str(rule[key]))
            except InvalidOperation:
                messages.append(Error(f"FLW_WALLET_FEE_RULES[{operation!r}][{key!r}] is not a number.",
                                      id='flutterwave_wallet.E005'))

    if wallet_settings.PROCESSING_FEE_BEARER not in ('platform', 'customer'):
        messages.append(Error("FLW_WALLET_PROCESSING_FEE_BEARER must be 'platform' or 'customer'.",
                              id='flutterwave_wallet.E006'))

    for name in ('FEE_CALCULATOR', 'PHONE_NUMBER_NORMALIZER'):
        try:
            wallet_settings.import_from(name)
        except Exception as exc:
            messages.append(Error(str(exc), id='flutterwave_wallet.E007'))
    try:
        wallet_settings.import_list('NOTIFICATION_BACKENDS')
    except Exception as exc:
        messages.append(Error(f"FLW_WALLET_NOTIFICATION_BACKENDS: {exc}", id='flutterwave_wallet.E009'))

    if wallet_settings.FIELD_ENCRYPTION_KEYS:
        try:
            from flutterwave_wallet.utils.crypto import get_fernet
            get_fernet()
        except Exception as exc:
            messages.append(Error(str(exc), id='flutterwave_wallet.E010'))
    elif not getattr(settings, 'DEBUG', False):
        messages.append(Warning('FLW_WALLET_FIELD_ENCRYPTION_KEYS is empty: card tokens are encrypted with a key '
                                'derived from SECRET_KEY, so rotating SECRET_KEY makes them unreadable.',
                                hint='Generate a dedicated key with cryptography.fernet.Fernet.generate_key().',
                                id='flutterwave_wallet.W008'))

    redirect_url = wallet_settings.CALLBACK_REDIRECT_URL
    if redirect_url and not str(redirect_url).startswith(('/', 'https://', 'http://')):
        messages.append(Warning(f"FLW_WALLET_CALLBACK_REDIRECT_URL={redirect_url!r} is not a path or URL.",
                                id='flutterwave_wallet.W009'))
    return messages


V3_ONLY_FEATURES = ('DEPOSITS', 'CARDS', 'BILL_PAYMENTS', 'KYC')


def _check_v4(secret):
    messages = []
    if not wallet_settings.FLUTTERWAVE_CLIENT_ID or not wallet_settings.FLUTTERWAVE_CLIENT_SECRET:
        messages.append(Error('FLW_WALLET_API_VERSION is v4 but FLUTTERWAVE_CLIENT_ID / FLUTTERWAVE_CLIENT_SECRET '
                              'are not set.', id='flutterwave_wallet.E013'))
    environment = str(wallet_settings.FLUTTERWAVE_V4_ENVIRONMENT or '').lower()
    if environment not in ('sandbox', 'production'):
        messages.append(Error("FLUTTERWAVE_V4_ENVIRONMENT must be 'sandbox' or 'production'.",
                              id='flutterwave_wallet.E014'))
    if environment == 'production' and getattr(settings, 'DEBUG', False):
        messages.append(Warning('The v4 production environment is configured while DEBUG=True.',
                                id='flutterwave_wallet.W011'))
    withdrawal_rule = (wallet_settings.FEE_RULES or {}).get('withdrawal') or {}
    if wallet_settings.ENABLE_FEES and withdrawal_rule.get('source') == 'flutterwave':
        messages.append(Error('Flutterwave v4 has no transfer fee quote: replace "source": "flutterwave" in the '
                              'withdrawal fee rule with a percentage/flat rule.', id='flutterwave_wallet.E015'))
    if not secret:
        enabled = [name for name in V3_ONLY_FEATURES if wallet_settings.get(f'ENABLE_{name}')]
        if enabled:
            messages.append(Warning(
                'Hosted checkout (deposits), saved cards, bills and BVN checks only exist on Flutterwave v3 and '
                'need FLUTTERWAVE_SECRET_KEY, which is not set.',
                hint='Set FLUTTERWAVE_SECRET_KEY, or turn off: ' + ', '.join(f'FLW_WALLET_ENABLE_{n}' for n in enabled),
                id='flutterwave_wallet.W012'))
    return messages
