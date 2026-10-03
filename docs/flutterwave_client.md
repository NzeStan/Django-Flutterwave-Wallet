# Flutterwave clients

Two clients wrap the whole Flutterwave API. The wallet uses them, and so can your own code.

```python
from flutterwave_wallet.flutterwave import FlutterwaveClient, get_flutterwave_client            # v3
from flutterwave_wallet.flutterwave.v4 import FlutterwaveV4Client, get_flutterwave_v4_client   # v4

flw = get_flutterwave_client()        # shared instance; settings are read lazily
flw4 = get_flutterwave_v4_client()
```

What both clients have in common:

- Methods return the response's `data`. Use `request(method, path, raw=True)` for the full body.
- `None` arguments are not sent, and extra keyword arguments pass through, so new Flutterwave
  parameters work without an upgrade.
- Path segments are URL-encoded.
- Errors raise `FlutterwaveAPIError`:
  - `is_definitive`: Flutterwave rejected the request, so it did not happen.
  - Otherwise the outcome is unknown: reconcile.
  - Also available: `is_not_found`, `status_code`, `flutterwave_message`.
- `paginate(path, params)` walks every page.
- **No card data.** Every way of sending card details directly is refused with
  `CardDataNotAllowed`:
  - v3: `card`, `applepay` and `googlepay` charges;
  - v4: `card`, `applepay` and `googlepay` payment methods, and `pin` / `external_3ds` authorisations.

  Take card payments on hosted checkout (`flw.payments.create`) and charge saved cards by token
  (`flw.tokenized_charges.charge`).

## v3 (`FlutterwaveClient`)

- **Auth**: `FLUTTERWAVE_SECRET_KEY` as a bearer token.
- **Base URL**: `https://api.flutterwave.com/v3`.
- **Retries**: only GET is retried (connection errors, 429, 5xx). POST and PUT never are.

| Attribute | Methods |
| --- | --- |
| `payments` | `create` (hosted checkout link: the way to accept cards) |
| `charges` | `create(type, data, raw)` (non-card), `bank_transfer`, `ussd`, `mpesa`, `mobile_money(network, ...)`, `nigerian_account`, `uk_eu_account`, `ach`, `voucher`, `enaira`, `nqr`, `opay`, `capitec`, `fawry_pay`, `validate(otp, flw_ref)` |
| `preauth` | `capture`, `void`, `refund` |
| `tokenized_charges` | `charge` (saved-card token), `bulk`, `bulk_status`, `bulk_transactions`, `update_card_token`, `tokenize_account`, `account_token`, `update_account_token` |
| `transactions` | `verify`, `verify_by_reference`, `list`, `timeline`, `refund`, `fee`, `resend_webhook` |
| `refunds` | `list`, `fetch` |
| `virtual_accounts` | `create`, `create_bulk`, `fetch_bulk`, `fetch`, `update_bvn`, `set_status`, `deactivate`, `reactivate` |
| `payment_plans` | `create`, `list`, `fetch`, `update`, `cancel` |
| `subscriptions` | `list`, `cancel`, `activate` |
| `subaccounts` (split payments) | `create`, `list`, `fetch`, `update`, `delete` |
| `settlements` | `list`, `fetch` |
| `chargebacks` | `list`, `fetch`, `respond`, `accept`, `decline`, `upload_proof` |
| `transfers` | `create`, `to_beneficiary`, `create_bulk`, `fee`, `list`, `fetch`, `retry`, `retries`, `fetch_bulk`, `rates` |
| `beneficiaries` | `create`, `list`, `fetch`, `delete` |
| `payout_subaccounts` | `create`, `list`, `fetch`, `update`, `delete`, `transactions`, `balance`, `static_account`, `refund_funding`, `funding_refunds` |
| `bills` | `categories`, `billers`, `items`, `validate_customer`, `pay`, `create_bulk`, `status`, `summary`, `history`, `legacy_categories`, `legacy_pay` |
| `remita` | `agencies`, `products`, `product_amount`, `create_order`, `update_order` |
| `fx` | `create_quote`, `quote`, `create_trade`, `trade` |
| `otps` | `create`, `validate` |
| `banks` | `list(country)`, `branches` |
| `verification` | `resolve_account`, `card_bin`, `bvn_consent`, `bvn_details` |
| `balances` | `all`, `currency`, `statement` |

v3 virtual-card *issuing* is intentionally not wrapped: it returns full card numbers to your server.

## v4 (`FlutterwaveV4Client`)

- **Auth**: OAuth 2.0 client credentials (`FLUTTERWAVE_CLIENT_ID`, `FLUTTERWAVE_CLIENT_SECRET`).
  - Tokens last 10 minutes.
  - The client caches them in process memory and in Django's cache (every worker shares one).
  - It refreshes them a minute before expiry, and once more if Flutterwave answers 401.
