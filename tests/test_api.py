import json

import pytest
from rest_framework.test import APIClient

from flutterwave_wallet.models import BankAccount, IdempotencyRecord, Transaction
from tests.conftest import charge_data, fund, money

pytestmark = pytest.mark.django_db

API = '/wallet/api'


def test_requires_authentication():
    assert APIClient().get(f'{API}/wallets/').status_code == 403


def test_wallets_list_and_open_currency(api_client, user):
    response = api_client.get(f'{API}/wallets/')
    assert response.status_code == 200 and response.json()['results'][0]['currency'] == 'NGN'
    response = api_client.post(f'{API}/wallets/', {'currency': 'usd'}, format='json')
    assert response.status_code == 201 and response.json()['currency'] == 'USD'
    assert api_client.post(f'{API}/wallets/', {'currency': 'GBP'}, format='json').status_code == 400
    me = api_client.get(f'{API}/wallets/me/?currency=USD').json()
    assert me['currency'] == 'USD' and me['daily_limit'] is None


def test_cannot_see_other_users_wallet(api_client, other_wallet):
    assert api_client.get(f'{API}/wallets/{other_wallet.pk}/').status_code == 404
    assert api_client.get(f'{API}/wallets/not-a-uuid/').status_code == 404


def test_checkout_endpoint_and_idempotency(api_client, wallet, flw):
    flw.add('POST', 'payments', {'link': 'https://checkout/x'})
    headers = {'HTTP_IDEMPOTENCY_KEY': 'key-1'}
    first = api_client.post(f'{API}/wallets/me/checkout/', {'amount': '5000'}, format='json', **headers)
    second = api_client.post(f'{API}/wallets/me/checkout/', {'amount': '5000'}, format='json', **headers)
    assert first.status_code == 201 and second.status_code == 201
    assert second.headers['Idempotent-Replayed'] == 'true'
    assert first.json() == second.json()
    assert Transaction.objects.filter(wallet=wallet).count() == 1
    assert len(flw.calls('payments')) == 1
    reused = api_client.post(f'{API}/wallets/me/checkout/', {'amount': '6000'}, format='json', **headers)
    assert reused.status_code == 422


def test_idempotency_key_required_when_configured(api_client, wallet, settings):
    settings.FLW_WALLET_REQUIRE_IDEMPOTENCY_KEY = True
    response = api_client.post(f'{API}/wallets/me/checkout/', {'amount': '5000'}, format='json')
    assert response.status_code == 400 and response.json()['code'] == 'idempotency_key_required'


def test_idempotency_record_released_on_server_error(api_client, wallet, flw):
    flw.add('POST', 'payments', body={'status': 'error', 'message': 'x'}, status=500)
    response = api_client.post(f'{API}/wallets/me/checkout/', {'amount': '5000'}, format='json',
                               HTTP_IDEMPOTENCY_KEY='key-2')
    assert response.status_code == 502
    assert not IdempotencyRecord.objects.filter(key='key-2').exists()


def test_transfer_endpoint_with_pin(api_client, user, funded_wallet, other_wallet, service, settings):
    settings.FLW_WALLET_REQUIRE_TRANSACTION_PIN = True
    payload = {'recipient': '@bola', 'amount': '250.00'}
    response = api_client.post(f'{API}/wallets/me/transfer/', payload, format='json')
    assert response.status_code == 400 and response.json()['code'] == 'pin_required'
    service.set_pin(user, '1357')
    response = api_client.post(f'{API}/wallets/me/transfer/', {**payload, 'pin': '0000'}, format='json')
    assert response.status_code == 403 and response.json()['code'] == 'invalid_pin'
    response = api_client.post(f'{API}/wallets/me/transfer/', {**payload, 'pin': '1357'}, format='json')
    assert response.status_code == 201
    data = response.json()
    assert data['total_amount'] == '250.00' and data['counterparty']['tag'] == 'bola'
    assert 'fee' not in data['metadata']


def test_amount_validation(api_client, funded_wallet, other_wallet):
    for amount in ('-1', '0', 'abc', '1.001', '1e30'):
        response = api_client.post(f'{API}/wallets/me/transfer/', {'recipient': '@bola', 'amount': amount},
                                   format='json')
        assert response.status_code == 400, amount


def test_errors_never_leak_provider_internals(api_client, funded_wallet, bank_account, flw):
    flw.add('POST', 'transfers', body={'status': 'error', 'message': 'internal db host 10.1.2.3'}, status=500)
    response = api_client.post(f'{API}/wallets/me/withdraw/', {'amount': '1000'}, format='json')
    # Unknown outcome: accepted and pending, never an error that invites a retry
    assert response.status_code == 202
    assert '10.1.2.3' not in json.dumps(response.json())


