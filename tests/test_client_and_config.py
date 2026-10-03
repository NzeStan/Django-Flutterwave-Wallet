import json
from decimal import Decimal

import pytest
import requests
import responses as responses_lib
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from flutterwave_wallet.conf import _parse_env, load_env_file, wallet_settings
from flutterwave_wallet.exceptions import (
    FlutterwaveAPIError,
    FlutterwaveConfigurationError,
    InvalidFlutterwaveResponse,
)
from flutterwave_wallet.flutterwave import FlutterwaveClient, compute_signature, verify_webhook
from flutterwave_wallet.utils.crypto import decrypt_value, encrypt_value, fingerprint
from flutterwave_wallet.utils.phone import default_normalize_phone_number, mask_phone_number

BASE = 'https://api.flutterwave.com/v3'


@pytest.fixture
def client():
    return FlutterwaveClient(max_retries=2)


def test_auth_header_and_data_unwrapping(client, flw):
    flw.add('GET', 'balances/NGN', {'currency': 'NGN', 'available_balance': 10})
    assert client.balances.currency('NGN') == {'currency': 'NGN', 'available_balance': 10}
    request = flw.calls()[0].request
    assert request.headers['Authorization'] == 'Bearer FLWSECK_TEST-dummy-key-for-tests-X'
    assert 'django-flutterwave-wallet' in request.headers['User-Agent']


def test_get_is_retried_but_post_never(client, flw, monkeypatch):
    monkeypatch.setattr('time.sleep', lambda s: None)
    flw.rsps.add(responses_lib.GET, f'{BASE}/transfers/1', status=503, json={})
    flw.add('GET', 'transfers/1', {'id': 1})
    assert client.transfers.fetch(1) == {'id': 1}
    flw.rsps.add(responses_lib.POST, f'{BASE}/transfers', status=503, json={'status': 'error', 'message': 'x'})
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.transfers.create(account_bank='044', account_number='1', amount=1, currency='NGN')
    assert not exc.value.is_definitive
    assert len(flw.calls('transfers', 'POST')) == 1


def test_error_classification(client, flw, monkeypatch):
    monkeypatch.setattr('time.sleep', lambda s: None)
    flw.error('GET', 'transfers/2', 'Transfer not found', status=404)
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.transfers.fetch(2)
    assert exc.value.is_definitive and exc.value.is_not_found and exc.value.http_status == 400
    flw.add('POST', 'otps', body={'status': 'error', 'message': 'Rejected'}, status=200)
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.otps.create({'name': 'a'}, 'Shop', ['sms'])
    assert exc.value.is_definitive
    flw.rsps.add(responses_lib.GET, f'{BASE}/banks/NG', body='<html>502</html>', status=502)
    with pytest.raises(InvalidFlutterwaveResponse) as exc:
        client.banks.list('NG')
    assert not exc.value.is_definitive and exc.value.http_status == 502
    flw.error('POST', 'transfers', 'Too many requests', status=429)
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.transfers.create(account_bank='044', account_number='1', amount=1, currency='NGN')
    assert not exc.value.is_definitive


def test_network_error_hides_details(client, flw, monkeypatch):
    monkeypatch.setattr('time.sleep', lambda s: None)
    flw.rsps.add(responses_lib.POST, f'{BASE}/transfers', body=requests.ConnectionError('secret host'))
    with pytest.raises(FlutterwaveAPIError) as exc:
        client.transfers.create(account_bank='044', account_number='1', amount=1, currency='NGN')
    assert 'secret host' not in str(exc.value) and exc.value.status_code is None


def test_missing_key(settings):
    settings.FLUTTERWAVE_SECRET_KEY = ''
    with pytest.raises(FlutterwaveConfigurationError):
        FlutterwaveClient().banks.list()


def test_path_segments_are_encoded(client, flw):
    flw.rsps.add(responses_lib.GET, f'{BASE}/transfers/..%2Fbalances', json={'status': 'success', 'data': {}})
    client.transfers.fetch('../balances')
    assert flw.calls()[0].request.url.endswith('/transfers/..%2Fbalances')


def test_none_values_are_not_sent(client, flw):
    flw.add('POST', 'payment-plans', {'id': 1})
    client.payment_plans.create(name='Gold', amount=5000, interval='monthly')
    assert flw.last_body('payment-plans') == {'name': 'Gold', 'amount': 5000, 'interval': 'monthly'}


