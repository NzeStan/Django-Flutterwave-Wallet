"""
Flutterwave v3 API resources.

Each class maps one section of https://developer.flutterwave.com/reference to
methods. Amounts are in **major units** (``500`` is NGN 500). Optional arguments
left as ``None`` are not sent, and any extra keyword arguments are passed through
unchanged - so parameters Flutterwave adds later work without a package update.
For anything not wrapped here use ``client.get/post/put/delete`` directly.
"""
from urllib.parse import quote


def _p(value):
    """URL-encode a path segment (prevents path traversal through user input)."""
    return quote(str(value), safe='')


class Resource:
    def __init__(self, client):
        self.client = client

    def _get(self, path, **params):
        return self.client.request('GET', path, params=params)

    def _post(self, path, data=None, params=None):
        return self.client.request('POST', path, params=params, json=data or {})

    def _put(self, path, data=None):
        return self.client.request('PUT', path, json=data or {})

    def _delete(self, path, data=None):
        return self.client.request('DELETE', path, json=data)


# ======================================================================
# Collections
# ======================================================================

class Payments(Resource):
    """Flutterwave Standard (hosted checkout). Returns ``{'link': ...}``."""

    def create(self, tx_ref, amount, currency, redirect_url, customer, customizations=None, meta=None,
               payment_options=None, session_duration=None, max_retry_attempt=None, subaccounts=None,
               payment_plan=None, payment_link=None, **extra):
        return self._post('payments', {
            'tx_ref': tx_ref, 'amount': amount, 'currency': currency, 'redirect_url': redirect_url,
            'customer': customer, 'customizations': customizations, 'meta': meta,
            'payment_options': payment_options, 'session_duration': session_duration,
            'max_retry_attempt': max_retry_attempt, 'subaccounts': subaccounts, 'payment_plan': payment_plan,
            **extra,
        })


class Charges(Resource):
    """
    Direct charges: ``POST /charges?type=<type>`` (bank transfer, USSD, M-Pesa, mobile money, ...).

    Card-based charges (``card``, ``applepay``, ``googlepay``) are refused by design: this package
    keeps card data off your servers. Take card payments with hosted checkout (:class:`Payments`)
    and charge saved cards by token (:class:`TokenizedCharges`).
    """

    def create(self, type, data, raw=False):
        """Use ``raw=True`` to also get ``meta.authorization`` (OTP, redirect, transfer details...)."""
        from flutterwave_wallet.constants import CARD_CHARGE_TYPES
        from flutterwave_wallet.exceptions import CardDataNotAllowed

        if str(type).lower() in CARD_CHARGE_TYPES:
            raise CardDataNotAllowed()
        return self.client.request('POST', 'charges', params={'type': type}, json=data or {}, raw=raw)

    # Convenience wrappers for common charge types
    def bank_transfer(self, tx_ref, amount, email, currency='NGN', **extra):
        return self.create('bank_transfer', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                             'currency': currency, **extra})

    def ussd(self, tx_ref, amount, email, account_bank, currency='NGN', **extra):
        return self.create('ussd', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                    'account_bank': account_bank, 'currency': currency, **extra})

    def mpesa(self, tx_ref, amount, email, phone_number, currency='KES', **extra):
        return self.create('mpesa', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                     'phone_number': phone_number, 'currency': currency, **extra})

    def mobile_money(self, network_type, tx_ref, amount, email, phone_number, currency, **extra):
        """``network_type``: ghana, rwanda, uganda, franco, tanzania, zambia."""
        return self.create(f'mobile_money_{network_type}', {
            'tx_ref': tx_ref, 'amount': amount, 'email': email, 'phone_number': phone_number,
            'currency': currency, **extra,
        })

    def nigerian_account(self, tx_ref, amount, email, account_bank, account_number, currency='NGN', **extra):
        return self.create('account', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                       'account_bank': account_bank, 'account_number': account_number,
                                       'currency': currency, **extra})

    def uk_eu_account(self, tx_ref, amount, email, currency='GBP', **extra):
        return self.create('debit_uk_account', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                                'currency': currency, **extra})

    def ach(self, tx_ref, amount, email, currency='USD', **extra):
        return self.create('ach_payment', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                           'currency': currency, **extra})

    def voucher(self, tx_ref, amount, email, pin, currency='ZAR', **extra):
        return self.create('voucher_payment', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                               'pin': pin, 'currency': currency, **extra})

    def enaira(self, tx_ref, amount, email, currency='NGN', **extra):
        return self.create('enaira', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                      'currency': currency, **extra})

    def nqr(self, tx_ref, amount, email, currency='NGN', **extra):
        return self.create('qr', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                  'currency': currency, **extra})

    def opay(self, tx_ref, amount, email, currency='NGN', **extra):
        return self.create('opay', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                    'currency': currency, **extra})

    def capitec(self, tx_ref, amount, email, currency='ZAR', **extra):
        return self.create('capitec', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                       'currency': currency, **extra})

    def fawry_pay(self, tx_ref, amount, email, currency='EGP', **extra):
        return self.create('fawry_pay', {'tx_ref': tx_ref, 'amount': amount, 'email': email,
                                         'currency': currency, **extra})


    def validate(self, otp, flw_ref, type=None, raw=False):
        """Complete a card/account charge that needs an OTP."""
        return self.client.request('POST', 'validate-charge',
                                   json={'otp': str(otp), 'flw_ref': flw_ref, 'type': type}, raw=raw)