- **Base URL**: `FLUTTERWAVE_V4_ENVIRONMENT`:
  - `sandbox` → `https://developersandbox-api.flutterwave.com`
  - `production` → `https://f4bexperience.flutterwave.com`
  - or set `FLUTTERWAVE_V4_API_URL`.
- **Headers**: every request carries `X-Trace-Id`. Every POST carries `X-Idempotency-Key`, either
  yours (`idempotency_key=`) or one generated per call, so **POSTs are safely retried** on connection
  errors, 429 and 5xx. PUT and DELETE are never retried. Pass `scenario_key=` to send
  `X-Scenario-Key` (sandbox simulations).
- **Replays**: `request(..., raw=True)` adds `_idempotent_replay: True` when Flutterwave answered from
  its idempotency cache.

| Attribute | Methods |
| --- | --- |
| `customers` | `create`, `list`, `get`, `update`, `search(email)` |
| `payment_methods` | `create(type, customer_id, **details)` (mobile_money, ussd, bank_account, bank_transfer, opay), `list`, `get` |
| `charges` | `create`, `list` (filter by `reference`, `status`, `customer_id`...), `get`, `update`, `authorize_otp`, `authorize_avs` |
| `orchestration` | `direct_charge` (customer + method + charge in one call), `direct_order` |
| `orders` | `create`, `list`, `get`, `update`, `capture`, `void` (preauthorization) |
| `refunds` | `create(charge_id, amount, reason)`, `list`, `get` |
| `transfers` | `create` (to a saved recipient), `direct` (orchestrator, recipient inline), `list`, `get`, `update` (initiate/close deferred), `retry(action='retry'\|'duplicate')` |
| `transfer_recipients` | `create(type, ...)` (`bank_ngn`, `bank_usd`, `mobile_money_ghs`...), `list`, `get`, `delete` |
| `transfer_senders` | `create`, `list`, `get`, `delete` |
| `transfer_rates` | `convert(source, destination, amount)`, `get` |
| `virtual_accounts` | `create(customer_id, reference, currency, account_type)`, `list`, `get`, `update`, `deactivate`, `update_bvn` |
| `banks` | `list(country)`, `branches(bank_id)`, `resolve_account(currency, code, number)` |
| `mobile_networks` | `list(country)` |
| `wallets` | `resolve(identifier)`, `statement(currency)`, `balance(currency)`, `balances()` |
| `settlements` | `list`, `get` |
| `chargebacks` | `list`, `create`, `get`, `update`, `accept`, `decline` |
| `fees` | `get(amount, currency, payment_method)` |
| `webhooks` | `resend(id, type)` |

v4 webhooks:

- carry `type` (`charge.completed`, `transfer.disburse`, `transfer.reversal`, `refund.completed`,
  `order.authorization`);
- are signed with `flutterwave-signature`;
- are verified and processed on the same `/webhook/` URL as v3.

## Examples

```python
# v3: subscriptions - create a plan, then pass payment_plan to hosted checkout
plan = flw.payment_plans.create(name='Pro', amount=5000, interval='monthly', currency='NGN')

# v3: split a hosted checkout between you and a vendor
sub = flw.subaccounts.create(account_bank='044', account_number='0690000031', business_name='Vendor',
                             country='NG', split_value=0.8, business_mobile='08000000000', split_type='percentage')
service.initialize_checkout(wallet, '10000', subaccounts=[{'id': sub['subaccount_id']}])

# v4: pay a saved recipient later, on a schedule
recipient = flw4.transfer_recipients.create('bank_ngn', bank={'account_number': '0690000031', 'code': '044'})
flw4.transfers.create('payroll-2026-10-31-ada', action='scheduled',
                      disburse_option={'date_time': '2026-10-31 09:00:00', 'timezone': 'Africa/Lagos'},
                      payment_instruction={'source_currency': 'NGN',
                                           'amount': {'value': 50000, 'applies_to': 'destination_currency'},
                                           'recipient_id': recipient['id']})

# v4: authorize now, capture later (non-card methods)
order = flw4.orders.create('order-1001', 5000, 'NGN', customer_id='cus_x', payment_method_id='pmd_x')
flw4.orders.capture(order['id'])

# v4: FX
rate = flw4.transfer_rates.convert('NGN', 'USD', 100)
```

## Webhook helpers

```python
from flutterwave_wallet.flutterwave import verify_webhook, compute_signature
verify_webhook(request.body, request.headers)   # constant time; flutterwave-signature (v3/v4) or verif-hash (v3)
```
