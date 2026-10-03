# Django Flutterwave Wallet

A pluggable, production-grade wallet for Django, backed by [Flutterwave](https://flutterwave.com).
It is backend only: models, services, a REST API, webhooks, Celery tasks and the admin.
Use it for an e-commerce site, a marketplace, a fintech product or any app where users hold,
send and receive money, and switch off whatever you don't need.

- **Flutterwave v3 and v4**: one setting (`FLW_WALLET_API_VERSION`) switches charges, virtual accounts,
  payouts, refunds and bank lookups to the v4 API (OAuth, idempotent requests); complete clients for both
- **Multi-currency wallets**: one wallet per user per currency (NGN, USD, GHS, KES, and more)
- **Cards on hosted checkout only**: card details never reach your server (PCI-DSS SAQ A scope);
  returning customers top up with **saved card tokens** (encrypted at rest)
- **Fund wallets** with hosted checkout, non-card **direct charges** (bank transfer, USSD, M-Pesa,
  mobile money, account debit, ACH, eNaira, NQR, OPay…), and **virtual account numbers**
  (a permanent account per wallet, or a one-off account per deposit)
- **Withdraw** to bank accounts and **mobile money** (Flutterwave Transfers), with account-name verification
- **Send money between wallets** by wallet ID, `@tag`, phone number or email
- **Pay with wallet** at checkout, with optional **escrow** for marketplaces
- **Bill payments** from the wallet balance: airtime, data, cable TV, electricity, internet
- **Refunds** to the payer's card/account, staff **reversals**, **BVN verification** (KYC)
- **Fees** that are fully configurable: who pays (customer / merchant / platform / split), per-operation
  rules, Flutterwave's own transfer fee quote, or your own calculator, with fees **accrued and swept**
  to a revenue wallet
- **The whole Flutterwave API**, both versions: `FlutterwaveClient` (v3: payment plans, subscriptions,
  split payments, payout subaccounts, bulk transfers, bulk tokenized charges, FX, settlements, chargebacks,
  OTPs, Remita…) and `FlutterwaveV4Client` (v4: customers, payment methods, charges, orchestrator,
  orders/preauth, transfers, recipients, senders, rates, virtual accounts, refunds, chargebacks,
  settlements, fees, wallets…)
- **Signals** for every money movement, **pluggable notifications**, optional **Celery**
- Django admin, management commands, system checks, `.env` configuration

## Why it's safe with money

| Rule | What it prevents |
| --- | --- |
| Every balance change happens in one place (the ledger) under a row lock (`SELECT … FOR UPDATE`), together with its ledger entry, `balance_after` and a unique per-wallet position | Double spending under concurrent requests; balances nobody can explain |
| Ledger entries form a **SHA-256 hash chain** per wallet; `manage.py verify_ledger` recomputes balances, positions and hashes | Silent edits or deletions of history, even by someone with database access |
| A database constraint makes negative balances impossible | Any bug elsewhere driving a wallet below zero |
| Webhooks are verified (`verif-hash` / `flutterwave-signature`, constant-time) **and every charge, transfer, bill and refund is re-fetched from the Flutterwave API before money moves** | Forged webhooks: even a leaked secret hash can't credit a wallet |
| Deposits are credited from the *verified* amount and currency, checked against what was expected; mismatches are flagged for review | Under-payments, tampered amounts, currency confusion |
| Withdrawals, bills and refunds debit **first**, commit, then call Flutterwave. Only a definitive rejection or a *verified* failure returns the money, exactly once. Unknown outcomes stay pending and are reconciled | Paying out twice; refunding money that actually left |
| Money-out operations refuse to run inside an outer `transaction.atomic()`, and the views opt out of `ATOMIC_REQUESTS` | A rollback erasing a debit after the payout already left |
| `Idempotency-Key` support on every money-moving endpoint, claimed *before* the work starts | Double charges when a mobile app retries |
| Per-user rate limits; PIN attempts are counted under a row lock with lockout | Phone-number enumeration, PIN/OTP brute force (including parallel guessing) |
| Cards are accepted **only** on Flutterwave's hosted checkout; every direct card path (v3 card charges, v4 card payment methods, card PIN/3DS authorisation) is refused with `CardDataNotAllowed` | Card data on your servers, and the PCI-DSS burden that comes with it |
| Card tokens are encrypted at rest (Fernet, rotatable keys); BVN/NIN are never stored | Damage from a database leak |
| Signals fire only after commit; every staff action is written to an audit log | "You've been paid" for money that rolled back; "who did this?" |

These are covered by 360+ tests, including race-condition tests that run on PostgreSQL
(parallel overspending, duplicate webhooks on v3 and v4, parallel refunds, parallel PIN guessing).

## Installation

```bash
pip install django-flutterwave-wallet            # core
pip install "django-flutterwave-wallet[celery]"  # + background processing
```

```python
# settings.py
from pathlib import Path
from flutterwave_wallet.conf import load_env_file

BASE_DIR = Path(__file__).resolve().parent.parent
load_env_file(BASE_DIR / '.env')          # optional; or use django-environ / real env vars

INSTALLED_APPS = [
    # ...
    'rest_framework',
    'djmoney',
    'flutterwave_wallet',
]
```

```python
# urls.py
urlpatterns = [
    # ...
    path('wallet/', include('flutterwave_wallet.urls')),
]
```

```bash
cp .env.example .env        # set FLUTTERWAVE_SECRET_KEY, FLUTTERWAVE_PUBLIC_KEY, FLUTTERWAVE_SECRET_HASH
python manage.py migrate
python manage.py sync_banks
python manage.py check      # the package validates its own configuration
```

To use **Flutterwave v4** for charges, virtual accounts, payouts and refunds, add your v4 client
credentials and switch the version (hosted checkout, saved cards, bills and BVN stay on v3):

```bash
FLW_WALLET_API_VERSION=v4
FLUTTERWAVE_CLIENT_ID=...
FLUTTERWAVE_CLIENT_SECRET=...
FLUTTERWAVE_V4_ENVIRONMENT=sandbox     # or production
```

On the Flutterwave dashboard (**Settings → Webhooks**), set the URL to
`https://your-domain.com/wallet/webhook/` and the **secret hash** to the same long random value
as `FLUTTERWAVE_SECRET_HASH`.

Every new user now gets a wallet automatically, and the API is live under `/wallet/api/`.

## A quick tour

```python
from flutterwave_wallet.services import WalletService

service = WalletService()
wallet = service.get_wallet(request.user)                  # default currency (FLW_WALLET_CURRENCY)
usd = service.get_wallet(request.user, 'USD')              # one wallet per currency

# Fund it: send the customer to checkout['link']
checkout = service.initialize_checkout(wallet, '5000', redirect_url='https://wallet.example/wallet/callback/')

# Or give the wallet a permanent account number (bank transfers credit it automatically)
account = service.create_static_account(wallet, bvn='12345678901')

# Send money by phone number, @tag, email or wallet id
service.set_phone_number(request.user, '0803 123 4567')
service.transfer(wallet, '08099998888', '1500', description='Lunch')
service.transfer(wallet, '@ada', '2000')

# Pay a seller, held in escrow until delivery
order = service.pay(buyer_wallet, '25000', merchant_wallet=seller_wallet, escrow=True)
service.release_payment(order)                      # or service.cancel_payment(order)

# Withdraw to a bank account (the name is verified with Flutterwave)
payout_account = service.add_bank_account(request.user, account_bank='044', account_number='0690000031')
service.withdraw(wallet, '10000', payout_account)

# Buy airtime from the balance
service.pay_bill(wallet, biller_code='BIL099', item_code='AT099', customer_id='08031234567', amount='500')

# Anything else Flutterwave offers, on either API version
from flutterwave_wallet.flutterwave import get_flutterwave_client
from flutterwave_wallet.flutterwave.v4 import get_flutterwave_v4_client
flw = get_flutterwave_client()
flw.payment_plans.create(name='Gold', amount=5000, interval='monthly', currency='NGN')
flw4 = get_flutterwave_v4_client()
flw4.transfer_rates.convert('NGN', 'USD', 1000)
flw4.orders.capture('ord_xxx')
```

React to money movement with signals:

```python
from django.dispatch import receiver
from flutterwave_wallet.signals import deposit_completed, transaction_needs_review

@receiver(deposit_completed)
def fulfil(sender, transaction, wallet, **kwargs):
    ...

@receiver(transaction_needs_review)
def page_ops(sender, transaction, wallet, reason, **kwargs):
    ...
```

## Use only what you need

```bash
FLW_WALLET_ALLOWED_CURRENCIES=NGN,USD
FLW_WALLET_ENABLE_WITHDRAWALS=false
FLW_WALLET_ENABLE_BILL_PAYMENTS=false
FLW_WALLET_ENABLE_VIRTUAL_ACCOUNTS=true
FLW_WALLET_ENABLE_FEES=true
FLW_WALLET_REQUIRE_TRANSACTION_PIN=true
FLW_WALLET_USE_CELERY=true
```

| Core (always there) | Pluggable (opt in / replace) |
| --- | --- |
| Ledger, wallets, hash chain, locking, idempotency | Notifications (`FLW_WALLET_NOTIFICATION_BACKENDS`) |
| Flutterwave client, webhook verification and re-verification | Fee pricing (`FLW_WALLET_FEE_RULES`, `FLW_WALLET_FEE_CALCULATOR`) |
| Reconciliation, fee accounting, signals | Phone normalisation, background processing (Celery) |
| | REST API permissions, URLs, admin, webhook forwarding |

## Scaling

- No global locks: each operation locks only the wallets it touches, always in primary-key order (no deadlocks).
- Platform fees are **accrued** and swept in batches, so a single revenue wallet never serialises traffic.
- Webhooks are stored and acknowledged immediately; with `FLW_WALLET_USE_CELERY=true` processing runs on
  your workers with retries and backoff.
- Reconciliation, sweeps and retries are safe to run on many workers at once (cache claims plus
  `SKIP LOCKED`; periodic tasks take a single-instance lock).
- Hot queries are indexed; statements are ordered by ledger position, not timestamps.

See [docs/operations.md](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/operations.md) for the Celery beat schedule, monitoring and runbooks.

## Documentation

- [Installation](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/installation.md)
- [Configuration & environment variables](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/configuration.md)
- [Usage guide](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/usage.md): every flow, end to end
- [REST API reference](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/api_reference.md)
- [Flutterwave clients](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/flutterwave_client.md): every v3 and v4 endpoint
- [Security](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/security.md): threat model and hardening checklist
- [Operations](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/operations.md): Celery, reconciliation, monitoring, scaling
- [Extending](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/docs/extending.md): signals, notifications, custom fees

## Development

```bash
pip install -e ".[dev]"
pytest                                     # SQLite
pytest --ds=tests.settings_postgres        # PostgreSQL: includes the race-condition tests
```

## License

MIT. See [LICENSE](https://github.com/NzeStan/Django-Flutterwave-Wallet/blob/main/LICENSE).