class Preauthorization(Resource):
    """Card preauthorization (hold, then capture/void/refund)."""

    def capture(self, flw_ref, amount=None):
        return self._post(f'charges/{_p(flw_ref)}/capture', {'amount': amount})

    def void(self, flw_ref):
        return self._post(f'charges/{_p(flw_ref)}/void')

    def refund(self, flw_ref, amount=None):
        return self._post(f'charges/{_p(flw_ref)}/refund', {'amount': amount})


class TokenizedCharges(Resource):
    """Charge saved card tokens and bank-account mandates."""

    def charge(self, token, email, amount, currency, tx_ref, country=None, first_name=None, last_name=None,
               narration=None, ip=None, **extra):
        return self._post('tokenized-charges', {
            'token': token, 'email': email, 'amount': amount, 'currency': currency, 'tx_ref': tx_ref,
            'country': country, 'first_name': first_name, 'last_name': last_name, 'narration': narration,
            'ip': ip, **extra,
        })

    def bulk(self, title, retry_strategy, bulk_data, **extra):
        return self._post('bulk-tokenized-charges', {'title': title, 'retry_strategy': retry_strategy,
                                                     'bulk_data': bulk_data, **extra})

    def bulk_status(self, bulk_id):
        return self._get(f'bulk-tokenized-charges/{_p(bulk_id)}')

    def bulk_transactions(self, bulk_id):
        return self._get(f'bulk-tokenized-charges/{_p(bulk_id)}/transactions')

    def update_card_token(self, token, email=None, full_name=None, phone_number=None, **extra):
        return self._put(f'tokens/{_p(token)}', {'email': email, 'full_name': full_name,
                                                  'phone_number': phone_number, **extra})

    def tokenize_account(self, account_bank, account_number, address, amount, email, phone_number, end_date,
                         start_date=None, narration=None, **extra):
        return self._post('accounts/tokenize', {
            'account_bank': account_bank, 'account_number': account_number, 'address': address,
            'amount': amount, 'email': email, 'phone_number': phone_number, 'end_date': end_date,
            'start_date': start_date, 'narration': narration, **extra,
        })

    def account_token(self, reference):
        return self._get(f'accounts/token/{_p(reference)}')

    def update_account_token(self, reference, status):
        """``status``: ACTIVE, SUSPENDED or DELETED."""
        return self._put(f'accounts/token/{_p(reference)}', {'status': status})


