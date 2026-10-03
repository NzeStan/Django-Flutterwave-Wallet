# Installation

## Requirements

- Python 3.10+
- Django 4.2 – 6.x, Django REST Framework 3.14+
- PostgreSQL (recommended for production; MySQL works; SQLite is for development only:
  it has no row-level locking)
- A shared cache (Redis or Memcached) when you run more than one process, for rate limits and
  work claims
- Celery 5.3+ (optional)

## Install

```bash
pip install django-flutterwave-wallet
pip install "django-flutterwave-wallet[celery]"   # with background processing
```

```python
# settings.py
INSTALLED_APPS = [
    ...
    'rest_framework',
    'djmoney',
    'flutterwave_wallet',
]
```

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    ...
    path('wallet/', include('flutterwave_wallet.urls')),
]
```

This gives you:

| URL | What |
| --- | --- |
| `/wallet/api/...` | REST API (see [api_reference.md](api_reference.md)) |
| `/wallet/webhook/` | Flutterwave webhook receiver |
| `/wallet/callback/` | Where Flutterwave Standard checkout returns the customer |

## Credentials

Create `.env` from `.env.example` (or set real environment variables):

```bash
FLUTTERWAVE_SECRET_KEY=FLWSECK_TEST-...
FLUTTERWAVE_PUBLIC_KEY=FLWPUBK_TEST-...
FLUTTERWAVE_SECRET_HASH=<a long random string>
FLW_WALLET_FIELD_ENCRYPTION_KEYS=<Fernet key>
# Flutterwave v4 (optional - see "Choosing v3 or v4" in usage.md)
FLW_WALLET_API_VERSION=v4
FLUTTERWAVE_CLIENT_ID=...
FLUTTERWAVE_CLIENT_SECRET=...
FLUTTERWAVE_V4_ENVIRONMENT=sandbox
FLW_WALLET_DEFAULT_REDIRECT_URL=https://api.example.com/wallet/callback/
FLW_WALLET_CALLBACK_REDIRECT_URL=https://app.example.com/wallet/funded
```

Generate the field-encryption key once and keep it in your secrets manager:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Load `.env` at the top of `settings.py` if you don't use another loader:

```python
from flutterwave_wallet.conf import load_env_file
load_env_file(BASE_DIR / '.env')
```

## Database and reference data

```bash
python manage.py migrate
python manage.py sync_banks NG GH KE     # bank / mobile-money operator lists
python manage.py check --deploy          # the package validates its own configuration
```

## Flutterwave dashboard

1. **Settings → Webhooks**: URL `https://your-domain.com/wallet/webhook/`, secret hash = `FLUTTERWAVE_SECRET_HASH`.
   Tick the events you use (charge completed, transfer completed, bill payments...).
2. **Settings → API keys**: copy the v3 secret and public keys (and, for v4, the client ID and secret).
   You do **not** need the encryption key: this package never handles card details.
3. If you charge fees on top in checkout, decide whether customers pay Flutterwave's
   processing fee (dashboard setting) and set `FLW_WALLET_PROCESSING_FEE_BEARER` to match.
4. Whitelist your server IPs for transfers if your account requires it.

## Background jobs

Without Celery, schedule the management commands (every 5 minutes is a good default):

```cron
*/5 * * * *  python manage.py reconcile_transactions
*/5 * * * *  python manage.py retry_webhooks
*/10 * * * * python manage.py sweep_platform_fees
0 3 * * *    python manage.py verify_ledger && python manage.py prune_wallet_data
```

With Celery, see [operations.md](operations.md#celery).

## Test mode

Use `FLWSECK_TEST-` keys. Flutterwave's sandbox accepts its documented test cards and
accounts. For v3 transfers, the reference suffixes `_PMCKDU_1` (success) and `_PMCKDU_1_FAIL`
let you simulate outcomes. Pass `reference=` explicitly when testing those.

On v4, set `FLUTTERWAVE_V4_ENVIRONMENT=sandbox`. The v4 client accepts `scenario_key=` on calls
(sent as `X-Scenario-Key`) to simulate outcomes such as `scenario:successful` for transfers.
