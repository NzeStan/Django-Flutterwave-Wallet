"""The Flutterwave v4 client: OAuth, retries, idempotency, card refusal and every endpoint."""
import json

import pytest
import requests

from flutterwave_wallet.exceptions import (
    CardDataNotAllowed,
    FlutterwaveAPIError,
    FlutterwaveConfigurationError,
    InvalidFlutterwaveResponse,
)
from flutterwave_wallet.flutterwave.v4 import PRODUCTION_URL, SANDBOX_URL, FlutterwaveV4Client
from tests.conftest import FLW4_BASE, FLW4_TOKEN_URL

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(flw4):
    return FlutterwaveV4Client(max_retries=2)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda s: None)


def test_oauth_token_fetched_once_and_sent(client, flw4):
    flw4.add('GET', 'wallets/balances', [{'currency': 'NGN', 'available_balance': 10}])
    client.wallets.balances()
    client.wallets.balances()
    assert len(flw4.token_calls()) == 1
    token_request = flw4.token_calls()[0].request
    assert 'grant_type=client_credentials' in token_request.body
    assert 'client_id=test-client-id-for-tests' in token_request.body
    api = flw4.api_calls('wallets/balances')[0].request
    assert api.headers['Authorization'] == 'Bearer v4-access-token'
    assert len(api.headers['X-Trace-Id']) >= 12
    assert 'X-Idempotency-Key' not in api.headers  # GETs don't need one


def test_token_is_shared_between_workers_via_cache(flw4):
    flw4.add('GET', 'wallets/balances', [])
    FlutterwaveV4Client().wallets.balances()
    FlutterwaveV4Client().wallets.balances()   # a fresh process-level client reuses the cached token
    assert len(flw4.token_calls()) == 1


def test_expired_token_is_refreshed_on_401(client, flw4):
    flw4.rsps.add('GET', f'{FLW4_BASE}/wallets/balances', status=401,
                  json={'status': 'failed', 'error': {'message': 'Token expired'}})
    flw4.add('GET', 'wallets/balances', [{'currency': 'NGN'}])
    assert client.wallets.balances() == [{'currency': 'NGN'}]
    assert len(flw4.token_calls()) == 2


def test_post_always_carries_idempotency_key_and_is_safely_retried(client, flw4):
    flw4.rsps.add('POST', f'{FLW4_BASE}/customers', status=503, json={'status': 'failed'})
    flw4.add('POST', 'customers', {'id': 'cus_1'})
    assert client.customers.create(email='ada@example.com') == {'id': 'cus_1'}
    keys = [c.request.headers['X-Idempotency-Key'] for c in flw4.api_calls('customers', 'POST')]
    assert len(keys) == 2 and keys[0] == keys[1] and len(keys[0]) >= 12


def test_put_is_never_retried(client, flw4):
    flw4.rsps.add('PUT', f'{FLW4_BASE}/charges/chg_1', status=503, json={'status': 'failed'})
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.charges.authorize_otp('chg_1', '123456')
    assert not exc.value.is_definitive
    assert len(flw4.api_calls('charges/chg_1', 'PUT')) == 1


def test_error_body_and_classification(client, flw4):
    flw4.add('POST', 'refunds', status=400, ok=False, message='charge_id is invalid')
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.refunds.create('chg_x', 10)
    assert exc.value.is_definitive and exc.value.flutterwave_message == 'charge_id is invalid'
    flw4.add('POST', 'transfers', status=409, ok=False, message='Duplicate request')
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.transfers.create('ref-0000001', {})
    assert not exc.value.is_definitive  # conflict: outcome unknown, reconcile
    flw4.rsps.add('GET', f'{FLW4_BASE}/banks', body='<html>bad gateway</html>', status=502)
    with pytest.raises(InvalidFlutterwaveResponse):
        FlutterwaveV4Client(max_retries=0).banks.list('NG')