class Transactions(Resource):

    def verify(self, transaction_id):
        """The source of truth for a payment. Always verify before giving value."""
        return self._get(f'transactions/{_p(transaction_id)}/verify')

    def verify_by_reference(self, tx_ref):
        return self._get('transactions/verify_by_reference', tx_ref=tx_ref)

    def list(self, **params):
        """Filters: from, to, page, customer_email, status, tx_ref, customer_fullname, currency."""
        return self._get('transactions', **params)

    def timeline(self, transaction_id):
        return self._get(f'transactions/{_p(transaction_id)}/events')

    def refund(self, transaction_id, amount=None, comments=None, **extra):
        return self._post(f'transactions/{_p(transaction_id)}/refund',
                          {'amount': amount, 'comments': comments, **extra})

    def fee(self, amount, currency, payment_type=None, card_first6digits=None, **params):
        return self._get('transactions/fee', amount=amount, currency=currency, payment_type=payment_type,
                         card_first6digits=card_first6digits, **params)

    def resend_webhook(self, transaction_id, wait=None):
        return self._post(f'transactions/{_p(transaction_id)}/resend-hook', {'wait': wait})


class Refunds(Resource):

    def list(self, **params):
        """Filters: from, to, page, currency, id, status."""
        return self._get('refunds', **params)

    def fetch(self, refund_id):
        return self._get(f'refunds/{_p(refund_id)}')


class VirtualAccounts(Resource):

    def create(self, email, currency='NGN', amount=None, tx_ref=None, is_permanent=None, bvn=None, nin=None,
               firstname=None, lastname=None, phonenumber=None, narration=None, expires=None, frequency=None,
               bank_code=None, **extra):
        return self._post('virtual-account-numbers', {
            'email': email, 'currency': currency, 'amount': amount, 'tx_ref': tx_ref, 'is_permanent': is_permanent,
            'bvn': bvn, 'nin': nin, 'firstname': firstname, 'lastname': lastname, 'phonenumber': phonenumber,
            'narration': narration, 'expires': expires, 'frequency': frequency, 'bank_code': bank_code, **extra,
        })

    def create_bulk(self, batch_ref, bulk_data, **extra):
        return self._post('bulk-virtual-account-numbers', {'batch_ref': batch_ref, 'bulk_data': bulk_data,
                                                           **extra})

    def fetch_bulk(self, batch_id):
        return self._get(f'bulk-virtual-account-numbers/{_p(batch_id)}')

    def fetch(self, order_ref):
        return self._get(f'virtual-account-numbers/{_p(order_ref)}')

    def update_bvn(self, order_ref, bvn):
        return self._put(f'virtual-account-numbers/{_p(order_ref)}', {'bvn': bvn})

    def set_status(self, order_ref, status):
        """``status``: 'inactive' to deactivate, 'active' to reactivate."""
        return self._post(f'virtual-account-numbers/{_p(order_ref)}', {'status': status})

    def deactivate(self, order_ref):
        return self.set_status(order_ref, 'inactive')

    def reactivate(self, order_ref):
        return self.set_status(order_ref, 'active')


class PaymentPlans(Resource):

    def create(self, name, amount, interval, currency=None, duration=None, **extra):
        """``interval``: daily, weekly, monthly, quarterly, bi-annually, yearly."""
        return self._post('payment-plans', {'name': name, 'amount': amount, 'interval': interval,
                                            'currency': currency, 'duration': duration, **extra})

    def list(self, **params):
        return self._get('payment-plans', **params)

    def fetch(self, plan_id):
        return self._get(f'payment-plans/{_p(plan_id)}')

    def update(self, plan_id, name=None, status=None, **extra):
        return self._put(f'payment-plans/{_p(plan_id)}', {'name': name, 'status': status, **extra})

    def cancel(self, plan_id):
        return self._put(f'payment-plans/{_p(plan_id)}/cancel')


class Subscriptions(Resource):

    def list(self, **params):
        """Filters: email, transaction_id, plan, subscribed_from, subscribed_to, status, page."""
        return self._get('subscriptions', **params)

    def cancel(self, subscription_id):
        return self._put(f'subscriptions/{_p(subscription_id)}/cancel')

    def activate(self, subscription_id):
        return self._put(f'subscriptions/{_p(subscription_id)}/activate')


