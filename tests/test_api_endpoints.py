"""Every remaining REST endpoint, end to end."""
import json

import pytest

from flutterwave_wallet.models import Card, Transaction, WebhookDeliveryAttempt, WebhookEndpoint, WebhookEvent
from tests.conftest import charge_data, money

pytestmark = pytest.mark.django_db

API = '/wallet/api'


def test_balance_and_retrieve(api_client, funded_wallet):
    assert api_client.get(f'{API}/wallets/me/balance/').json()['balance'] == '100000.00'
    assert api_client.get(f'{API}/wallets/{funded_wallet.pk}/').json()['balance'] == '100000.00'


def test_bank_transfer_deposit_and_verify(api_client, wallet, flw):
    flw.add('POST', 'virtual-account-numbers', {'account_number': '0067100155', 'bank_name': 'Mock Bank'})
    response = api_client.post(f'{API}/wallets/me/bank-transfer-deposit/', {'amount': '3000'}, format='json')
    assert response.status_code == 201
    reference = response.json()['reference']
    flw.add('GET', 'transactions/verify_by_reference', charge_data(reference, 3000, payment_type='bank_transfer'))
    response = api_client.post(f'{API}/wallets/me/verify-deposit/', {'reference': reference}, format='json')
    assert response.status_code == 200 and response.json()['status'] == 'success'
    assert api_client.post(f'{API}/wallets/me/verify-deposit/', {'reference': 'nope-0000'},
                           format='json').status_code == 404


def test_charge_card_endpoint(api_client, wallet, card, flw):
    flw.add('POST', 'tokenized-charges', {'id': 5001, 'status': 'pending'})
    response = api_client.post(f'{API}/wallets/me/charge-card/', {'amount': '700'}, format='json')
    assert response.status_code == 202 and response.json()['status'] == 'pending'
    Card.objects.all().update(is_active=False)
    response = api_client.post(f'{API}/wallets/me/charge-card/', {'amount': '700'}, format='json')
    assert response.status_code == 404


def test_validate_charge_endpoint(api_client, wallet, flw):
    flw.add('POST', 'charges', body={'status': 'success', 'message': 'ok',
                                     'data': {'id': 4001, 'flw_ref': 'R1', 'status': 'pending'},
                                     'meta': {'authorization': {'mode': 'otp'}}})
    reference = api_client.post(f'{API}/wallets/me/charge/', {'amount': '1000', 'charge_type': 'account',
                                                               'payload': {'account_bank': '044'}},
                                format='json').json()['reference']
    flw.add('POST', 'validate-charge', {'id': 4001, 'status': 'failed', 'processor_response': 'Wrong OTP'})
    response = api_client.post(f'{API}/wallets/me/validate-charge/', {'reference': reference, 'otp': '1'},
                               format='json')
    assert response.status_code == 400 and response.json()['status'] == 'failed'


def test_virtual_account_endpoint(api_client, wallet, flw):
    assert api_client.get(f'{API}/wallets/me/virtual-account/').status_code == 404
    flw.add('POST', 'virtual-account-numbers', {'account_number': '7824822527', 'bank_name': 'WEMA BANK'})
    response = api_client.post(f'{API}/wallets/me/virtual-account/', {'bvn': '12345678901'}, format='json')
    assert response.status_code == 201 and 'bvn' not in response.json()
    assert api_client.get(f'{API}/wallets/me/virtual-account/').json()['account_number'] == '7824822527'
    assert api_client.get(f'{API}/wallets/me/').json()['virtual_account']['account_number'] == '7824822527'
    assert api_client.post(f'{API}/wallets/me/virtual-account/', {'bvn': '123'}, format='json').status_code == 400


def test_pay_and_pay_bill_endpoints(api_client, funded_wallet, other_wallet, flw):
    response = api_client.post(f'{API}/wallets/me/pay/', {'amount': '100', 'merchant': '@bola', 'escrow': True},
                               format='json')
    assert response.status_code == 201 and response.json()['status'] == 'pending'
    flw.add('GET', 'bill-items/AT099/validate', {'name': 'MTN'})
    flw.add('POST', 'billers/BIL099/items/AT099/payment', {'status': 'success', 'flw_ref': 'B1'})
    response = api_client.post(f'{API}/wallets/me/pay-bill/', {'biller_code': 'BIL099', 'item_code': 'AT099',
                                                                'customer_id': '08031234567', 'amount': '500'},
                               format='json')
    assert response.status_code == 200 and response.json()['transaction_type'] == 'bill_payment'


def test_withdraw_endpoint_success(api_client, funded_wallet, bank_account, flw):
    def respond(request):
        ref = json.loads(request.body)['reference']
        return 200, {}, json.dumps({'status': 'success', 'data': {'id': 1, 'reference': ref, 'status': 'NEW'}})
    flw.rsps.add_callback('POST', 'https://api.flutterwave.com/v3/transfers', callback=respond)
    response = api_client.post(f'{API}/wallets/me/withdraw/', {'amount': '1000'}, format='json')
    assert response.status_code == 202 and response.json()['bank_account']['account_number'] == '******0031'


def test_cards_endpoints(api_client, card):
    assert api_client.get(f'{API}/cards/').json()['count'] == 1
    data = api_client.get(f'{API}/cards/{card.pk}/').json()
    assert 'token' not in data and data['last_4digits'] == '2950'
    assert api_client.post(f'{API}/cards/{card.pk}/set-default/').status_code == 200
    assert api_client.delete(f'{API}/cards/{card.pk}/').status_code == 204
    card.refresh_from_db()
    assert not card.is_active and card.token == ''


