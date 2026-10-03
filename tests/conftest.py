import json
from decimal import Decimal

import pytest
import responses as responses_lib
from djmoney.money import Money
from rest_framework.test import APIClient

from flutterwave_wallet.flutterwave.client import compute_signature
from flutterwave_wallet.models import Bank, BankAccount, Card
from flutterwave_wallet.services.wallet_service import WalletService

FLW_BASE = 'https://api.flutterwave.com/v3'


class FlutterwaveMock:
    """Registers fake Flutterwave responses and inspects the requests we sent."""

    def __init__(self, rsps):
        self.rsps = rsps

    def add(self, method, path, data=None, status=200, message='ok', ok=True, body=None, meta=None, **kwargs):
        if body is None:
            body = {'status': 'success' if ok else 'error', 'message': message, 'data': data}
            if meta is not None:
                body['meta'] = meta
        url = f"{FLW_BASE}/{path.lstrip('/')}"
        self.rsps.add(method, url, json=body, status=status, **kwargs)

    def error(self, method, path, message='Bad request', status=400):
        self.add(method, path, status=status, ok=False, message=message)

    def calls(self, path=None, method=None):
        result = []
        for call in self.rsps.calls:
            url = call.request.url.split('?')[0]
            if path is not None and not url.endswith('/' + path.lstrip('/')):
                continue
            if method is not None and call.request.method != method:
                continue
            result.append(call)
        return result

    def last_body(self, path=None):
        call = self.calls(path)[-1]
        return json.loads(call.request.body) if call.request.body else {}


@pytest.fixture(autouse=True)
def _fresh_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def flw():
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FlutterwaveMock(rsps)


@pytest.fixture
def service():
    return WalletService()


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username='ada', email='ada@example.com', first_name='Ada',
                                                 last_name='Obi', password='x')


@pytest.fixture
def other_user(django_user_model):
    return django_user_model.objects.create_user(username='bola', email='bola@example.com', first_name='Bola',
                                                 last_name='Ade', password='x')


@pytest.fixture
def staff_user(django_user_model):
    return django_user_model.objects.create_user(username='staff', email='staff@example.com', is_staff=True,
                                                 is_superuser=True, password='x')


@pytest.fixture
def wallet(user, service):
    return service.get_wallet(user)


@pytest.fixture
def other_wallet(other_user, service):
    return service.get_wallet(other_user)


def fund(wallet, amount):
    """Give a wallet money through the ledger (an audited adjustment)."""
    WalletService().credit_wallet(wallet, Decimal(str(amount)), description='test funding')
    wallet.refresh_from_db()
    return wallet


@pytest.fixture
def funded_wallet(wallet):
    return fund(wallet, 100000)


@pytest.fixture
def bank(db):
    return Bank.objects.create(name='Access Bank', code='044', country='NG')


@pytest.fixture
def bank_account(user, bank):
    return BankAccount.objects.create(user=user, bank=bank, account_bank='044', bank_name='Access Bank',
                                      account_number='0690000031', account_name='ADA OBI', currency='NGN',
                                      country='NG', is_verified=True, is_default=True)


@pytest.fixture
def card(user):
    return Card.objects.create(user=user, token='flw-t1nf-secret-token', fingerprint='fp1', first_6digits='553188',
                               last_4digits='2950', issuer='MASTERCARD', card_type='MASTERCARD', country='NG',
                               expiry='09/99', email='ada@example.com', is_default=True)


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff_client(staff_user):
    client = APIClient()
    client.force_authenticate(user=staff_user)
    return client


def webhook(client, payload, secret=None, mode='hash', **extra):
    """POST a signed webhook (``mode``: 'hash' sends verif-hash, 'hmac' sends flutterwave-signature)."""
    from django.conf import settings

    body = json.dumps(payload).encode()
    secret = secret if secret is not None else settings.FLUTTERWAVE_SECRET_HASH
    headers = {'HTTP_VERIF_HASH': secret} if mode == 'hash' else \
        {'HTTP_FLUTTERWAVE_SIGNATURE': compute_signature(body, secret)}
    return client.post('/wallet/webhook/', data=body, content_type='application/json', **headers, **extra)