class Subaccounts(Resource):
    """Collection subaccounts (split payments)."""

    def create(self, account_bank, account_number, business_name, country, split_value, business_mobile,
               business_email=None, business_contact=None, business_contact_mobile=None, split_type=None,
               meta=None, **extra):
        return self._post('subaccounts', {
            'account_bank': account_bank, 'account_number': account_number, 'business_name': business_name,
            'country': country, 'split_value': split_value, 'business_mobile': business_mobile,
            'business_email': business_email, 'business_contact': business_contact,
            'business_contact_mobile': business_contact_mobile, 'split_type': split_type, 'meta': meta, **extra,
        })

    def list(self, **params):
        return self._get('subaccounts', **params)

    def fetch(self, subaccount_id):
        return self._get(f'subaccounts/{_p(subaccount_id)}')

    def update(self, subaccount_id, **data):
        return self._put(f'subaccounts/{_p(subaccount_id)}', data)

    def delete(self, subaccount_id):
        return self._delete(f'subaccounts/{_p(subaccount_id)}')


class Settlements(Resource):

    def list(self, **params):
        """Filters: page, subaccount_id, from, to."""
        return self._get('settlements', **params)

    def fetch(self, settlement_id, **params):
        return self._get(f'settlements/{_p(settlement_id)}', **params)


class Chargebacks(Resource):

    def list(self, **params):
        """Filters: page, status, from, to, flw_ref."""
        return self._get('chargebacks', **params)

    def fetch(self, flw_ref):
        return self._get('chargebacks', flw_ref=flw_ref)

    def respond(self, chargeback_id, action, comment, proof=None):
        """``action``: 'accept' or 'decline'. ``proof`` is a URL from :meth:`upload_proof`."""
        return self._put(f'chargebacks/{_p(chargeback_id)}', {'action': action, 'comment': comment,
                                                               'proof': proof})

    def accept(self, chargeback_id, comment):
        return self.respond(chargeback_id, 'accept', comment)

    def decline(self, chargeback_id, comment, proof=None):
        return self.respond(chargeback_id, 'decline', comment, proof)

    def upload_proof(self, file_obj, filename='proof.png', content_type='image/png'):
        """Upload an image proof; returns its URL."""
        return self.client.request('POST', 'upload_image', files={'image': (filename, file_obj, content_type)})


# ======================================================================
# Payouts
# ======================================================================

class Transfers(Resource):

    def create(self, account_bank, account_number, amount, currency, reference=None, narration=None,
               debit_currency=None, beneficiary_name=None, callback_url=None, meta=None, destination_branch_code=None,
               **extra):
        return self._post('transfers', {
            'account_bank': account_bank, 'account_number': account_number, 'amount': amount, 'currency': currency,
            'reference': reference, 'narration': narration, 'debit_currency': debit_currency,
            'beneficiary_name': beneficiary_name, 'callback_url': callback_url, 'meta': meta,
            'destination_branch_code': destination_branch_code, **extra,
        })

    def to_beneficiary(self, beneficiary, amount, currency, reference=None, narration=None, **extra):
        return self._post('transfers', {'beneficiary': beneficiary, 'amount': amount, 'currency': currency,
                                        'reference': reference, 'narration': narration, **extra})

    def create_bulk(self, bulk_data, title=None, **extra):
        return self._post('bulk-transfers', {'title': title, 'bulk_data': bulk_data, **extra})

    def fee(self, amount, currency, type='account'):
        """``type``: account, mobilemoney, barter."""
        return self._get('transfers/fee', amount=amount, currency=currency, type=type)

    def list(self, **params):
        """Filters: page, status, reference."""
        return self._get('transfers', **params)

    def fetch(self, transfer_id):
        return self._get(f'transfers/{_p(transfer_id)}')

    def retry(self, transfer_id):
        return self._post(f'transfers/{_p(transfer_id)}/retries')

    def retries(self, transfer_id):
        return self._get(f'transfers/{_p(transfer_id)}/retries')

    def fetch_bulk(self, batch_id):
        return self._get('transfers', batch_id=batch_id)

    def rates(self, amount, destination_currency, source_currency):
        return self._get('transfers/rates', amount=amount, destination_currency=destination_currency,
                         source_currency=source_currency)


