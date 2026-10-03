# Usage guide

Everything goes through `WalletService` (or the focused services it combines). Amounts are
`Decimal` or strings, never floats (floats are refused). Flutterwave amounts are in major units,
so `'5000'` means NGN 5,000.

```python
from flutterwave_wallet.services import WalletService
service = WalletService()
```

## Wallets, profile, PIN

```python
wallet = service.get_wallet(user)              # default currency; created on first use
usd = service.get_wallet(user, 'USD')          # must be in FLW_WALLET_ALLOWED_CURRENCIES
service.get_wallets(user)

service.set_tag(user, 'ada')                   # others can pay @ada
service.set_phone_number(user, '0803 123 4567')    # stored as +2348031234567
service.set_pin(user, '1357')                  # weak PINs (1234, 0000...) are refused
service.set_pin(user, '2468', current_pin='1357')
service.verify_pin(user, '2468')               # counts failures, locks out after PIN_MAX_ATTEMPTS
```

The tag, phone number, PIN and KYC status live on `WalletProfile` and are shared by all of a
user's wallets.

## Deposits

Every deposit is a pending `Transaction` whose `reference` is Flutterwave's `tx_ref`. The
wallet is credited only after the charge is **verified with the Flutterwave API**, which
happens through the webhook, the callback view, `verify_deposit()`, or reconciliation.

### Hosted checkout (Flutterwave Standard)

```python
checkout = service.initialize_checkout(wallet, '5000', redirect_url='https://api.example.com/wallet/callback/')
redirect(checkout['link'])
```

Returns the link, the reference, `charge_amount` (including a customer-borne fee) and a
`fee_breakdown`.

### Cards: hosted checkout only

Card details never reach your server. Cards (and Apple Pay / Google Pay) are paid on Flutterwave's
hosted checkout (`initialize_checkout`). After a successful card payment the reusable token is saved, so
returning customers top up with `charge_saved_card`. Any attempt to send card data directly (a v3 `card`
charge, a v4 `card` payment method, a card PIN or 3DS authorisation) raises `CardDataNotAllowed`.

### Direct charges (non-card)

```python
result = service.direct_charge(wallet, '2000', 'mpesa', {'phone_number': '254700000000'})
result['status']   # 'success' | 'pending' | 'failed'
result['action']   # what the customer must do next, e.g.
                   # {'mode': 'redirect', 'redirect': 'https://...'}
                   # {'mode': 'otp', 'instruction': 'Enter the OTP...'}
                   # {'mode': 'banktransfer', 'transfer_account': '...', 'transfer_bank': '...', ...}
```

`charge_type` depends on the API version:

- **v3**: `bank_transfer`, `ach_payment`, `account`, `debit_uk_account`, `mpesa`,
  `mobile_money_ghana|rwanda|uganda|franco|tanzania|zambia`, `voucher_payment`, `enaira`, `ussd`, `qr`,
  `opay`, `capitec`, `fawry_pay`.
- **v4**: `mobile_money`, `ussd`, `bank_account`, `bank_transfer`, `opay`. The `payload` is the
  payment-method object, e.g. `{'network': 'MTN', 'country_code': '233', 'phone_number': '...'}`.

On v4 the charge goes through Flutterwave's orchestrator (customer, payment method and charge in one
idempotent call).

- **OTP step**: `service.validate_charge(result['transaction'], '123456')`.

### Saved cards

After a successful card deposit the reusable token is saved, encrypted, and de-duplicated per
card (`SAVE_CARDS`).

```python
card = user.flutterwave_cards.filter(is_active=True).first()
service.charge_saved_card(card, '7000')     # tokenized charge
service.set_default_card(card)
service.remove_card(card)                   # wipes the token
```

### Virtual accounts

```python
account = service.create_static_account(wallet, bvn='12345678901')   # or nin=; NGN/GHS only
account.account_number, account.bank_name                            # show this to the user

deposit = service.create_bank_transfer_deposit(wallet, '3000')          # one-off account, expires
```

Every transfer into a static account becomes its own deposit (`reference = VA-<flutterwave id>`),
credited exactly once. The BVN/NIN is sent to Flutterwave and never stored.

### What gets credited

The credit is the **verified** amount minus your platform fee (per its bearer), minus
Flutterwave's processing fee when `PROCESSING_FEE_BEARER='customer'`.

- Wrong currency: never credited. The deposit is marked failed and flagged for review.
- Paid a different amount than expected: what arrived is credited and the deposit is flagged.
- Late success after a failed attempt: still credited (money that arrived is never lost).

## Choosing v3 or v4