def test_withdraw_uses_own_default_account_only(api_client, funded_wallet, bank_account, other_user, flw):
    foreign = BankAccount.objects.create(user=other_user, account_bank='044', account_number='9', currency='NGN',
                                         account_name='Thief')
    response = api_client.post(f'{API}/wallets/me/withdraw/', {'amount': '1000', 'bank_account_id': str(foreign.pk)},
                               format='json')
    assert response.status_code == 404


def test_throttling(api_client, wallet, settings, flw):
    settings.FLW_WALLET_THROTTLE_RATES = {'lookup': '2/min'}
    for _ in range(2):
        api_client.get(f'{API}/wallets/lookup/?recipient=@nobody')
    assert api_client.get(f'{API}/wallets/lookup/?recipient=@nobody').status_code == 429


def test_transactions_and_statement(api_client, user, funded_wallet, other_wallet, service):
    service.transfer(funded_wallet, other_wallet, '100')
    data = api_client.get(f'{API}/wallets/me/transactions/?direction=debit').json()
    assert data['count'] == 1
    statement = api_client.get(f'{API}/wallets/me/statement/').json()['results']
    assert [row['balance_after'] for row in statement] == ['100000.00', '99900.00']
    assert api_client.get(f'{API}/transactions/').json()['count'] == 2
    assert api_client.get(f'{API}/wallets/me/transactions/?type=nonsense').status_code == 400


def test_users_cannot_act_on_others_transactions(api_client, other_wallet, wallet, service):
    fund(other_wallet, 1000)
    txn = Transaction.objects.get(wallet=other_wallet)
    assert api_client.get(f'{API}/transactions/{txn.pk}/').status_code == 404
    assert api_client.post(f'{API}/transactions/{txn.pk}/refund/').status_code == 403
    assert api_client.post(f'{API}/transactions/{txn.pk}/reverse/').status_code == 403


def test_escrow_release_by_buyer_and_cancel_by_merchant(api_client, user, other_user, funded_wallet, other_wallet,
                                                          service):
    held = service.pay(funded_wallet, '500', merchant_wallet=other_wallet, escrow=True)
    response = api_client.post(f'{API}/transactions/{held.pk}/release/')
    assert response.status_code == 200 and response.json()['status'] == 'success'

    second = service.pay(funded_wallet, '500', merchant_wallet=other_wallet, escrow=True)
    assert api_client.post(f'{API}/transactions/{second.pk}/cancel/').status_code == 403  # buyer can't
    merchant = APIClient()
    merchant.force_authenticate(other_user)
    assert merchant.post(f'{API}/transactions/{second.pk}/release/').status_code == 404  # merchant can't release
    assert merchant.post(f'{API}/transactions/{second.pk}/cancel/', {'reason': 'Sold out'},
                         format='json').status_code == 200