def test_banks_and_resolve(api_client, bank, flw):
    assert api_client.get(f'{API}/banks/?country=NG').json()[0]['code'] == '044'
    flw.add('POST', 'accounts/resolve', {'account_number': '0690000031', 'account_name': 'ADA OBI'})
    response = api_client.post(f'{API}/bank-accounts/resolve/', {'account_bank': '044',
                                                                  'account_number': '0690000031'}, format='json')
    assert response.json()['account_name'] == 'ADA OBI'
    assert api_client.post(f'{API}/bank-accounts/resolve/', {'account_bank': '044', 'account_number': 'abc'},
                           format='json').status_code == 400


def test_bank_account_set_default(api_client, bank_account, user):
    from flutterwave_wallet.models import BankAccount
    second = BankAccount.objects.create(user=user, account_bank='058', account_number='1', currency='NGN',
                                        account_name='ADA OBI')
    assert api_client.post(f'{API}/bank-accounts/{second.pk}/set-default/').json()['is_default'] is True
    bank_account.refresh_from_db()
    assert not bank_account.is_default


def test_bills_endpoints(api_client, wallet, flw):
    flw.add('GET', 'top-bill-categories', [{'code': 'AIRTIME'}])
    flw.add('GET', 'bills/AIRTIME/billers', [{'biller_code': 'BIL099'}])
    flw.add('GET', 'billers/BIL099/items', [{'item_code': 'AT099'}])
    flw.add('GET', 'bill-items/AT099/validate', {'name': 'MTN'})
    assert api_client.get(f'{API}/bills/categories/').json() == [{'code': 'AIRTIME'}]
    assert api_client.get(f'{API}/bills/billers/?category=AIRTIME').json() == [{'biller_code': 'BIL099'}]
    assert api_client.get(f'{API}/bills/billers/').status_code == 400
    assert api_client.get(f'{API}/bills/items/?biller_code=BIL099').json() == [{'item_code': 'AT099'}]
    assert api_client.get(f'{API}/bills/items/').status_code == 400
    assert api_client.post(f'{API}/bills/validate/', {'item_code': 'AT099', 'customer_id': '080'},
                           format='json').json() == {'name': 'MTN'}


def test_kyc_endpoints(api_client, user, wallet, flw):
    flw.add('POST', 'bvn/verifications', {'url': 'https://consent', 'reference': 'R1'})
    response = api_client.post(f'{API}/profile/verify-bvn/', {'bvn': '12345678901', 'firstname': 'Ada',
                                                               'lastname': 'Obi'}, format='json')
    assert response.status_code == 202 and response.json()['url'] == 'https://consent'
    flw.add('GET', 'bvn/verifications/R1', {'status': 'COMPLETED', 'first_name': 'ADA'})
    assert api_client.post(f'{API}/profile/refresh-kyc/').json()['kyc_status'] == 'verified'


def test_staff_transaction_tools(staff_client, funded_wallet, other_wallet, bank_account, service, flw):
    debit = service.transfer(funded_wallet, other_wallet, '100')
    response = staff_client.post(f'{API}/transactions/{debit.pk}/reverse/', {'reason': 'Fraud'}, format='json')
    assert response.status_code == 201 and 'entry_hash' in response.json()

    flw.add('POST', 'transfers', body={'status': 'error', 'message': 't'}, status=504)
    withdrawal = service.withdraw(funded_wallet, '1000', bank_account)
    flw.add('GET', 'transfers', [{'id': 9, 'reference': withdrawal.reference, 'status': 'SUCCESSFUL'}])
    assert staff_client.post(f'{API}/transactions/{withdrawal.pk}/requery/').json()['status'] == 'success'


def test_staff_force_reverse(staff_client, funded_wallet, bank_account, service, flw):
    flw.add('POST', 'transfers', body={'status': 'error', 'message': 't'}, status=504)
    withdrawal = service.withdraw(funded_wallet, '1000', bank_account)
    response = staff_client.post(f'{API}/transactions/{withdrawal.pk}/force-reverse/', {'reason': 'Never left'},
                                 format='json')
    assert response.status_code == 200 and response.json()['status'] == 'failed'
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_staff_webhook_tools(staff_client, api_client, flw):
    import responses as responses_lib
    event = WebhookEvent.objects.create(event_type='subscription.cancelled', payload={'data': {'id': 1}},
                                        idempotency_key='k1')
    assert api_client.get(f'{API}/webhook-events/').status_code == 403
    assert staff_client.get(f'{API}/webhook-events/?processed=false').json()['count'] == 1
    assert staff_client.post(f'{API}/webhook-events/{event.pk}/reprocess/').json()['processed'] is True

    response = staff_client.post(f'{API}/webhook-endpoints/', {'name': 'erp', 'url': 'http://erp.local/hook',
                                                                'secret': 'x' * 30}, format='json')
    assert response.status_code == 400  # https required outside DEBUG
    response = staff_client.post(f'{API}/webhook-endpoints/', {'name': 'erp', 'url': 'https://erp.example/hook',
                                                                'secret': 'x' * 30}, format='json')
    assert response.status_code == 201 and 'secret' not in response.json()
    endpoint = WebhookEndpoint.objects.get()
    flw.rsps.add(responses_lib.POST, 'https://erp.example/hook', status=500)
    attempt = staff_client.post(f'{API}/webhook-endpoints/{endpoint.pk}/test/').json()
    assert attempt['is_success'] is False
    flw.rsps.replace(responses_lib.POST, 'https://erp.example/hook', status=200)
    retried = staff_client.post(f"{API}/webhook-deliveries/{attempt['id']}/retry/").json()
    assert retried['is_success'] is True
    assert staff_client.post(f"{API}/webhook-deliveries/{retried['id']}/retry/").status_code == 400
    assert WebhookDeliveryAttempt.objects.count() == 2