def charge_data(tx_ref, amount, flw_id=9001, status='successful', currency='NGN', payment_type='card',
                charged_amount=None, app_fee=0, card=True, email='ada@example.com', **extra):
    data = {
        'id': flw_id, 'tx_ref': tx_ref, 'flw_ref': f'FLW-MOCK-{flw_id}', 'amount': amount,
        'charged_amount': charged_amount if charged_amount is not None else amount, 'app_fee': app_fee,
        'merchant_fee': 0, 'currency': currency, 'status': status, 'payment_type': payment_type,
        'processor_response': 'Approved' if status == 'successful' else 'Declined',
        'customer': {'id': 1, 'email': email, 'name': 'Ada Obi'},
    }
    if card and payment_type == 'card':
        data['card'] = {'first_6digits': '553188', 'last_4digits': '2950', 'issuer': 'MASTERCARD CREDIT',
                        'country': 'NG', 'type': 'MASTERCARD', 'expiry': '09/32', 'token': 'flw-t1nf-new-token'}
    data.update(extra)
    return data


def money(amount, currency='NGN'):
    return Money(Decimal(str(amount)), currency)


# ----------------------------------------------------------------------
# Flutterwave v4
# ----------------------------------------------------------------------

FLW4_BASE = 'https://developersandbox-api.flutterwave.com'
FLW4_TOKEN_URL = 'https://idp.flutterwave.com/realms/flutterwave/protocol/openid-connect/token'


class FlutterwaveV4Mock(FlutterwaveMock):
    def add(self, method, path, data=None, status=200, message='ok', ok=True, body=None, meta=None, **kwargs):
        if body is None:
            body = {'status': 'success' if ok else 'failed', 'message': message, 'data': data}
            if not ok:
                body['error'] = {'type': 'REQUEST_NOT_VALID', 'code': '10400', 'message': message}
            if meta is not None:
                body['meta'] = meta
        self.rsps.add(method, f"{FLW4_BASE}/{path.lstrip('/')}", json=body, status=status, **kwargs)

    def add_v3(self, *args, **kwargs):
        """Register a v3 response on the same mock (hosted checkout, saved cards, bills...)."""
        return FlutterwaveMock.add(self, *args, **kwargs)

    def token_calls(self):
        return [c for c in self.rsps.calls if c.request.url == FLW4_TOKEN_URL]

    def api_calls(self, path=None, method=None):
        return [c for c in self.calls(path, method) if c.request.url != FLW4_TOKEN_URL]


@pytest.fixture
def flw4():
    """Mocked v4 API (sandbox) with an OAuth token endpoint; the wallet runs on API_VERSION='v4'."""
    from flutterwave_wallet.flutterwave.v4 import client as v4_client
    v4_client._default_client = None
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add('POST', FLW4_TOKEN_URL, json={'access_token': 'v4-access-token', 'expires_in': 600,
                                               'token_type': 'Bearer'})
        yield FlutterwaveV4Mock(rsps)
    v4_client._default_client = None


@pytest.fixture
def v4(settings, flw4):
    settings.FLW_WALLET_API_VERSION = 'v4'
    return flw4


def v4_webhook(client, payload, secret=None):
    """POST a v4-style webhook signed with flutterwave-signature (HMAC-SHA256, base64)."""
    return webhook(client, payload, secret=secret, mode='hmac')


def v4_charge(reference, amount, charge_id='chg_test0001', status='succeeded', currency='NGN',
              method='mobile_money', customer_id='cus_test0001', **extra):
    return {'id': charge_id, 'reference': reference, 'amount': amount, 'currency': currency, 'status': status,
            'customer_id': customer_id, 'payment_method_details': {'type': method},
            'fees': [{'type': 'app', 'amount': 14}, {'type': 'vat', 'amount': 1.05}], **extra}