def test_staff_refund_and_review_queue(staff_client, wallet, service, flw):
    flw.add('POST', 'payments', {'link': 'x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    deposit = service.settle_charge(charge_data(reference, 5000, flw_id=6001))
    flw.add('POST', 'transactions/6001/refund', {'id': 1, 'status': 'completed'})
    response = staff_client.post(f'{API}/transactions/{deposit.pk}/refund/', {'amount': '100'}, format='json')
    assert response.status_code == 201 and response.json()['status'] == 'success'
    Transaction.objects.filter(pk=deposit.pk).update(needs_review=True)
    assert staff_client.get(f'{API}/transactions/review-queue/').json()['count'] == 1


def test_profile_endpoints(api_client, user, wallet):
    assert api_client.get(f'{API}/profile/').json()['tag'] == 'ada'
    response = api_client.patch(f'{API}/profile/', {'tag': 'ada_obi', 'phone_number': '08031234567'}, format='json')
    assert response.json()['tag'] == 'ada_obi' and response.json()['phone_number'] == '+2348031234567'
    response = api_client.post(f'{API}/profile/set-pin/', {'pin': '1357'}, format='json')
    assert response.status_code == 200
    response = api_client.post(f'{API}/profile/set-pin/', {'pin': '2468'}, format='json')
    assert response.status_code == 403  # changing needs the current PIN


def test_lookup_and_fee_quote(api_client, wallet, other_wallet, settings):
    info = api_client.get(f'{API}/wallets/lookup/?recipient=@bola').json()
    assert info['tag'] == 'bola' and info['can_receive']
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'transfer': {'percentage': '1', 'bearer': 'customer'}}
    quote = api_client.post(f'{API}/wallets/fee-quote/', {'amount': '1000', 'operation': 'transfer'},
                            format='json').json()
    assert quote['fee'] == '10.00' and quote['customer_pays'] == '1010.00'


def test_feature_switch_on_endpoint(api_client, wallet, settings):
    settings.FLW_WALLET_ENABLE_BILL_PAYMENTS = False
    assert api_client.get(f'{API}/bills/categories/').status_code == 403


def test_bank_account_endpoints(api_client, user, bank, flw):
    flw.add('POST', 'accounts/resolve', {'account_number': '0690000031', 'account_name': 'ADA OBI'})
    response = api_client.post(f'{API}/bank-accounts/', {'account_bank': '044', 'account_number': '0690000031'},
                               format='json')
    assert response.status_code == 201
    body = response.json()
    assert body['account_name'] == 'ADA OBI' and body['account_number'] == '******0031'
    assert api_client.delete(f"{API}/bank-accounts/{body['id']}/").status_code == 204
    assert api_client.get(f'{API}/bank-accounts/').json()['count'] == 0


def test_direct_charge_endpoint_strips_protected_fields(api_client, wallet, flw):
    flw.add('POST', 'charges', body={'status': 'success', 'message': 'Charge initiated',
                                     'data': {'id': 4001, 'flw_ref': 'R', 'status': 'pending'},
                                     'meta': {'authorization': {'mode': 'redirect', 'redirect': 'https://bank'}}})
    response = api_client.post(f'{API}/wallets/me/charge/', {
        'amount': '1000', 'charge_type': 'mpesa',
        'payload': {'phone_number': '254700000000', 'amount': '1', 'tx_ref': 'evil', 'meta': {'wallet_id': 'x'}},
    }, format='json')
    assert response.status_code == 202
    assert response.json()['action'] == {'mode': 'redirect', 'redirect': 'https://bank'}
    sent = flw.last_body('charges')
    assert sent['amount'] == 1000 and sent['tx_ref'] != 'evil' and sent['meta']['wallet_id'] == str(wallet.pk)


def test_callback_view_verifies_server_side(client, wallet, service, flw, settings):
    flw.add('POST', 'payments', {'link': 'x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    flw.add('GET', 'transactions/9001/verify', charge_data(reference, 5000))
    response = client.get(f'/wallet/callback/?status=successful&tx_ref={reference}&transaction_id=9001')
    assert response.status_code == 200 and response.json()['status'] == 'success'
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)
    settings.FLW_WALLET_CALLBACK_REDIRECT_URL = 'https://app.example/funded'
    response = client.get(f'/wallet/callback/?tx_ref={reference}')
    assert response.status_code == 302 and response['Location'].startswith('https://app.example/funded?tx_ref=')
    assert client.get('/wallet/callback/?tx_ref=unknown').status_code == 302


def test_callback_cannot_be_used_to_credit_without_payment(client, wallet, service, flw):
    flw.add('POST', 'payments', {'link': 'x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    flw.add('GET', 'transactions/9001/verify', charge_data('different-ref', 5000))
    client.get(f'/wallet/callback/?status=successful&tx_ref={reference}&transaction_id=9001')
    wallet.refresh_from_db()
    assert wallet.balance == money(0)


@pytest.mark.django_db(transaction=True)
def test_views_opt_out_of_atomic_requests(settings, user, bank, flw):
    from django.db import connection

    from flutterwave_wallet.models import BankAccount
    from flutterwave_wallet.services.wallet_service import WalletService
    settings.DATABASES['default']['ATOMIC_REQUESTS'] = True
    connection.settings_dict['ATOMIC_REQUESTS'] = True
    try:
        fund(WalletService().get_wallet(user), 5000)
        BankAccount.objects.create(user=user, bank=bank, account_bank='044', account_number='0690000031',
                                   account_name='ADA OBI', currency='NGN', is_default=True)
        flw.add('POST', 'transfers', {'id': 1, 'status': 'NEW'})
        client = APIClient()
        client.force_authenticate(user)
        response = client.post(f'{API}/wallets/me/withdraw/', {'amount': '1000'}, format='json')
        assert response.status_code == 202
    finally:
        connection.settings_dict['ATOMIC_REQUESTS'] = False
        settings.DATABASES['default']['ATOMIC_REQUESTS'] = False


def test_card_charge_endpoint_is_refused(api_client, wallet, flw):
    response = api_client.post(f'{API}/wallets/me/charge/', {
        'amount': '1000', 'charge_type': 'card', 'payload': {'card_number': '5531886652142950', 'cvv': '564'},
    }, format='json')
    assert response.status_code == 400
    assert not flw.calls('charges')