```python
FLW_WALLET_API_VERSION = 'v4'      # default 'v3'
```

| Operation | v3 | v4 |
| --- | --- | --- |
| Hosted checkout (cards) | yes | v3 is always used (v4 has no hosted checkout) |
| Saved-card top-ups | yes | v3 is always used |
| Non-card direct charges | `POST /charges?type=` | orchestrator `POST /orchestration/direct-charges` |
| Virtual accounts | `virtual-account-numbers` | `virtual-accounts` (linked to a v4 customer) |
| Withdrawals | `POST /transfers` | `POST /direct-transfers`, idempotent |
| Refunds | `POST /transactions/:id/refund` | `POST /refunds` |
| Bank lookups, bank lists, balances | yes | yes |
| Bills, BVN, Remita, payment plans, OTPs | yes | v3 is always used (not on v4) |

- Each transaction records its `api_version`. Verification, refunds and reconciliation always go back to
  the version that created it, so you can switch at any time.
- Webhooks from both versions are accepted on the same URL: v3 `event`/`verif-hash` and v4
  `type`/`flutterwave-signature`. Charges are re-verified with the API version their id belongs to.
- A user's v4 customer is created on first use and stored on `WalletProfile.flw_customer_id`.
- Deposits into a v4 static account are matched by that customer and the currency.
- v4 POSTs carry `X-Idempotency-Key`. A withdrawal whose outcome was unknown is resolved by
  re-sending the same request with the same key: Flutterwave answers with the original transfer.

## Withdrawals

```python
account = service.add_bank_account(user, account_bank='044', account_number='0690000031')
# NG/GH: the name is resolved with Flutterwave; a caller-supplied name is ignored
mm = service.add_mobile_money_account(user, 'MPS', '254700000000', 'KES', 'Ada Obi')

txn = service.withdraw(wallet, '10000', account, narration='Payout')
txn.status        # 'processing' until transfer.completed (re-verified) settles it
```

- A definitive rejection refunds immediately.
- A verified `FAILED` refunds exactly once.
- An unknown outcome stays pending with `needs_review=True` and is reconciled.
- If Flutterwave has no record after an hour, the withdrawal is flagged. After you confirm on the
  dashboard that it never left, call `force_reverse_withdrawal(txn, reason, performed_by=staff)`.

## Transfers, payments, escrow

```python
service.transfer(wallet, '@bola', '1500')
service.transfer(wallet, '08031234567', '1500')
service.transfer(wallet, 'bola@example.com', '1500', lookup='email')
service.describe_recipient(service.resolve_recipient('@bola', 'NGN'))   # masked, for confirmation

service.pay(wallet, '2500')                                  # platform is the merchant
service.pay(buyer, '2500', merchant_wallet=seller)           # marketplace, instant
held = service.pay(buyer, '2500', merchant_wallet=seller, escrow=True)
service.release_payment(held)  # or service.cancel_payment(held, reason='Not delivered')

service.reverse_transaction(debit_txn, reason='Fraud', performed_by=staff_user)   # staff
```

## Bills

```python
service.bill_categories('NG')
service.billers('AIRTIME')
service.biller_items('BIL099')
service.validate_bill_customer('AT099', '08031234567')
txn = service.pay_bill(wallet, 'BIL099', 'AT099', '08031234567', '500')
txn.metadata.get('token')       # e.g. electricity token, when the biller returns one
```

## Refunds

```python
refund = service.refund_deposit(deposit, '2000', reason='Customer request', performed_by=staff)
service.refundable_amount(deposit)
```

## KYC (BVN)

```python
result = service.start_bvn_verification(user, '12345678901', 'Ada', 'Obi', redirect_url=...)
redirect(result['url'])        # customer consents; `bvn.completed` webhook updates the profile
user.wallet_profile.kyc_status # 'verified'
```

## Staff adjustments and integrity

```python
service.credit_wallet(wallet, '500', description='Cash-back', performed_by=staff)   # audited
service.debit_wallet(wallet, '100', description='Chargeback', performed_by=staff)
service.verify_ledger(wallet)        # [] when the books are clean
service.get_statement(wallet)        # posted entries in ledger order
service.flutterwave_balances()       # compare with the sum of wallet balances
```

## Everything else Flutterwave offers

On v3: payment plans, subscriptions, split payments (`subaccounts`), payout subaccounts,
preauthorization, bulk transfers, bulk tokenized charges, FX, settlements, chargebacks, OTPs and Remita.
On v4: orders (authorize and capture), transfer recipients and senders, rates, settlements,
chargebacks, fees and wallet statements. See [flutterwave_client.md](flutterwave_client.md).
