"""
Flutterwave v4 API resources (https://developer.flutterwave.com/v4.0.0/reference).

Amounts are decimals in major units. ``None`` arguments are not sent; extra keyword
arguments pass through. POST methods accept ``idempotency_key=`` (generated if omitted) and
``scenario_key=`` (sandbox simulations).

Card payment methods (and card PIN / 3DS authorizations) are refused by design: card data
must never reach your server. Take cards on Flutterwave's hosted checkout instead.
"""
from urllib.parse import quote

from flutterwave_wallet.exceptions import CardDataNotAllowed

CARD_METHOD_TYPES = {'card', 'applepay', 'googlepay'}
CARD_AUTHORIZATION_TYPES = {'pin', 'external_3ds'}


def _p(value):
    return quote(str(value), safe='')


def _refuse_card_method(payment_method_type):
    if str(payment_method_type or '').lower() in CARD_METHOD_TYPES:
        raise CardDataNotAllowed()


def _refuse_card_authorization(authorization):
    if isinstance(authorization, dict) and str(authorization.get('type') or '').lower() in CARD_AUTHORIZATION_TYPES:
        raise CardDataNotAllowed()


class Resource:
    def __init__(self, client):
        self.client = client

    def _get(self, path, **params):
        return self.client.request('GET', path, params=params)

    def _post(self, path, data=None, idempotency_key=None, scenario_key=None, params=None):
        return self.client.request('POST', path, params=params, json=data or {}, idempotency_key=idempotency_key,
                                   scenario_key=scenario_key)

    def _put(self, path, data=None, scenario_key=None):
        return self.client.request('PUT', path, json=data or {}, scenario_key=scenario_key)

    def _delete(self, path):
        return self.client.request('DELETE', path)


class Customers(Resource):

    def create(self, email, name=None, phone=None, address=None, meta=None, idempotency_key=None, **extra):
        """``name``: {first, middle, last}; ``phone``: {country_code, number}; ``address``: {line1, city, ...}."""
        return self._post('customers', {'email': email, 'name': name, 'phone': phone, 'address': address,
                                        'meta': meta, **extra}, idempotency_key)

    def list(self, **params):
        return self._get('customers', **params)

    def get(self, customer_id):
        return self._get(f'customers/{_p(customer_id)}')

    def update(self, customer_id, **data):
        return self._put(f'customers/{_p(customer_id)}', data)

    def search(self, email, page=None, size=None):
        return self._post('customers/search', {'email': email}, params={'page': page, 'size': size})


class PaymentMethods(Resource):
    """Non-card payment methods: mobile_money, ussd, bank_account, bank_transfer, opay."""

    def create(self, type, customer_id=None, idempotency_key=None, scenario_key=None, **details):
        """``details`` is the object for ``type``, e.g. ``mobile_money={'network': 'MTN', ...}``."""
        _refuse_card_method(type)
        return self._post('payment-methods', {'type': type, 'customer_id': customer_id, **details},
                          idempotency_key, scenario_key)

    def list(self, **params):
        return self._get('payment-methods', **params)

    def get(self, payment_method_id):
        return self._get(f'payment-methods/{_p(payment_method_id)}')


class Charges(Resource):

    def create(self, reference, amount, currency, customer_id, payment_method_id, redirect_url=None, meta=None,
               authorization=None, idempotency_key=None, scenario_key=None, **extra):
        _refuse_card_authorization(authorization)
        return self._post('charges', {
            'reference': reference, 'amount': amount, 'currency': currency, 'customer_id': customer_id,
            'payment_method_id': payment_method_id, 'redirect_url': redirect_url, 'meta': meta,
            'authorization': authorization, **extra,
        }, idempotency_key or f"flw-chg-{reference}", scenario_key)

    def list(self, **params):
        """Filters: status, reference, from, to, customer_id, virtual_account_id, payment_method_id, order_id."""
        return self._get('charges', **params)

    def get(self, charge_id):
        return self._get(f'charges/{_p(charge_id)}')

    def update(self, charge_id, authorization=None, meta=None, scenario_key=None):
        _refuse_card_authorization(authorization)
        return self._put(f'charges/{_p(charge_id)}', {'authorization': authorization, 'meta': meta}, scenario_key)

    def authorize_otp(self, charge_id, code):
        return self.update(charge_id, authorization={'type': 'otp', 'otp': {'code': str(code)}})

    def authorize_avs(self, charge_id, address):
        return self.update(charge_id, authorization={'type': 'avs', 'avs': {'address': address}})