def test_network_error_message_is_generic(client, flw4):
    flw4.rsps.add('GET', f'{FLW4_BASE}/banks', body=requests.ConnectionError('internal-host:5432'))
    with pytest.raises(FlutterwaveAPIError) as exc:
        FlutterwaveV4Client(max_retries=0).banks.list('NG')
    assert 'internal-host' not in str(exc.value)


def test_bad_credentials_never_leak(flw4, settings):
    flw4.rsps.replace('POST', FLW4_TOKEN_URL, status=401,
                      json={'error': 'invalid_client', 'error_description': 'secret abc123 is wrong'})
    with pytest.raises(FlutterwaveAPIError) as exc:
        FlutterwaveV4Client().wallets.balances()
    assert 'abc123' not in str(exc.value) and exc.value.status_code == 401
    settings.FLUTTERWAVE_CLIENT_ID = ''
    with pytest.raises(FlutterwaveConfigurationError):
        FlutterwaveV4Client().wallets.balances()


def test_environment_urls(settings):
    assert FlutterwaveV4Client().base_url == SANDBOX_URL
    settings.FLUTTERWAVE_V4_ENVIRONMENT = 'production'
    assert FlutterwaveV4Client().base_url == PRODUCTION_URL
    settings.FLUTTERWAVE_V4_API_URL = 'https://proxy.example/flw/'
    assert FlutterwaveV4Client().base_url == 'https://proxy.example/flw'


def test_scenario_key_header(client, flw4):
    flw4.add('POST', 'banks/account-resolve', {'account_name': 'ADA'})
    client.banks.resolve_account('NGN', '044', '0690000031', scenario_key='scenario:successful')
    assert flw4.api_calls('banks/account-resolve')[0].request.headers['X-Scenario-Key'] == 'scenario:successful'


@pytest.mark.parametrize('call', [
    lambda c: c.payment_methods.create('card', card={'encrypted_card_number': 'x'}),
    lambda c: c.payment_methods.create('applepay', applepay={}),
    lambda c: c.payment_methods.create('googlepay', googlepay={}),
    lambda c: c.orchestration.direct_charge('ref-00000001', 10, 'NGN', {'email': 'a@b.c'},
                                            {'type': 'card', 'card': {}}),
    lambda c: c.orchestration.direct_order('ref-00000001', 10, 'NGN', {'email': 'a@b.c'},
                                           {'type': 'card', 'card': {}}),
    lambda c: c.charges.update('chg_1', authorization={'type': 'pin', 'pin': {'encrypted_pin': 'x'}}),
    lambda c: c.charges.update('chg_1', authorization={'type': 'external_3ds', 'external_3ds': {}}),
])
def test_card_data_is_refused(client, flw4, call):
    with pytest.raises(CardDataNotAllowed):
        call(client)
    assert not flw4.api_calls()