def test_paginate(client, flw):
    flw.add('GET', 'transfers', [{'id': 1}, {'id': 2}], meta={'page_info': {'total_pages': 2}})
    flw.add('GET', 'transfers', [{'id': 3}], meta={'page_info': {'total_pages': 2}})
    assert [t['id'] for t in client.paginate('transfers')] == [1, 2, 3]


@pytest.mark.parametrize('call, method, path', [
    (lambda c: c.preauth.capture('FLW-1', 100), 'POST', 'charges/FLW-1/capture'),
    (lambda c: c.preauth.void('FLW-1'), 'POST', 'charges/FLW-1/void'),
    (lambda c: c.preauth.refund('FLW-1', 50), 'POST', 'charges/FLW-1/refund'),
    (lambda c: c.tokenized_charges.bulk('t', 'r', []), 'POST', 'bulk-tokenized-charges'),
    (lambda c: c.tokenized_charges.bulk_status(9), 'GET', 'bulk-tokenized-charges/9'),
    (lambda c: c.tokenized_charges.bulk_transactions(9), 'GET', 'bulk-tokenized-charges/9/transactions'),
    (lambda c: c.tokenized_charges.update_card_token('tok', email='a@b.c'), 'PUT', 'tokens/tok'),
    (lambda c: c.tokenized_charges.tokenize_account('044', '1', 'addr', 200, 'a@b.c', '080', '2030-01-01'),
     'POST', 'accounts/tokenize'),
    (lambda c: c.tokenized_charges.account_token('R1'), 'GET', 'accounts/token/R1'),
    (lambda c: c.tokenized_charges.update_account_token('R1', 'SUSPENDED'), 'PUT', 'accounts/token/R1'),
    (lambda c: c.transactions.list(status='successful'), 'GET', 'transactions'),
    (lambda c: c.transactions.timeline(1), 'GET', 'transactions/1/events'),
    (lambda c: c.transactions.fee(100, 'NGN'), 'GET', 'transactions/fee'),
    (lambda c: c.transactions.resend_webhook(1), 'POST', 'transactions/1/resend-hook'),
    (lambda c: c.refunds.list(), 'GET', 'refunds'),
    (lambda c: c.virtual_accounts.create_bulk('b', []), 'POST', 'bulk-virtual-account-numbers'),
    (lambda c: c.virtual_accounts.fetch_bulk('b'), 'GET', 'bulk-virtual-account-numbers/b'),
    (lambda c: c.virtual_accounts.fetch('URF'), 'GET', 'virtual-account-numbers/URF'),
    (lambda c: c.virtual_accounts.update_bvn('URF', '1'), 'PUT', 'virtual-account-numbers/URF'),
    (lambda c: c.virtual_accounts.deactivate('URF'), 'POST', 'virtual-account-numbers/URF'),
    (lambda c: c.payment_plans.list(), 'GET', 'payment-plans'),
    (lambda c: c.payment_plans.fetch(1), 'GET', 'payment-plans/1'),
    (lambda c: c.payment_plans.update(1, name='x'), 'PUT', 'payment-plans/1'),
    (lambda c: c.payment_plans.cancel(1), 'PUT', 'payment-plans/1/cancel'),
    (lambda c: c.subscriptions.list(), 'GET', 'subscriptions'),
    (lambda c: c.subscriptions.cancel(1), 'PUT', 'subscriptions/1/cancel'),
    (lambda c: c.subscriptions.activate(1), 'PUT', 'subscriptions/1/activate'),
    (lambda c: c.subaccounts.create('044', '1', 'Biz', 'NG', 0.5, '080'), 'POST', 'subaccounts'),
    (lambda c: c.subaccounts.update(1, split_value=0.2), 'PUT', 'subaccounts/1'),
    (lambda c: c.subaccounts.delete(1), 'DELETE', 'subaccounts/1'),
    (lambda c: c.settlements.list(), 'GET', 'settlements'),
    (lambda c: c.settlements.fetch(1), 'GET', 'settlements/1'),
    (lambda c: c.chargebacks.list(), 'GET', 'chargebacks'),
    (lambda c: c.chargebacks.accept(1, 'ok'), 'PUT', 'chargebacks/1'),
    (lambda c: c.transfers.create_bulk([]), 'POST', 'bulk-transfers'),
    (lambda c: c.transfers.retry(1), 'POST', 'transfers/1/retries'),
    (lambda c: c.transfers.retries(1), 'GET', 'transfers/1/retries'),
    (lambda c: c.transfers.rates(100, 'USD', 'NGN'), 'GET', 'transfers/rates'),
    (lambda c: c.beneficiaries.create('044', '1'), 'POST', 'beneficiaries'),
    (lambda c: c.beneficiaries.delete(1), 'DELETE', 'beneficiaries/1'),
    (lambda c: c.payout_subaccounts.create('Ada', 'a@b.c', 'NG'), 'POST', 'payout-subaccounts'),
    (lambda c: c.payout_subaccounts.balance('PSA1'), 'GET', 'payout-subaccounts/PSA1/balances'),
    (lambda c: c.payout_subaccounts.transactions('PSA1', '2026-01-01', '2026-02-01'), 'GET',
     'payout-subaccounts/PSA1/transactions'),
    (lambda c: c.payout_subaccounts.static_account('PSA1'), 'GET', 'payout-subaccounts/PSA1/static-account'),
    (lambda c: c.payout_subaccounts.refund_funding('F1'), 'POST', 'payout-subaccounts/refunds'),
    (lambda c: c.bills.create_bulk('b', 'https://cb', []), 'POST', 'bulk-bills'),
    (lambda c: c.bills.status('R1'), 'GET', 'bills/R1'),
    (lambda c: c.bills.summary('2026-01-01', '2026-02-01'), 'GET', 'bills/summary'),
    (lambda c: c.bills.history(), 'GET', 'bills/history'),
    (lambda c: c.remita.agencies(), 'GET', 'billers'),
    (lambda c: c.remita.products('B1'), 'GET', 'billers/B1/products'),
    (lambda c: c.remita.product_amount('B1', 'P1'), 'GET', 'billers/B1/products/P1'),
    (lambda c: c.remita.create_order('B1', 'P1', {}, []), 'POST', 'billers/B1/products/P1/orders'),
    (lambda c: c.remita.update_order('O1', 100), 'PUT', 'product-orders/O1'),
    (lambda c: c.fx.create_quote('NGN', 'USD', 1000), 'POST', 'rfq'),
    (lambda c: c.fx.quote('Q1'), 'GET', 'rfq/Q1'),
    (lambda c: c.fx.create_trade('Q1', 'note'), 'POST', 'trades'),
    (lambda c: c.fx.trade('T1'), 'GET', 'trade/T1'),
    (lambda c: c.otps.validate('R1', 123456), 'POST', 'otps/R1/validate'),
    (lambda c: c.banks.branches(1), 'GET', 'banks/1/branches'),
    (lambda c: c.verification.card_bin('553188'), 'GET', 'card-bins/553188'),
    (lambda c: c.verification.bvn_details('R1'), 'GET', 'bvn/verifications/R1'),
    (lambda c: c.balances.all(), 'GET', 'balances'),
    (lambda c: c.balances.statement('2026-01-01', '2026-02-01', 'NGN'), 'GET', 'wallet/statement'),
])
def test_resource_paths(client, flw, call, method, path):
    flw.add(method, path, {'ok': True})
    assert call(client) == {'ok': True}
    assert flw.calls(path, method)