class Beneficiaries(Resource):

    def create(self, account_bank, account_number, beneficiary_name=None, currency=None, bank_name=None, **extra):
        return self._post('beneficiaries', {'account_bank': account_bank, 'account_number': account_number,
                                            'beneficiary_name': beneficiary_name, 'currency': currency,
                                            'bank_name': bank_name, **extra})

    def list(self, **params):
        return self._get('beneficiaries', **params)

    def fetch(self, beneficiary_id):
        return self._get(f'beneficiaries/{_p(beneficiary_id)}')

    def delete(self, beneficiary_id):
        return self._delete(f'beneficiaries/{_p(beneficiary_id)}')


class PayoutSubaccounts(Resource):
    """Payout subaccounts (wallets held at Flutterwave, each with a NUBAN)."""

    def create(self, account_name, email, country, mobilenumber=None, **extra):
        return self._post('payout-subaccounts', {'account_name': account_name, 'email': email,
                                                 'country': country, 'mobilenumber': mobilenumber, **extra})

    def list(self, **params):
        return self._get('payout-subaccounts', **params)

    def fetch(self, account_reference):
        return self._get(f'payout-subaccounts/{_p(account_reference)}')

    def update(self, account_reference, account_name=None, email=None, mobilenumber=None, country=None, **extra):
        return self._put(f'payout-subaccounts/{_p(account_reference)}', {
            'account_name': account_name, 'email': email, 'mobilenumber': mobilenumber, 'country': country,
            **extra,
        })

    def delete(self, account_reference):
        return self._delete(f'payout-subaccounts/{_p(account_reference)}')

    def transactions(self, account_reference, from_date, to_date, currency=None, page=None, fetch_limit=None):
        return self._get(f'payout-subaccounts/{_p(account_reference)}/transactions', **{
            'from': from_date, 'to': to_date, 'currency': currency, 'page': page, 'fetch_limit': fetch_limit,
        })

    def balance(self, account_reference, currency=None):
        return self._get(f'payout-subaccounts/{_p(account_reference)}/balances', currency=currency)

    def static_account(self, account_reference, currency=None, verbose=None):
        return self._get(f'payout-subaccounts/{_p(account_reference)}/static-account', currency=currency,
                         verbose=verbose)

    def refund_funding(self, funding_reference, debit_currency=None):
        """Reverse a wallet funding (``data.response`` of the funding webhook)."""
        return self._post('payout-subaccounts/refunds', {'funding_reference': funding_reference,
                                                         'debit_currency': debit_currency})

    def funding_refunds(self, **params):
        """Filters: status, from, to, reference."""
        return self._get('payout-subaccounts/refunds', **params)


# ======================================================================
# Value-added services
# ======================================================================

class Bills(Resource):
    """Airtime, data, cable TV, electricity, internet and more."""

    def categories(self, country=None):
        return self._get('top-bill-categories', country=country)

    def billers(self, category, country=None):
        return self._get(f'bills/{_p(category)}/billers', country=country)

    def items(self, biller_code):
        return self._get(f'billers/{_p(biller_code)}/items')

    def validate_customer(self, item_code, customer, **params):
        return self._get(f'bill-items/{_p(item_code)}/validate', customer=customer, **params)

    def pay(self, biller_code, item_code, customer_id, amount, country, reference=None, callback_url=None,
            **extra):
        return self._post(f'billers/{_p(biller_code)}/items/{_p(item_code)}/payment', {
            'customer_id': customer_id, 'amount': amount, 'country': country, 'reference': reference,
            'callback_url': callback_url, **extra,
        })

    def create_bulk(self, bulk_reference, callback_url, bulk_data, **extra):
        return self._post('bulk-bills', {'bulk_reference': bulk_reference, 'callback_url': callback_url,
                                         'bulk_data': bulk_data, **extra})

    def status(self, reference, verbose=None):
        return self._get(f'bills/{_p(reference)}', verbose=verbose)

    def summary(self, from_date=None, to_date=None, **params):
        return self._get('bills/summary', **{'from': from_date, 'to': to_date, **params})

    def history(self, **params):
        """Filters: status, from, to, page, limit, reference, batch_reference, category, country."""
        return self._get('bills/history', **params)

    # Older v3 bill endpoints (still served for existing integrations)
    def legacy_categories(self, **params):
        return self._get('bill-categories', **params)

    def legacy_pay(self, country, customer, amount, type, reference, recurrence='ONCE', biller_name=None, **extra):
        return self._post('bills', {'country': country, 'customer': customer, 'amount': amount, 'type': type,
                                    'reference': reference, 'recurrence': recurrence, 'biller_name': biller_name,
                                    **extra})