@pytest.mark.parametrize('call, method, path', [
    (lambda c: c.customers.create(email='a@b.c'), 'POST', 'customers'),
    (lambda c: c.customers.list(page=1), 'GET', 'customers'),
    (lambda c: c.customers.get('cus_1'), 'GET', 'customers/cus_1'),
    (lambda c: c.customers.update('cus_1', meta={'a': 'b'}), 'PUT', 'customers/cus_1'),
    (lambda c: c.customers.search('a@b.c'), 'POST', 'customers/search'),
    (lambda c: c.payment_methods.create('mobile_money', mobile_money={'network': 'MTN'}), 'POST', 'payment-methods'),
    (lambda c: c.payment_methods.list(), 'GET', 'payment-methods'),
    (lambda c: c.payment_methods.get('pmd_1'), 'GET', 'payment-methods/pmd_1'),
    (lambda c: c.charges.create('ref-00000001', 10, 'NGN', 'cus_1', 'pmd_1'), 'POST', 'charges'),
    (lambda c: c.charges.list(reference='ref-1'), 'GET', 'charges'),
    (lambda c: c.charges.get('chg_1'), 'GET', 'charges/chg_1'),
    (lambda c: c.charges.authorize_otp('chg_1', '123456'), 'PUT', 'charges/chg_1'),
    (lambda c: c.orchestration.direct_charge('ref-00000001', 10, 'GHS', {'email': 'a@b.c'},
                                             {'type': 'mobile_money', 'mobile_money': {}}),
     'POST', 'orchestration/direct-charges'),
    (lambda c: c.orchestration.direct_order('ref-00000001', 10, 'NGN', {'email': 'a@b.c'},
                                            {'type': 'ussd', 'ussd': {}}), 'POST', 'orchestration/direct-orders'),
    (lambda c: c.orders.create('ref-00000001', 10, 'NGN', 'cus_1', 'pmd_1'), 'POST', 'orders'),
    (lambda c: c.orders.list(), 'GET', 'orders'),
    (lambda c: c.orders.get('ord_1'), 'GET', 'orders/ord_1'),
    (lambda c: c.orders.capture('ord_1'), 'PUT', 'orders/ord_1'),
    (lambda c: c.orders.void('ord_1'), 'PUT', 'orders/ord_1'),
    (lambda c: c.refunds.create('chg_1', 10), 'POST', 'refunds'),
    (lambda c: c.refunds.list(), 'GET', 'refunds'),
    (lambda c: c.refunds.get('rfd_1'), 'GET', 'refunds/rfd_1'),
    (lambda c: c.transfers.create('ref-00000001', {'recipient_id': 'rcb_1'}), 'POST', 'transfers'),
    (lambda c: c.transfers.direct('ref-00000001', 'bank', {}), 'POST', 'direct-transfers'),
    (lambda c: c.transfers.list(status='failed'), 'GET', 'transfers'),
    (lambda c: c.transfers.get('trf_1'), 'GET', 'transfers/trf_1'),
    (lambda c: c.transfers.update('trf_1', initiate=True), 'PUT', 'transfers/trf_1'),
    (lambda c: c.transfers.retry('trf_1'), 'POST', 'transfers/trf_1/retries'),
    (lambda c: c.transfer_recipients.create('bank_ngn', bank={'code': '044'}), 'POST', 'transfers/recipients'),
    (lambda c: c.transfer_recipients.list(), 'GET', 'transfers/recipients'),
    (lambda c: c.transfer_recipients.get('rcb_1'), 'GET', 'transfers/recipients/rcb_1'),
    (lambda c: c.transfer_recipients.delete('rcb_1'), 'DELETE', 'transfers/recipients/rcb_1'),
    (lambda c: c.transfer_senders.create('generic_sender', name={'first': 'A'}), 'POST', 'transfers/senders'),
    (lambda c: c.transfer_senders.list(), 'GET', 'transfers/senders'),
    (lambda c: c.transfer_senders.get('sdr_1'), 'GET', 'transfers/senders/sdr_1'),
    (lambda c: c.transfer_senders.delete('sdr_1'), 'DELETE', 'transfers/senders/sdr_1'),
    (lambda c: c.transfer_rates.convert('NGN', 'USD', 100), 'POST', 'transfers/rates'),
    (lambda c: c.transfer_rates.get('rte_1'), 'GET', 'transfers/rates/rte_1'),
    (lambda c: c.virtual_accounts.create('cus_1', 'ref-00000001', 'NGN'), 'POST', 'virtual-accounts'),
    (lambda c: c.virtual_accounts.list(), 'GET', 'virtual-accounts'),
    (lambda c: c.virtual_accounts.get('va_1'), 'GET', 'virtual-accounts/va_1'),
    (lambda c: c.virtual_accounts.deactivate('va_1'), 'PUT', 'virtual-accounts/va_1'),
    (lambda c: c.virtual_accounts.update_bvn('va_1', '12345678912'), 'PUT', 'virtual-accounts/va_1'),
    (lambda c: c.banks.list('NG'), 'GET', 'banks'),
    (lambda c: c.banks.branches('bnk_1'), 'GET', 'banks/bnk_1/branches'),
    (lambda c: c.banks.resolve_account('NGN', '044', '0690000031'), 'POST', 'banks/account-resolve'),
    (lambda c: c.mobile_networks.list('GH'), 'GET', 'mobile-networks'),
    (lambda c: c.wallets.resolve('00118468'), 'POST', 'wallets/account-resolve'),
    (lambda c: c.wallets.statement('NGN'), 'GET', 'wallets/statement'),
    (lambda c: c.wallets.balance('NGN'), 'GET', 'wallets/balances/NGN'),
    (lambda c: c.wallets.balances(), 'GET', 'wallets/balances'),
    (lambda c: c.settlements.list(), 'GET', 'settlements'),
    (lambda c: c.settlements.get('stl_1'), 'GET', 'settlements/stl_1'),
    (lambda c: c.chargebacks.list(), 'GET', 'chargebacks'),
    (lambda c: c.chargebacks.create('chg_1', 10, 72, 'local'), 'POST', 'chargebacks'),
    (lambda c: c.chargebacks.get('cbk_1'), 'GET', 'chargebacks/cbk_1'),
    (lambda c: c.chargebacks.decline('cbk_1', 'Delivered', uploaded_proof='https://p'), 'PUT', 'chargebacks/cbk_1'),
    (lambda c: c.fees.get(100, 'NGN', 'mobile_money'), 'GET', 'fees'),
    (lambda c: c.webhooks.resend('chg_1', 'charge.completed'), 'POST', 'resend-webhook'),
])
def test_v4_resource_paths(client, flw4, call, method, path):
    flw4.add(method, path, {'ok': True})
    assert call(client) == {'ok': True}
    sent = flw4.api_calls(path, method)
    assert sent
    if method == 'POST':
        assert len(sent[-1].request.headers['X-Idempotency-Key']) >= 12


