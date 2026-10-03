# REST API reference

Prefix: wherever you include `flutterwave_wallet.urls`, plus `api/`. The examples assume
`/wallet/api/`.

- **Auth**: `FLW_WALLET_API_PERMISSION_CLASSES` (default `IsAuthenticated`) with your DRF
  authentication. Staff endpoints require `is_staff`.
- **Money**: amounts are strings with 2 decimals (`"5000.00"`); send strings or numbers.
- **Idempotency**: send `Idempotency-Key: <uuid>` on every money-moving POST. A retry with the
  same key and body returns the stored response with `Idempotent-Replayed: true`. The same key
  with a different body returns 422; a key still in progress returns 409.
- **PIN**: when `FLW_WALLET_REQUIRE_TRANSACTION_PIN` is on, money-out endpoints and adding a
  payout account need `"pin"`.
- **Errors**: `{"detail": "...", "code": "..."}`. The codes are:

  | Code | Meaning |
  | --- | --- |
  | `insufficient_funds` | Not enough balance |
  | `limit_exceeded` | Transaction or daily limit exceeded |
  | `wallet_locked` | Wallet is locked |
  | `pin_required` / `invalid_pin` / `pin_locked` | PIN missing, wrong, or locked out |
  | `recipient_not_found` | No wallet matches the recipient |
  | `feature_disabled` | The feature is switched off |
  | `duplicate_reference` | The reference was already used |
  | `card_data_not_allowed` (400) | Card details were sent; use hosted checkout |
  | `flutterwave_rejected` (400) | Flutterwave said no |
  | `flutterwave_error` (502) | Provider unreachable or outcome unknown |

## Wallets: `wallets/`

`{id}` is a wallet UUID or `me` (the default wallet; add `?currency=USD` for another).

| Method | Path | Body / query | Response |
| --- | --- | --- | --- |
| GET | `wallets/` | | the user's wallets |
| POST | `wallets/` | `{currency}` | opens a wallet (201) |
| GET | `wallets/{id}/` | | details, `daily_limit`, `daily_spent`, `virtual_account` |
| GET | `wallets/{id}/balance/` | | `{balance, currency, is_operational}` |
| GET | `wallets/{id}/transactions/` | `type, status, direction, start_date, end_date` | paginated |
| GET | `wallets/{id}/statement/` | `start_date, end_date` | posted entries in ledger order |
| POST | `wallets/{id}/checkout/` | `amount, email?, redirect_url?, reference?, payment_options?[], description?, metadata?` | `{link, reference, charge_amount, fee_breakdown, public_key}` |
| POST | `wallets/{id}/charge/` | `amount, charge_type, payload{}, reference?` | `{status, action, reference, transaction}`; 200 / 202 / 400 |
| POST | `wallets/{id}/validate-charge/` | `reference, otp` | same as `charge` |
| POST | `wallets/{id}/bank-transfer-deposit/` | `amount, reference?` | one-off account details |
| POST | `wallets/{id}/verify-deposit/` | `reference` | the deposit |
| POST | `wallets/{id}/charge-card/` | `amount, card_id?, reference?, pin?` | the deposit (200 / 202) |
| GET / POST | `wallets/{id}/virtual-account/` | POST `{bvn}` or `{nin}` | the permanent account |
| POST | `wallets/{id}/withdraw/` | `amount, bank_account_id?, narration?, reference?, pin?` | the withdrawal (202 until settled) |
| POST | `wallets/{id}/transfer/` | `recipient, amount, recipient_type?, description?, reference?, pin?` | debit leg (201) |
| POST | `wallets/{id}/pay/` | `amount, merchant?, escrow?, description?, reference?, pin?` | debit leg (201) |
| POST | `wallets/{id}/pay-bill/` | `biller_code, item_code, customer_id, amount, reference?, pin?` | bill payment |
| GET | `wallets/lookup/` | `recipient, recipient_type?, currency?` | masked recipient |
| POST | `wallets/fee-quote/` | `amount, operation, currency?` | fee breakdown |

`charge_type` is listed in [usage.md](usage.md#direct-charges-non-card) and depends on
`FLW_WALLET_API_VERSION`. Card types (`card`, `applepay`, `googlepay`) are rejected with 400: cards are
paid on hosted checkout only. Protected fields in `payload` (`amount`, `currency`, `tx_ref`, `meta`,
`redirect_url`) are ignored: the wallet sets them.

## Profile: `profile/`

| Method | Path | Body |
| --- | --- | --- |
| GET | `profile/` | |
| PATCH | `profile/` | `tag?, phone_number?` |
| POST | `profile/set-pin/` | `pin, current_pin?` |
| POST | `profile/verify-bvn/` | `bvn, firstname, lastname, redirect_url?` → `{url, reference}` |
| POST | `profile/refresh-kyc/` | |

## Transactions: `transactions/`

| Method | Path | Who |
| --- | --- | --- |
| GET | `transactions/`, `transactions/{id}/` | owner (`type, status, direction, currency` filters) |
| POST | `transactions/{id}/release/` | the buyer (or staff): release escrow |
| POST | `transactions/{id}/cancel/` | the merchant (or staff): cancel escrow and refund the buyer |
| POST | `transactions/{id}/refund/` | staff: `amount?, reason?` |
| POST | `transactions/{id}/reverse/` | staff: `reason?` |
| POST | `transactions/{id}/requery/` | staff |
| POST | `transactions/{id}/force-reverse/` | staff (withdrawals flagged for review) |
| GET | `transactions/review-queue/` | staff |

## Cards, banks, payout accounts, bills

| Method | Path | Notes |
| --- | --- | --- |
| GET / DELETE | `cards/`, `cards/{id}/` | tokens are never returned; DELETE wipes the token |
| POST | `cards/{id}/set-default/` | |
| GET | `banks/?country=NG` | |
| GET / POST | `bank-accounts/` | POST `account_bank, account_number, currency?, beneficiary_type?, account_name?, pin?` |
| DELETE | `bank-accounts/{id}/` | |
| POST | `bank-accounts/{id}/set-default/` | |
| POST | `bank-accounts/resolve/` | `account_bank, account_number` → name |
| GET | `bills/categories/?country=`, `bills/billers/?category=`, `bills/items/?biller_code=` | |
| POST | `bills/validate/` | `item_code, customer_id` |

## Staff: webhooks

| Method | Path |
| --- | --- |
| GET | `webhook-events/?event_type=&processed=` |
| POST | `webhook-events/{id}/reprocess/` |
| CRUD | `webhook-endpoints/` (https only; the secret is write-only) |
| POST | `webhook-endpoints/{id}/test/` |
| GET | `webhook-deliveries/` |
| POST | `webhook-deliveries/{id}/retry/` |

## Non-API endpoints

- `POST webhook/`: the Flutterwave webhook (no auth; verified by signature).
- `GET callback/`: the checkout return URL. It verifies server-side, then redirects to
  `FLW_WALLET_CALLBACK_REDIRECT_URL?tx_ref=&status=`, or answers with JSON when that is unset.