@pytest.mark.parametrize('charge_type', ['card', 'applepay', 'googlepay'])
def test_client_refuses_card_charges(client, flw, charge_type):
    from flutterwave_wallet.exceptions import CardDataNotAllowed
    with pytest.raises(CardDataNotAllowed):
        client.charges.create(charge_type, {'card_number': '5531886652142950'})
    assert not flw.calls()
    assert not hasattr(client.charges, 'card') and not hasattr(client, 'virtual_cards')


def test_field_encryption_and_rotation(settings):
    from cryptography.fernet import Fernet
    token = encrypt_value('secret-token')
    assert token.startswith('enc1:') and decrypt_value(token) == 'secret-token'
    settings.FLW_WALLET_FIELD_ENCRYPTION_KEYS = [Fernet.generate_key().decode(),
                                                 *settings.FLW_WALLET_FIELD_ENCRYPTION_KEYS]
    assert decrypt_value(token) == 'secret-token'  # old key still decrypts
    settings.FLW_WALLET_FIELD_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    with pytest.raises(ImproperlyConfigured):
        decrypt_value(token)


def test_fingerprint_is_keyed_and_stable():
    assert fingerprint('553188', '2950') == fingerprint('553188', '2950')
    assert fingerprint('553188', '2950') != fingerprint('553188', '2951')