def test_request_bodies(client, flw4):
    flw4.add('PUT', 'charges/chg_1', {})
    client.charges.authorize_otp('chg_1', 123456)
    assert json.loads(flw4.api_calls('charges/chg_1')[0].request.body) == {
        'authorization': {'type': 'otp', 'otp': {'code': '123456'}}}
    flw4.add('PUT', 'virtual-accounts/va_1', {})
    client.virtual_accounts.deactivate('va_1')
    assert json.loads(flw4.api_calls('virtual-accounts/va_1')[0].request.body) == {
        'action_type': 'update_status', 'status': 'inactive'}
    flw4.add('POST', 'transfers/rates', {})
    client.transfer_rates.convert('NGN', 'EUR', 1200.56, precision=4)
    assert json.loads(flw4.api_calls('transfers/rates')[0].request.body) == {
        'source': {'currency': 'NGN'}, 'destination': {'currency': 'EUR', 'amount': 1200.56}, 'precision': 4}


def test_paginate_pages_and_cursor(client, flw4):
    flw4.add('GET', 'charges', [{'id': 1}], meta={'page_info': {'total_pages': 2}})
    flw4.add('GET', 'charges', [{'id': 2}], meta={'page_info': {'total_pages': 2}})
    assert [c['id'] for c in client.paginate('charges')] == [1, 2]
    flw4.add('GET', 'transfers', [{'id': 'a'}], meta={'cursor': {'next': 'cur2'}})
    flw4.add('GET', 'transfers', [{'id': 'b'}], meta={'cursor': {}})
    assert [t['id'] for t in client.paginate('transfers')] == ['a', 'b']
    assert 'next=cur2' in flw4.api_calls('transfers')[-1].request.url


def test_idempotent_replay_is_reported(client, flw4):
    flw4.rsps.add('POST', f'{FLW4_BASE}/refunds', json={'status': 'success', 'data': {'id': 'rfd_1'}},
                  headers={'X-Idempotency-Cache-Hit': 'true'})
    body = client.request('POST', 'refunds', json={'charge_id': 'chg_1'}, raw=True)
    assert body['_idempotent_replay'] is True

