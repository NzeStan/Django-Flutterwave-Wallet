# Changelog

## 1.0.0

First release.

### Flutterwave v3 and v4

- A complete **v4 client** (`flutterwave_wallet.flutterwave.v4.FlutterwaveV4Client`):
  - OAuth client credentials, with the token shared across workers and refreshed automatically;
  - `X-Trace-Id` on every request and `X-Idempotency-Key` on every POST, so POSTs are safely retried;
  - `X-Scenario-Key` for sandbox testing;
  - endpoints: customers, payment methods, charges, orchestrator charges and orders, orders
    (capture/void), refunds, transfers (direct, recipient-based, retry/duplicate), transfer
    recipients, senders and rates, virtual accounts, banks, branches, account resolution, mobile
    networks, wallet resolution, statements and balances, settlements, chargebacks, fees and webhook resend.
- `FLW_WALLET_API_VERSION = 'v4'` routes direct charges, virtual accounts, withdrawals, refunds, bank
  lookups and balances through v4, via a gateway layer that gives both versions the same safety guarantees.
- Each transaction records its `api_version`, and webhooks from both versions are handled on one URL
  (including v4 `transfer.disburse` and `transfer.reversal`).

### Cards: hosted checkout only

- Card details never reach your server. Direct card charges, Apple/Google Pay direct charges and v4
  card payment methods or card PIN/3DS authorisations are refused (`CardDataNotAllowed`), and there is
  no card-encryption key setting.
- v3 virtual-card issuing was left out because it returns full card numbers to your server.
- Saved cards are charged by Flutterwave token only.

### Wallets and ledger

- Multi-currency wallets: one per user per currency (`FLW_WALLET_ALLOWED_CURRENCIES`).
  The tag, phone number, PIN and KYC status are shared on `WalletProfile`.
- One ledger service changes balances: row-locked, a ledger entry per change with
  `balance_after`, a unique per-wallet `ledger_sequence`, and a SHA-256 hash chain.
  `manage.py verify_ledger` recomputes everything.
- A database constraint prevents negative balances.

### Money in

- Hosted checkout for cards, and non-card direct charges: bank transfer, account debit, ACH, UK/EU,
  M-Pesa, mobile money (GH, RW, UG, TZ, ZM, francophone), USSD, NQR, eNaira, OPay, voucher, Capitec
  and Fawry, with OTP validation.
- Saved cards: tokens are encrypted at rest, de-duplicated by keyed fingerprint, and used for
  one-tap top-ups.
- Virtual accounts: a permanent account per wallet (BVN/NIN never stored) and one-off
  accounts per deposit.
- Every credit comes from data verified with the Flutterwave API. Currency and amount
  mismatches are flagged for review. A success that arrives after a failed attempt is still
  credited.

### Money out

- Withdrawals to bank accounts and mobile money, with account-name resolution.
- Debit-then-call. Only a definitive rejection or a verified failure refunds, exactly once.
  Unknown outcomes are reconciled. `force_reverse_withdrawal` is an audited escape hatch.
- Bill payments (airtime, data, cable, electricity...) with the same guarantees.
- Refunds of deposits back to the payer.
- Money-out services refuse to run inside an outer transaction, and the views opt out of
  `ATOMIC_REQUESTS`.

### Between wallets

- Transfers by ID, `@tag`, phone or email.
- Payments to the platform or a merchant, with optional escrow (release/cancel).
- Staff reversals.

### Fees

- Per-operation rules (percentage, flat, minimum, cap, bearer), Flutterwave's own transfer
  fee quote, or a custom calculator.
- Fees are accrued (`PlatformFee`) and swept in batches to a revenue wallet. Reversals void
  them, with a clawback if already swept.

### Platform

- The complete Flutterwave v3 client: payments, non-card charges, preauthorization, tokenized and
  bulk charges, transactions, refunds, virtual accounts, payment plans, subscriptions, collection
  and payout subaccounts, settlements, chargebacks, transfers, beneficiaries, bills, Remita, FX,
  OTPs, banks, verification and balances.
- Webhooks: constant-time `verif-hash` / `flutterwave-signature` verification, storage,
  de-duplication, API re-verification, retries, and signed forwarding.
- A REST API with idempotency keys, throttling, PIN enforcement and staff tools.
- Optional Celery with retries and single-instance periodic tasks; management commands for
  everything.
- Signals for every event, pluggable notifications, an admin with audited actions, and
  system checks.
- Supports Python 3.10–3.14 and Django 4.2–6.x. Race-condition tests run on PostgreSQL.