class Orchestration(Resource):
    """One-call flows: customer + payment method + charge/order in a single request (non-card only)."""

    def direct_charge(self, reference, amount, currency, customer, payment_method, redirect_url=None, meta=None,
                      authorization=None, idempotency_key=None, scenario_key=None, **extra):
        _refuse_card_method((payment_method or {}).get('type'))
        _refuse_card_authorization(authorization)
        return self._post('orchestration/direct-charges', {
            'reference': reference, 'amount': amount, 'currency': currency, 'customer': customer,
            'payment_method': payment_method, 'redirect_url': redirect_url, 'meta': meta,
            'authorization': authorization, **extra,
        }, idempotency_key or f"flw-och-{reference}", scenario_key)

    def direct_order(self, reference, amount, currency, customer, payment_method, redirect_url=None, meta=None,
                     authorization=None, idempotency_key=None, scenario_key=None, **extra):
        _refuse_card_method((payment_method or {}).get('type'))
        _refuse_card_authorization(authorization)
        return self._post('orchestration/direct-orders', {
            'reference': reference, 'amount': amount, 'currency': currency, 'customer': customer,
            'payment_method': payment_method, 'redirect_url': redirect_url, 'meta': meta,
            'authorization': authorization, **extra,
        }, idempotency_key or f"flw-ord-{reference}", scenario_key)


class Orders(Resource):
    """Orders support authorize-then-capture (preauthorization)."""

    def create(self, reference, amount, currency, customer_id, payment_method_id, redirect_url=None, meta=None,
               idempotency_key=None, scenario_key=None, **extra):
        return self._post('orders', {
            'reference': reference, 'amount': amount, 'currency': currency, 'customer_id': customer_id,
            'payment_method_id': payment_method_id, 'redirect_url': redirect_url, 'meta': meta, **extra,
        }, idempotency_key or f"flw-ord-{reference}", scenario_key)

    def list(self, **params):
        return self._get('orders', **params)

    def get(self, order_id):
        return self._get(f'orders/{_p(order_id)}')

    def update(self, order_id, action=None, meta=None):
        """``action``: 'capture' or 'void'."""
        return self._put(f'orders/{_p(order_id)}', {'action': action, 'meta': meta})

    def capture(self, order_id):
        return self.update(order_id, action='capture')

    def void(self, order_id):
        return self.update(order_id, action='void')


class Refunds(Resource):

    def create(self, charge_id, amount, reason='requested_by_customer', meta=None, idempotency_key=None, **extra):
        """``reason``: duplicate, fraudulent, requested_by_customer, expired_uncaptured_charge."""
        return self._post('refunds', {'charge_id': charge_id, 'amount': amount, 'reason': reason, 'meta': meta,
                                      **extra}, idempotency_key)

    def list(self, **params):
        return self._get('refunds', **params)

    def get(self, refund_id):
        return self._get(f'refunds/{_p(refund_id)}')


class Transfers(Resource):

    def create(self, reference, payment_instruction, action='instant', narration=None, callback_url=None,
               meta=None, disburse_option=None, idempotency_key=None, scenario_key=None, **extra):
        """Transfer to a pre-created recipient (``payment_instruction.recipient_id``/``sender_id``)."""
        return self._post('transfers', {
            'action': action, 'reference': reference, 'narration': narration, 'callback_url': callback_url,
            'meta': meta, 'disburse_option': disburse_option, 'payment_instruction': payment_instruction, **extra,
        }, idempotency_key or f"flw-trf-{reference}", scenario_key)

    def direct(self, reference, type, payment_instruction, action='instant', narration=None, callback_url=None,
               meta=None, disburse_option=None, idempotency_key=None, scenario_key=None, **extra):
        """Orchestrator transfer: recipient details inline (``type``: bank, mobile_money, wallet...)."""
        return self._post('direct-transfers', {
            'action': action, 'type': type, 'reference': reference, 'narration': narration,
            'callback_url': callback_url, 'meta': meta, 'disburse_option': disburse_option,
            'payment_instruction': payment_instruction, **extra,
        }, idempotency_key or f"flw-trf-{reference}", scenario_key)

    def list(self, **params):
        """Filters: size, next, previous, from, to, bulk_id, recipient_id, sender_id, currencies, action, type, status."""
        return self._get('transfers', **params)

    def get(self, transfer_id):
        return self._get(f'transfers/{_p(transfer_id)}')

    def update(self, transfer_id, initiate=None, close=None, disburse_option=None):
        """Initiate or close a deferred/scheduled transfer."""
        return self._put(f'transfers/{_p(transfer_id)}', {'initiate': initiate, 'close': close,
                                                          'disburse_option': disburse_option})

    def retry(self, transfer_id, action='retry', reference=None, meta=None, callback_url=None,
              idempotency_key=None):
        """``action``: 'retry' (failed transfers) or 'duplicate' (successful ones)."""
        return self._post(f'transfers/{_p(transfer_id)}/retries', {
            'action': action, 'reference': reference, 'meta': meta, 'callback_url': callback_url,
        }, idempotency_key)


class TransferRecipients(Resource):

    def create(self, type, idempotency_key=None, **details):
        """``type`` e.g. bank_ngn, bank_usd, mobile_money_ghs; ``details`` as documented for that type."""
        return self._post('transfers/recipients', {'type': type, **details}, idempotency_key)

    def list(self, **params):
        return self._get('transfers/recipients', **params)

    def get(self, recipient_id):
        return self._get(f'transfers/recipients/{_p(recipient_id)}')

    def delete(self, recipient_id):
        return self._delete(f'transfers/recipients/{_p(recipient_id)}')