class Remita(Resource):
    """Government agency payments (Remita)."""

    def agencies(self):
        return self._get('billers')

    def products(self, biller_code):
        return self._get(f'billers/{_p(biller_code)}/products')

    def product_amount(self, biller_code, product_code):
        return self._get(f'billers/{_p(biller_code)}/products/{_p(product_code)}')

    def create_order(self, biller_code, product_code, customer, fields, amount=None, reference=None, country=None,
                     **extra):
        return self._post(f'billers/{_p(biller_code)}/products/{_p(product_code)}/orders', {
            'customer': customer, 'fields': fields, 'amount': amount, 'reference': reference, 'country': country,
            **extra,
        })

    def update_order(self, order_reference, amount, reference=None):
        return self._put(f'product-orders/{_p(order_reference)}', {'amount': amount, 'reference': reference})


class FX(Resource):
    """Foreign-exchange trades: request a quote, then execute it."""

    def create_quote(self, base_currency, target_currency, quantity, reference=None):
        return self._post('rfq', {'base_currency': base_currency, 'target_currency': target_currency,
                                  'quantity': str(quantity), 'reference': reference})

    def quote(self, quote_id):
        return self._get(f'rfq/{_p(quote_id)}')

    def create_trade(self, quote_id, narration, meta=None):
        return self._post('trades', {'quote_id': quote_id, 'narration': narration, 'meta': meta})

    def trade(self, trade_id):
        return self._get(f'trade/{_p(trade_id)}')


class OTPs(Resource):

    def create(self, customer, sender, medium, length=6, send=True, expiry=None):
        """``medium``: list of 'email', 'sms', 'whatsapp'. ``customer``: {name, email, phone}."""
        return self._post('otps', {'customer': customer, 'sender': sender, 'medium': list(medium),
                                   'length': length, 'send': send, 'expiry': expiry})

    def validate(self, reference, otp):
        return self._post(f'otps/{_p(reference)}/validate', {'otp': otp})


# ======================================================================
# Reference data, KYC and balances
# ======================================================================

class Banks(Resource):

    def list(self, country='NG'):
        return self._get(f'banks/{_p(country)}')

    def branches(self, bank_id):
        return self._get(f'banks/{_p(bank_id)}/branches')


class Verification(Resource):

    def resolve_account(self, account_number, account_bank):
        return self._post('accounts/resolve', {'account_number': account_number, 'account_bank': account_bank})

    def card_bin(self, bin):
        return self._get(f'card-bins/{_p(bin)}')

    def bvn_consent(self, bvn, firstname, lastname, redirect_url=None):
        """Start BVN verification. Returns ``{url, reference}`` - send the customer to ``url``."""
        return self._post('bvn/verifications', {'bvn': bvn, 'firstname': firstname, 'lastname': lastname,
                                                'redirect_url': redirect_url})

    def bvn_details(self, reference, include_complete_message=None):
        return self._get(f'bvn/verifications/{_p(reference)}', include_complete_message=include_complete_message)


class Balances(Resource):

    def all(self):
        return self._get('balances')

    def currency(self, currency):
        return self._get(f'balances/{_p(currency)}')

    def statement(self, from_date, to_date, currency, **params):
        """Filters: type (C/D), page, include_transaction_type, include_payout_id, payout_ref, payout_id."""
        return self._get('wallet/statement', **{'from': from_date, 'to': to_date, 'currency': currency, **params})
