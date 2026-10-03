# Security

## Threat model and defences

| Threat | Defence |
| --- | --- |
| **Forged webhook** (attacker posts "charge successful") | The signature is checked in constant time (v4 and v3: `flutterwave-signature` HMAC-SHA256; v3: `verif-hash`), and webhooks are refused while no secret hash is configured. Then **every object is re-fetched from the Flutterwave API** before money moves: the webhook body is only a hint. A leaked secret hash still cannot credit a wallet. |
| **Replayed webhook / Flutterwave retries** | Events are stored and de-duplicated by body hash. Settlement locks the transaction row and is idempotent: a deposit can be posted once and only once (DB-enforced ledger positions). |
| **Tampered amounts / currencies** | Deposits are credited from the verified `amount` and `currency` compared with what we expected. A mismatched currency is never credited; a mismatched amount is credited as received and flagged. The client can never set `amount`, `currency`, `tx_ref` or `meta` sent to Flutterwave. |
| **Double spending under concurrency** | `SELECT … FOR UPDATE` on wallet rows (in primary-key order), balance checks under the lock, daily limits under the lock, a DB `CHECK (balance >= 0)`, and a unique `(wallet, ledger_sequence)`. Race tests on PostgreSQL. |
| **Double payout on retries/timeouts** | Debit-then-call. v3 POSTs are never retried. v4 POSTs carry `X-Idempotency-Key` derived from the transaction reference, so a retry or a reconciliation re-send returns the original transfer instead of creating a second one. Unknown outcomes stay pending and are reconciled, never refunded blindly. `Idempotency-Key` on every money-moving endpoint of your API is claimed before the work starts. |
| **Rollback after payout** | Money-out services refuse to run inside an outer `transaction.atomic()`; the views opt out of `ATOMIC_REQUESTS`. |
| **Silent ledger tampering** (insider with DB access) | SHA-256 hash chain per wallet plus `verify_ledger` (positions, running balances, hashes). Admin balances and entries are read-only and undeletable. |
| **PIN / OTP brute force** | Per-user throttles. PIN attempts are counted under a row lock (parallel guesses can't bypass the lockout). Weak PINs are refused. PINs are hashed with Django's password hasher. |
| **Account takeover → payout to attacker** | Adding a payout account requires the PIN (when enabled). NG/GH account names are resolved by Flutterwave (a caller name is ignored). Payout accounts must match the wallet's owner and currency. |
| **Phone-number enumeration** | Lookup is throttled and returns masked data (`Ada O.`, `+234803****567`). |
| **Card data exposure / PCI-DSS scope** | Cards are accepted **only** on Flutterwave's hosted checkout. The v3 client has no card-charge method and refuses `card`/`applepay`/`googlepay` charge types. The v4 client refuses card payment methods and card PIN/3DS authorisations. The services and API refuse them too (`CardDataNotAllowed`). Card numbers, CVVs and PINs never touch your servers, keeping you in PCI-DSS SAQ A scope. Saved cards are charged by Flutterwave token only. |
| **Database leak** | Card tokens are encrypted (Fernet, rotatable keys). BVN and NIN are never stored. Card fingerprints are keyed hashes. |
| **v4 credential misuse** | OAuth tokens last 10 minutes and are refreshed automatically. The client secret is never logged or echoed, even when Flutterwave rejects it. Tokens are cached in Django's cache, so protect your Redis like any secret store. |
| **Secrets in logs** | Keys are never logged. `wallet_settings` masks them. Network errors don't echo hostnames. Provider errors are generic to clients unless definitive. |
| **CSV formula injection (admin exports)** | Cells starting with `= + - @` are neutralised. |
| **Open redirect from the callback** | Redirects only to `FLW_WALLET_CALLBACK_REDIRECT_URL` from settings. Query parameters are not trusted, and verification is rate-limited per deposit. |
| **SSRF via webhook forwarding** | Endpoints are staff-managed, https only (outside DEBUG), with no redirects followed. Deliveries are signed with a timestamp (`X-Wallet-Signature: sha256=…` over `"<timestamp>.<body>"`). |
| **Mass assignment / oversized input** | Every input is a bounded serializer. Metadata is limited to 4 KB of JSON objects. Webhook bodies are limited to 1 MB. |

## Hardening checklist

- [ ] Use PostgreSQL in production.
- [ ] Use a shared cache (Redis) for throttles and claims.
- [ ] Set a long random `FLUTTERWAVE_SECRET_HASH` (32+ chars) and the same value on the dashboard.
- [ ] Set a dedicated `FLW_WALLET_FIELD_ENCRYPTION_KEYS` (don't rely on `SECRET_KEY`). Rotate by prepending a new key.
- [ ] Keep `FLW_WALLET_VERIFY_WEBHOOKS_WITH_API = True`.
- [ ] Turn on `FLW_WALLET_REQUIRE_TRANSACTION_PIN` and `FLW_WALLET_REQUIRE_IDEMPOTENCY_KEY` for public apps.
- [ ] Set `FLW_WALLET_MAXIMUM_DAILY_TRANSACTION` / `MAXIMUM_TRANSACTION_AMOUNT` to match your risk appetite and KYC tiers.
- [ ] Only enable `FLW_WALLET_TRUST_X_FORWARDED_FOR` behind a proxy that overwrites the header.
- [ ] Route the `flutterwave_wallet.audit` logger to append-only storage.
- [ ] Alert on the `transaction_needs_review` signal and on `verify_ledger` failures.
- [ ] Run `manage.py check --deploy` in CI.
- [ ] Whitelist your server IPs on the Flutterwave dashboard and enable 2FA for dashboard users.
- [ ] Never "retry" a failed wallet withdrawal from the Flutterwave dashboard: the user was already refunded.

## Reporting a vulnerability

Please email nnamaniifeanyi10@gmail.com rather than opening a public issue.
