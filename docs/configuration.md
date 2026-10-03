# Configuration

Every setting can be provided three ways, checked in this order:

1. **Django settings**: `FLW_WALLET_ENABLE_FEES = True`
2. **Environment variable** with the same name: `FLW_WALLET_ENABLE_FEES=true`
3. **Package default**

Flutterwave credentials use bare names (`FLUTTERWAVE_SECRET_KEY`, ...); everything else is
prefixed with `FLW_WALLET_`, so the package can live alongside other wallet packages.
Settings are read on every access, so `override_settings` and environment changes always apply.
Environment values are converted to the default's type: booleans accept `true/false/1/0/yes/no`,
lists accept `a,b,c` or JSON, dicts accept JSON. An empty value means "use the default".

```bash
python manage.py wallet_settings          # what is in effect (secrets masked)
python manage.py wallet_settings --json
```

`.env.example` lists every setting with its default and an explanation.

## Flutterwave

| Setting | Default | Notes |
| --- | --- | --- |
| `FLUTTERWAVE_SECRET_KEY` | `''` | **Required** |
| `FLUTTERWAVE_PUBLIC_KEY` | `''` | Returned by the checkout endpoint for Inline JS |
| `FLUTTERWAVE_SECRET_HASH` | `''` | **Required for webhooks**. Webhooks are refused while it is empty |
| `FLUTTERWAVE_API_URL` | `https://api.flutterwave.com/v3` | |
| `FLUTTERWAVE_TIMEOUT` | `30` | Seconds |
| `FLUTTERWAVE_MAX_RETRIES` | `2` | v3: GET only. v4: GET and POST (POSTs carry an idempotency key) |

## Flutterwave v4

| Setting | Default | Notes |
| --- | --- | --- |
| `FLW_WALLET_API_VERSION` | `v3` | `v4` moves direct charges, virtual accounts, withdrawals, refunds, bank lookups and balances to v4 |
| `FLUTTERWAVE_CLIENT_ID` / `FLUTTERWAVE_CLIENT_SECRET` | `''` | **Required for v4**. OAuth client credentials |
| `FLUTTERWAVE_V4_ENVIRONMENT` | `sandbox` | `sandbox` or `production` (picks the base URL) |
| `FLUTTERWAVE_V4_API_URL` | `''` | Override the base URL (e.g. an egress proxy) |
| `FLUTTERWAVE_V4_TOKEN_URL` | Flutterwave's IdP | OAuth token endpoint |

Hosted checkout, saved-card top-ups, bills and BVN verification only exist on v3, so they keep using
`FLUTTERWAVE_SECRET_KEY` even when `API_VERSION` is `v4`. Existing records always continue on the
version that created them, so switching versions is safe at any time.

## Core and currencies

| Setting | Default | Notes |
| --- | --- | --- |
| `FLW_WALLET_CURRENCY` | `NGN` | The default wallet's currency |
| `FLW_WALLET_ALLOWED_CURRENCIES` | `['NGN']` | Users get at most one wallet per listed currency |
| `FLW_WALLET_COUNTRY` | `NG` | Your business country (bills, tokenized charges) |
| `FLW_WALLET_AUTO_CREATE_WALLET` | `True` | Create the default wallet when a user is created |
| `FLW_WALLET_USE_CELERY` | `False` | Process webhooks/provisioning/deliveries on Celery |

## Feature switches

A disabled feature makes its service methods raise `FeatureDisabled` and its endpoints answer 403.

`ENABLE_DEPOSITS`, `ENABLE_DIRECT_CHARGES`, `ENABLE_WITHDRAWALS`, `ENABLE_INTERNAL_TRANSFERS`,
`ENABLE_PAYMENTS`, `ENABLE_CARDS`, `ENABLE_REFUNDS`, `ENABLE_VIRTUAL_ACCOUNTS`,
`ENABLE_BILL_PAYMENTS`, `ENABLE_KYC`, `ENABLE_RECIPIENT_LOOKUP` (all `True`),
`ENABLE_WEBHOOK_FORWARDING` (`False`), `SAVE_CARDS` (`True`). Every name takes the `FLW_WALLET_` prefix.

## Limits

| Setting | Default |
| --- | --- |
| `FLW_WALLET_MINIMUM_BALANCE` | `0` |
| `FLW_WALLET_MINIMUM_TRANSACTION_AMOUNT` / `MAXIMUM_TRANSACTION_AMOUNT` | `None` |
| `FLW_WALLET_MAXIMUM_DAILY_TRANSACTION` | `None` (per-wallet override: `Wallet.daily_limit`) |
| `FLW_WALLET_MINIMUM_DEPOSIT_AMOUNT` / `MINIMUM_WITHDRAWAL_AMOUNT` | `100` |
| `FLW_WALLET_AMOUNT_TOLERANCE` | `0.01` |

The daily limit counts pending and successful withdrawals, transfers, payments and bills,
and is checked while the wallet row is locked, so parallel requests cannot race past it.

## Security