class TransferSenders(Resource):

    def create(self, type, idempotency_key=None, **details):
        return self._post('transfers/senders', {'type': type, **details}, idempotency_key)

    def list(self, **params):
        return self._get('transfers/senders', **params)

    def get(self, sender_id):
        return self._get(f'transfers/senders/{_p(sender_id)}')

    def delete(self, sender_id):
        return self._delete(f'transfers/senders/{_p(sender_id)}')


class TransferRates(Resource):

    def convert(self, source_currency, destination_currency, destination_amount, precision=None,
                idempotency_key=None):
        return self._post('transfers/rates', {
            'source': {'currency': source_currency},
            'destination': {'currency': destination_currency, 'amount': destination_amount},
            'precision': precision,
        }, idempotency_key)

    def get(self, rate_id):
        return self._get(f'transfers/rates/{_p(rate_id)}')


class VirtualAccounts(Resource):

    def create(self, customer_id, reference, currency, account_type='static', amount=None, expiry=None,
               narration=None, bvn=None, nin=None, customer_account_number=None, bank_code=None, meta=None,
               idempotency_key=None, **extra):
        return self._post('virtual-accounts', {
            'customer_id': customer_id, 'reference': reference, 'currency': currency,
            'account_type': account_type, 'amount': amount, 'expiry': expiry, 'narration': narration,
            'bvn': bvn, 'nin': nin, 'customer_account_number': customer_account_number, 'bank_code': bank_code,
            'meta': meta, **extra,
        }, idempotency_key or f"flw-va-{reference}")

    def list(self, **params):
        return self._get('virtual-accounts', **params)

    def get(self, account_id):
        return self._get(f'virtual-accounts/{_p(account_id)}')

    def update(self, account_id, action_type, status=None, bvn=None, meta=None):
        """``action_type``: 'update_status' (with ``status='inactive'``) or 'update_bvn'."""
        return self._put(f'virtual-accounts/{_p(account_id)}', {'action_type': action_type, 'status': status,
                                                                'bvn': bvn, 'meta': meta})

    def deactivate(self, account_id):
        return self.update(account_id, 'update_status', status='inactive')

    def update_bvn(self, account_id, bvn):
        return self.update(account_id, 'update_bvn', bvn=bvn)


class Banks(Resource):

    def list(self, country):
        return self._get('banks', country=country)

    def branches(self, bank_id):
        return self._get(f'banks/{_p(bank_id)}/branches')

    def resolve_account(self, currency, code, number, scenario_key=None, **account):
        return self._post('banks/account-resolve', {
            'currency': currency, 'account': {'code': code, 'number': number, **account},
        }, scenario_key=scenario_key)


class MobileNetworks(Resource):

    def list(self, country):
        return self._get('mobile-networks', country=country)


class Wallets(Resource):

    def resolve(self, identifier, provider='flutterwave'):
        return self._post('wallets/account-resolve', {'provider': provider, 'identifier': identifier})

    def statement(self, currency, **params):
        """Filters: size, next, previous, from, to."""
        return self._get('wallets/statement', currency=currency, **params)

    def balance(self, currency):
        return self._get(f'wallets/balances/{_p(currency)}')

    def balances(self):
        return self._get('wallets/balances')


class Settlements(Resource):

    def list(self, **params):
        return self._get('settlements', **params)

    def get(self, settlement_id):
        return self._get(f'settlements/{_p(settlement_id)}')


class Chargebacks(Resource):

    def list(self, **params):
        return self._get('chargebacks', **params)

    def create(self, charge_id, amount, expiry, type, idempotency_key=None, **extra):
        return self._post('chargebacks', {'charge_id': charge_id, 'amount': amount, 'expiry': expiry, 'type': type,
                                          **extra}, idempotency_key)

    def get(self, chargeback_id):
        return self._get(f'chargebacks/{_p(chargeback_id)}')

    def update(self, chargeback_id, status=None, comment=None, uploaded_proof=None, proof_data=None, **extra):
        """``status``: accepted, declined or partially-accepted."""
        return self._put(f'chargebacks/{_p(chargeback_id)}', {
            'status': status, 'comment': comment, 'uploaded_proof': uploaded_proof, 'proof_data': proof_data,
            **extra,
        })

    def accept(self, chargeback_id, comment):
        return self.update(chargeback_id, status='accepted', comment=comment)

    def decline(self, chargeback_id, comment, uploaded_proof=None, proof_data=None):
        return self.update(chargeback_id, status='declined', comment=comment, uploaded_proof=uploaded_proof,
                           proof_data=proof_data)


class Fees(Resource):

    def get(self, amount, currency, payment_method, **params):
        """``payment_method`` e.g. mobile_money, bank_transfer; optional card6, country, network."""
        return self._get('fees', amount=amount, currency=currency, payment_method=payment_method, **params)


class Webhooks(Resource):

    def resend(self, entity_id, type):
        """``type``: charge.completed, transfer.disburse, transfer.reversal, order.authorization."""
        return self._post('resend-webhook', {'id': entity_id, 'type': type})