def test_webhook_signature_helpers():
    body = b'{"event":"x"}'
    signature = compute_signature(body, 'h')
    assert verify_webhook(body, {'flutterwave-signature': signature}, 'h')
    assert verify_webhook(body, {'verif-hash': 'h'}, 'h')
    assert not verify_webhook(body, {'verif-hash': 'H'}, 'h')
    assert not verify_webhook(b'{"event":"y"}', {'flutterwave-signature': signature}, 'h')
    assert not verify_webhook(body, {}, 'h')
    assert not verify_webhook(body, {'verif-hash': ''}, '')


def test_env_parsing(tmp_path, monkeypatch):
    assert _parse_env('true', False) is True
    assert _parse_env('5', 1) == 5
    assert _parse_env('1.5', Decimal('0')) == Decimal('1.5')
    assert _parse_env('NGN,USD', ['NGN']) == ['NGN', 'USD']
    assert _parse_env('{"a": 1}', {}) == {'a': 1}
    assert _parse_env('', 'x') == 'x'
    assert _parse_env('none', None) is None
    assert _parse_env('250', None) == Decimal('250')
    with pytest.raises(ImproperlyConfigured):
        _parse_env('maybe', False)
    env = tmp_path / '.env'
    env.write_text('# comment\nFLW_WALLET_PIN_LENGTH=6\nexport FLW_WALLET_COUNTRY="GH"\n', encoding='utf-8')
    # Registered with monkeypatch so the values are removed again after the test
    monkeypatch.setenv('FLW_WALLET_PIN_LENGTH', '')
    monkeypatch.setenv('FLW_WALLET_COUNTRY', '')
    assert load_env_file(env, override=True) == 2
    assert wallet_settings.PIN_LENGTH == 6 and wallet_settings.COUNTRY == 'GH'
    assert load_env_file(tmp_path / 'missing') == 0


def test_settings_masking_and_unknown(settings):
    resolved = wallet_settings.as_dict()
    for name in ('FLUTTERWAVE_SECRET_KEY', 'FLUTTERWAVE_SECRET_HASH'):
        assert resolved[name] != getattr(settings, name) and '...' in resolved[name]
    assert resolved['FIELD_ENCRYPTION_KEYS'] == ['***']
    with pytest.raises(AttributeError):
        wallet_settings.NOT_A_SETTING


@pytest.mark.django_db
def test_system_checks(settings):
    settings.FLUTTERWAVE_SECRET_KEY = 'sk_live_wrong'
    settings.FLW_WALLET_FEE_RULES = {'deposit': {'bearer': 'nobody', 'percentage': 'abc'}}
    settings.FLW_WALLET_CURRENCY = 'XYZ'
    ids = {message.id for message in run_checks()}
    assert {'flutterwave_wallet.W002', 'flutterwave_wallet.E005', 'flutterwave_wallet.E003'} <= ids


def test_phone_helpers():
    assert default_normalize_phone_number('0803 123 4567') == '+2348031234567'
    assert default_normalize_phone_number('2348031234567') == '+2348031234567'
    assert mask_phone_number('+2348031234567') == '+234803****567'
    from flutterwave_wallet.exceptions import InvalidPhoneNumber
    with pytest.raises(InvalidPhoneNumber):
        default_normalize_phone_number('abc')


@pytest.mark.django_db
def test_management_commands(funded_wallet, capsys):
    call_command('verify_ledger')
    call_command('reconcile_transactions')
    call_command('sweep_platform_fees')
    call_command('prune_wallet_data')
    call_command('wallet_settings', '--json')
    out = capsys.readouterr().out
    assert 'ledger(s) verified' in out and 'FLUTTERWAVE_SECRET_KEY' in out