| Setting | Default | Notes |
| --- | --- | --- |
| `FLW_WALLET_VERIFY_WEBHOOKS_WITH_API` | `True` | Re-fetch every object from Flutterwave before moving money. Keep it on |
| `FLW_WALLET_WEBHOOK_SIGNATURE_MODE` | `auto` | `auto` (HMAC `flutterwave-signature` or `verif-hash`), `hmac`, `hash`. v4 webhooks need `auto` or `hmac` |
| `FLW_WALLET_WEBHOOK_VERIFY_IP` / `WEBHOOK_ALLOWED_IPS` | `False` / `[]` | Optional source-IP allowlist |
| `FLW_WALLET_TRUST_X_FORWARDED_FOR` | `False` | Only behind a proxy you control |
| `FLW_WALLET_FIELD_ENCRYPTION_KEYS` | `[]` | Fernet keys for card tokens. First encrypts, all decrypt (rotation) |
| `FLW_WALLET_LEDGER_HASH_CHAIN` | `True` | Tamper-evident ledger |
| `FLW_WALLET_REQUIRE_TRANSACTION_PIN` | `False` | Required for money-out and adding payout accounts |
| `FLW_WALLET_PIN_LENGTH` / `PIN_MAX_ATTEMPTS` / `PIN_LOCKOUT_MINUTES` | `4` / `5` / `30` | |
| `FLW_WALLET_ENABLE_THROTTLING` / `THROTTLE_RATES` | `True` / see `.env.example` | Scopes: lookup, pin, money, otp, resolve, kyc |
| `FLW_WALLET_IDEMPOTENCY_HEADER` / `REQUIRE_IDEMPOTENCY_KEY` / `IDEMPOTENCY_TTL_HOURS` | `Idempotency-Key` / `False` / `24` | |
| `FLW_WALLET_AUDIT_LOGGER` | `flutterwave_wallet.audit` | Route it to durable storage |

## Fees

```python
FLW_WALLET_ENABLE_FEES = True
FLW_WALLET_FEE_RULES = {
    'deposit':      {'percentage': '1.4', 'flat': '0', 'cap': '2000', 'bearer': 'customer'},
    'withdrawal':   {'source': 'flutterwave', 'bearer': 'customer'},   # Flutterwave's own transfer fee
    'transfer':     {'percentage': '0', 'flat': '10', 'bearer': 'customer'},
    'payment':      {'percentage': '2.5', 'cap': '5000', 'bearer': 'merchant'},   # marketplace commission
    'bill_payment': {'flat': '20', 'bearer': 'customer'},
}
FLW_WALLET_FEE_COLLECTION_WALLET_ID = '<uuid of your revenue wallet>'
FLW_WALLET_PROCESSING_FEE_BEARER = 'platform'      # or 'customer'
```

A rule takes `percentage`, `flat`, `minimum`, `cap` and `bearer`:

- `customer`: the payer pays the fee on top
- `merchant`: the receiver absorbs it
- `platform`: you absorb it
- `split`: shared, see `FLW_WALLET_FEE_SPLIT_CUSTOMER_PERCENTAGE`

For anything else, subclass `FeeCalculator` and set `FLW_WALLET_FEE_CALCULATOR`.

## Checkout

| Setting | Default | Notes |
| --- | --- | --- |
| `FLW_WALLET_DEFAULT_REDIRECT_URL` | `None` | Usually your `/wallet/callback/` URL |
| `FLW_WALLET_CALLBACK_REDIRECT_URL` | `None` | Your frontend; the callback redirects there with `?tx_ref=&status=`. Empty = JSON |
| `FLW_WALLET_PAYMENT_OPTIONS` | `None` | e.g. `card,banktransfer,ussd` |
| `FLW_WALLET_CHECKOUT_TITLE` / `CHECKOUT_LOGO` | `None` | |
| `FLW_WALLET_CHECKOUT_SESSION_DURATION` | `30` | Minutes |
| `FLW_WALLET_CHECKOUT_MAX_RETRY_ATTEMPTS` | `5` | |

## Virtual accounts, reconciliation, retention, API, banks, notifications

| Setting | Default |
| --- | --- |
| `FLW_WALLET_VIRTUAL_ACCOUNT_NARRATION` | `None` |
| `FLW_WALLET_DYNAMIC_ACCOUNT_EXPIRY_SECONDS` | `3600` |
| `FLW_WALLET_RECONCILE_AFTER_MINUTES` / `RECONCILE_BATCH_SIZE` | `10` / `500` |
| `FLW_WALLET_ABANDON_DEPOSIT_AFTER_HOURS` | `24` |
| `FLW_WALLET_WEBHOOK_RETENTION_DAYS` / `WEBHOOK_RETRY_WINDOW_HOURS` | `90` / `72` |
| `FLW_WALLET_API_PERMISSION_CLASSES` | `['rest_framework.permissions.IsAuthenticated']` |
| `FLW_WALLET_API_PAGE_SIZE` / `API_MAX_PAGE_SIZE` | `20` / `100` |
| `FLW_WALLET_AUTO_SYNC_BANKS` / `BANK_COUNTRIES` | `False` / `['NG']` |
| `FLW_WALLET_NOTIFICATION_BACKENDS` / `EMAIL_SENDER` | `[]` / `None` |
| `FLW_WALLET_USER_PHONE_FIELD` / `PHONE_DEFAULT_COUNTRY_CODE` / `PHONE_NUMBER_NORMALIZER` | `None` / `234` / `None` |
| `FLW_WALLET_TRANSFER_RECIPIENT_LOOKUP_FIELDS` | `['id', 'tag', 'phone_number', 'email']` |

## System checks

`manage.py check` reports:

- missing or mismatched keys
- a live key while `DEBUG=True`
- a missing or short secret hash
- webhook re-verification switched off
- invalid fee rules or currencies
- `ATOMIC_REQUESTS` (supported, but read the warning)
- unreadable encryption keys
- unimportable dotted paths
