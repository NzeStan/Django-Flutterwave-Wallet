"""Every wallet flow running on Flutterwave v4 (FLW_WALLET_API_VERSION='v4')."""
import json

import pytest
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from rest_framework.test import APIClient

from flutterwave_wallet.exceptions import CardDataNotAllowed, FlutterwaveAPIError, InvalidAmount
from flutterwave_wallet.models import BankAccount, Transaction, VirtualAccount, WalletProfile, WebhookEvent
from tests.conftest import charge_data, fund, money, v4_charge, v4_webhook

pytestmark = pytest.mark.django_db


def body_of(call):
    return json.loads(call.request.body)


# ----------------------------------------------------------------------
# Deposits
# ----------------------------------------------------------------------

def start_mobile_money(service, wallet, v4, amount='2000'):
    v4.add('POST', 'orchestration/direct-charges', {
        'id': 'chg_mm0001', 'reference': 'x', 'amount': 2000, 'currency': 'NGN', 'status': 'pending',
        'next_action': {'type': 'payment_instruction',
                        'payment_instruction': {'note': 'Approve the prompt on your phone'}},
    })
    return service.direct_charge(wallet, amount, 'mobile_money',
                                 {'network': 'MTN', 'country_code': '234', 'phone_number': '8031234567'})


def test_direct_charge_uses_orchestrator(wallet, service, v4):
    result = start_mobile_money(service, wallet, v4)
    assert result['status'] == 'pending'
    assert result['action'] == {'mode': 'payment_instruction', 'instruction': 'Approve the prompt on your phone'}
    call = v4.api_calls('orchestration/direct-charges')[0]
    body = body_of(call)
    assert body['reference'] == result['reference'] and body['amount'] == 2000 and body['currency'] == 'NGN'
    assert body['payment_method'] == {'type': 'mobile_money', 'mobile_money': {
        'network': 'MTN', 'country_code': '234', 'phone_number': '8031234567'}}
    assert body['customer']['email'] == 'ada@example.com'
    assert call.request.headers['X-Idempotency-Key'] == f"flw-chg-{result['reference']}"
    txn = result['transaction']
    assert txn.api_version == 'v4' and txn.flw_id == 'chg_mm0001'


def test_signed_v4_webhook_verifies_and_credits_once(wallet, service, v4):
    reference = start_mobile_money(service, wallet, v4)['reference']
    v4.add('GET', 'charges/chg_mm0001', v4_charge(reference, 2000, 'chg_mm0001'))
    payload = {'webhook_id': 'wbk_1', 'timestamp': 1739456704200, 'type': 'charge.completed',
               'data': {'id': 'chg_mm0001', 'reference': reference, 'status': 'succeeded', 'amount': 999999}}
    client = APIClient()
    assert v4_webhook(client, payload).status_code == 200
    assert v4_webhook(client, payload).json()['duplicate'] is True
    wallet.refresh_from_db()
    assert wallet.balance == money(2000)  # the forged amount in the body is ignored
    txn = Transaction.objects.get(reference=reference)
    assert txn.status == 'success' and txn.payment_method == 'mobile_money'
    assert WebhookEvent.objects.get().flw_id == 'chg_mm0001'
    assert service.verify_ledger(wallet) == []


def test_v4_webhook_must_be_hmac_signed(wallet, service, v4):
    reference = start_mobile_money(service, wallet, v4)['reference']
    payload = {'type': 'charge.completed', 'data': {'id': 'chg_mm0001', 'reference': reference}}
    from tests.conftest import webhook
    assert webhook(APIClient(), payload, secret='wrong', mode='hmac').status_code == 401


def test_failed_v4_charge(wallet, service, v4):
    reference = start_mobile_money(service, wallet, v4)['reference']
    v4.add('GET', 'charges/chg_mm0001', v4_charge(reference, 2000, 'chg_mm0001', status='failed'))
    service.verify_and_settle('chg_mm0001', expected_reference=reference)
    assert Transaction.objects.get(reference=reference).status == 'failed'
    wallet.refresh_from_db()
    assert wallet.balance == money(0)


def test_otp_authorization(wallet, service, v4):
    v4.add('POST', 'orchestration/direct-charges', {
        'id': 'chg_acc001', 'reference': 'x', 'amount': 1000, 'currency': 'NGN', 'status': 'pending',
        'next_action': {'type': 'requires_otp', 'requires_otp': {}}})
    result = service.direct_charge(wallet, '1000', 'bank_account', {'account_bank': '044',
                                                                    'account_number': '0690000031'})
    assert result['action']['mode'] == 'requires_otp'
    v4.add('PUT', 'charges/chg_acc001', v4_charge(result['reference'], 1000, 'chg_acc001'))
    v4.add('GET', 'charges/chg_acc001', v4_charge(result['reference'], 1000, 'chg_acc001'))
    outcome = service.validate_charge(result['transaction'], '123456')
    assert outcome['status'] == 'success'
    assert body_of(v4.api_calls('charges/chg_acc001', 'PUT')[0]) == {
        'authorization': {'type': 'otp', 'otp': {'code': '123456'}}}


def test_reconcile_v4_deposit_by_reference(wallet, service, v4):
    v4.add('POST', 'orchestration/direct-charges', body={'status': 'failed', 'message': 'timeout'}, status=504)
    result = service.direct_charge(wallet, '1500', 'ussd', {'account_bank': '044'})
    assert result['status'] == 'pending' and result['transaction'].needs_review
    v4.add('GET', 'charges', [v4_charge(result['reference'], 1500, 'chg_ussd01', method='ussd')])
    service.reconcile_deposit(Transaction.objects.get(reference=result['reference']))
    wallet.refresh_from_db()
    assert wallet.balance == money(1500)


@pytest.mark.parametrize('charge_type', ['card', 'applepay', 'googlepay'])
def test_card_types_refused_on_v4(wallet, service, v4, charge_type):
    with pytest.raises(CardDataNotAllowed):
        service.direct_charge(wallet, '1000', charge_type, {})
    assert not v4.api_calls()


def test_v3_only_charge_type_refused_on_v4(wallet, service, v4):
    with pytest.raises(InvalidAmount):
        service.direct_charge(wallet, '1000', 'mpesa', {})


def test_hosted_checkout_still_works_in_v4_mode(wallet, service, v4):
    """Cards are hosted-checkout only, which exists on v3; webhooks route by id format."""
    v4.add_v3('POST', 'payments', {'link': 'https://checkout.flutterwave.com/pay/x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    assert Transaction.objects.get(reference=reference).api_version == 'v3'
    v4.add_v3('GET', 'transactions/9001/verify', charge_data(reference, 5000))
    from tests.conftest import webhook
    webhook(APIClient(), {'event': 'charge.completed', 'data': charge_data(reference, 5000)})
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)


# ----------------------------------------------------------------------
# Virtual accounts
# ----------------------------------------------------------------------

def make_static_account(service, wallet, v4):
    v4.add('POST', 'customers', {'id': 'cus_ada001', 'email': 'ada@example.com'})
    v4.add('POST', 'virtual-accounts', {'id': 'va_ada001', 'account_number': '9288669694',
                                        'account_bank_name': 'Flutterwave MFB', 'account_type': 'static',
                                        'status': 'active', 'note': 'Ada Obi'})
    return service.create_static_account(wallet, bvn='12345678901')


def test_static_virtual_account_on_v4(wallet, service, v4):
    account = make_static_account(service, wallet, v4)
    assert account.account_number == '9288669694' and account.bank_name == 'Flutterwave MFB'
    assert account.api_version == 'v4' and account.order_ref == 'va_ada001'
    assert WalletProfile.objects.get(user=wallet.user).flw_customer_id == 'cus_ada001'
    body = body_of(v4.api_calls('virtual-accounts')[0])
    assert body['customer_id'] == 'cus_ada001' and body['account_type'] == 'static' and body['bvn'] == '12345678901'
    assert '12345678901' not in str(VirtualAccount.objects.filter(pk=account.pk).values().first())
    # the v4 customer is created only once, then reused
    v4.add('POST', 'virtual-accounts', {'id': 'va_dyn001', 'account_number': '9059273981',
                                        'account_bank_name': 'Flutterwave MFB'})
    service.create_bank_transfer_deposit(wallet, '3000')
    assert len(v4.api_calls('customers', 'POST')) == 1


def test_transfer_into_v4_static_account_credits_once(wallet, service, v4):
    make_static_account(service, wallet, v4)
    charge = v4_charge('flw-generated-ref-1', 2500, 'chg_va0001', method='bank_transfer', customer_id='cus_ada001')
    v4.add('GET', 'charges/chg_va0001', charge)
    payload = {'type': 'charge.completed', 'data': {'id': 'chg_va0001', 'reference': 'flw-generated-ref-1'}}
    client = APIClient()
    v4_webhook(client, payload)
    v4_webhook(client, payload)
    wallet.refresh_from_db()
    assert wallet.balance == money(2500)
    txn = Transaction.objects.get(reference='VA-chg_va0001')
    assert txn.payment_method == 'virtual_account' and txn.api_version == 'v4'


def test_transfer_from_unknown_customer_is_not_credited(wallet, service, v4):
    make_static_account(service, wallet, v4)
    charge = v4_charge('ref-unknown-01', 2500, 'chg_va0002', method='bank_transfer', customer_id='cus_stranger')
    assert service.settle_charge(service.gateway('v4').normalize_charge(charge)) is None
    wallet.refresh_from_db()
    assert wallet.balance == money(0)


def test_existing_customer_is_found_by_search(wallet, service, v4):
    v4.add('POST', 'customers', status=409, ok=False, message='Customer already exists')
    v4.add('POST', 'customers/search', [{'id': 'cus_existing'}])
    v4.add('POST', 'virtual-accounts', {'id': 'va_1', 'account_number': '9288669694'})
    service.create_static_account(wallet, bvn='12345678901')
    assert WalletProfile.objects.get(user=wallet.user).flw_customer_id == 'cus_existing'


def test_dynamic_bank_transfer_deposit_on_v4(wallet, service, v4):
    WalletProfile.objects.filter(user=wallet.user).update(flw_customer_id='cus_ada001')
    v4.add('POST', 'virtual-accounts', {'id': 'va_dyn001', 'account_number': '9059273981',
                                        'account_bank_name': 'Flutterwave MFB'})
    result = service.create_bank_transfer_deposit(wallet, '3000')
    body = body_of(v4.api_calls('virtual-accounts')[0])
    assert body['account_type'] == 'dynamic' and body['amount'] == 3000 and body['reference'] == result['reference']
    v4.add('GET', 'charges/chg_dyn001', v4_charge(result['reference'], 3000, 'chg_dyn001', method='bank_transfer'))
    service.verify_and_settle('chg_dyn001')
    wallet.refresh_from_db()
    assert wallet.balance == money(3000)


# ----------------------------------------------------------------------
# Withdrawals
# ----------------------------------------------------------------------

def withdraw(service, wallet, account, v4, status='NEW'):
    def respond(request):
        ref = json.loads(request.body)['reference']
        return 201, {}, json.dumps({'status': 'success', 'data': {'id': 'trf_0001', 'reference': ref,
                                                                  'status': status}})
    v4.rsps.add_callback('POST', 'https://developersandbox-api.flutterwave.com/direct-transfers', callback=respond)
    return service.withdraw(wallet, '1000', account)


def test_bank_withdrawal_on_v4(funded_wallet, bank_account, service, v4):
    txn = withdraw(service, funded_wallet, bank_account, v4)
    call = v4.api_calls('direct-transfers')[0]
    body = body_of(call)
    assert body['type'] == 'bank' and body['action'] == 'instant' and body['reference'] == txn.reference
    assert body['payment_instruction'] == {
        'source_currency': 'NGN', 'destination_currency': 'NGN',
        'amount': {'value': 1000, 'applies_to': 'destination_currency'},
        'recipient': {'bank': {'code': '044', 'account_number': '0690000031'}},
    }
    assert call.request.headers['X-Idempotency-Key'] == f'flw-trf-{txn.reference}'
    assert txn.status == 'processing' and txn.flw_id == 'trf_0001' and txn.api_version == 'v4'
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99000)


def test_transfer_disburse_webhook_success_and_failure(funded_wallet, bank_account, service, v4):
    txn = withdraw(service, funded_wallet, bank_account, v4)
    v4.add('GET', 'transfers/trf_0001', {'id': 'trf_0001', 'reference': txn.reference, 'status': 'FAILED'})
    for _ in range(2):
        v4_webhook(APIClient(), {'type': 'transfer.disburse',
                                 'data': {'id': 'trf_0001', 'reference': txn.reference, 'status': 'SUCCESSFUL'}})
    txn.refresh_from_db()
    assert txn.status == 'failed'  # the API's answer wins over the webhook body
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)
    assert Transaction.objects.filter(reference=f'{txn.reference}-REV').count() == 1


def test_transfer_reversal_after_success(funded_wallet, bank_account, service, v4):
    txn = withdraw(service, funded_wallet, bank_account, v4, status='SUCCESSFUL')
    assert txn.status == 'success'
    v4.add('GET', 'transfers/trf_0001', {'id': 'trf_0001', 'reference': txn.reference, 'status': 'REVERSED'})
    v4_webhook(APIClient(), {'type': 'transfer.reversal', 'data': {'id': 'trf_0001', 'reference': txn.reference}})
    txn.refresh_from_db()
    assert txn.status == 'reversed'
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_reversal_event_while_api_says_successful_is_flagged(funded_wallet, bank_account, service, v4):
    txn = withdraw(service, funded_wallet, bank_account, v4, status='SUCCESSFUL')
    v4.add('GET', 'transfers/trf_0001', {'id': 'trf_0001', 'reference': txn.reference, 'status': 'SUCCESSFUL'})
    v4_webhook(APIClient(), {'type': 'transfer.reversal', 'data': {'id': 'trf_0001', 'reference': txn.reference}})
    txn.refresh_from_db()
    assert txn.status == 'success' and txn.needs_review


def test_unknown_outcome_recovered_by_idempotent_replay(funded_wallet, bank_account, service, v4):
    v4.add('POST', 'direct-transfers', body={'status': 'failed', 'message': 'gateway timeout'}, status=504)
    v4.add('POST', 'direct-transfers', body={'status': 'failed', 'message': 'gateway timeout'}, status=504)
    v4.add('POST', 'direct-transfers', body={'status': 'failed', 'message': 'gateway timeout'}, status=504)
    txn = service.withdraw(funded_wallet, '1000', bank_account)
    assert txn.is_pending and txn.needs_review and not txn.flw_id
    keys = {c.request.headers['X-Idempotency-Key'] for c in v4.api_calls('direct-transfers')}
    assert keys == {f'flw-trf-{txn.reference}'}  # every retry reused the same key
    v4.rsps.replace('POST', 'https://developersandbox-api.flutterwave.com/direct-transfers',
                    json={'status': 'success', 'data': {'id': 'trf_late', 'reference': txn.reference,
                                                        'status': 'SUCCESSFUL'}})
    service.verify_withdrawal(txn)
    txn.refresh_from_db()
    assert txn.status == 'success' and txn.flw_id == 'trf_late'


def test_definitive_rejection_refunds(funded_wallet, bank_account, service, v4):
    v4.add('POST', 'direct-transfers', status=400, ok=False, message='Invalid account number')
    with pytest.raises(FlutterwaveAPIError):
        service.withdraw(funded_wallet, '1000', bank_account)
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_mobile_money_withdrawal_on_v4(user, service, v4):
    wallet = fund(service.get_wallet(user, 'KES'), 5000)
    account = service.add_mobile_money_account(user, 'MPESA', '254700000000', 'KES', 'Ada Obi', country='KE')
    def respond(request):
        ref = json.loads(request.body)['reference']
        return 201, {}, json.dumps({'status': 'success', 'data': {'id': 'trf_mm', 'reference': ref, 'status': 'NEW'}})
    v4.rsps.add_callback('POST', 'https://developersandbox-api.flutterwave.com/direct-transfers', callback=respond)
    service.withdraw(wallet, '500', account)
    body = body_of(v4.api_calls('direct-transfers')[0])
    assert body['type'] == 'mobile_money'
    assert body['payment_instruction']['recipient'] == {
        'mobile_money': {'network': 'MPESA', 'msisdn': '254700000000', 'country': 'KE'},
        'name': {'first': 'Ada', 'last': 'Obi'}}
    assert body['payment_instruction']['source_currency'] == 'KES'


def test_flutterwave_fee_source_not_available_on_v4(funded_wallet, bank_account, service, v4, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'withdrawal': {'source': 'flutterwave', 'bearer': 'customer'}}
    with pytest.raises(ImproperlyConfigured):
        service.withdraw(funded_wallet, '1000', bank_account)


# ----------------------------------------------------------------------
# Refunds, banks, balances
# ----------------------------------------------------------------------

def test_refund_on_v4(wallet, service, v4):
    reference = start_mobile_money(service, wallet, v4)['reference']
    v4.add('GET', 'charges/chg_mm0001', v4_charge(reference, 2000, 'chg_mm0001'))
    deposit = service.verify_and_settle('chg_mm0001')
    v4.add('POST', 'refunds', {'id': 'rfd_0001', 'status': 'new', 'charge_id': 'chg_mm0001'})
    refund = service.refund_deposit(deposit, '500', reason='Customer request')
    body = body_of(v4.api_calls('refunds', 'POST')[0])
    assert body['charge_id'] == 'chg_mm0001' and body['amount'] == 500 and body['reason'] == 'requested_by_customer'
    assert refund.status == 'processing' and refund.api_version == 'v4'
    v4.add('GET', 'refunds/rfd_0001', {'id': 'rfd_0001', 'status': 'succeeded'})
    v4_webhook(APIClient(), {'type': 'refund.completed', 'data': {'id': 'rfd_0001', 'status': 'succeeded'}})
    refund.refresh_from_db()
    assert refund.status == 'success'
    wallet.refresh_from_db()
    assert wallet.balance == money(1500)


def test_resolve_and_add_bank_account_on_v4(user, bank, service, v4):
    v4.add('POST', 'banks/account-resolve', {'bank_code': '044', 'account_number': '0690000031',
                                             'account_name': 'ADA OBI'})
    account = service.add_bank_account(user, '044', '0690000031', account_name='Someone Else')
    assert account.account_name == 'ADA OBI' and account.is_verified
    assert body_of(v4.api_calls('banks/account-resolve')[0]) == {
        'currency': 'NGN', 'account': {'code': '044', 'number': '0690000031'}}


def test_sync_banks_and_balances_on_v4(service, v4):
    v4.add('GET', 'banks', [{'id': 'bnk_1', 'code': '044', 'name': 'Access Bank'}])
    assert service.sync_banks(['NG']) == (1, 0)
    assert 'country=NG' in v4.api_calls('banks')[0].request.url
    v4.add('GET', 'wallets/balances', [{'currency': 'NGN', 'available_balance': 100}])
    assert service.flutterwave_balances() == [{'currency': 'NGN', 'available_balance': 100}]


# ----------------------------------------------------------------------
# Configuration checks
# ----------------------------------------------------------------------

def test_v4_system_checks(settings):
    settings.FLW_WALLET_API_VERSION = 'v4'
    settings.FLUTTERWAVE_CLIENT_ID = ''
    settings.FLW_WALLET_WEBHOOK_SIGNATURE_MODE = 'hash'
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'withdrawal': {'source': 'flutterwave'}}
    settings.FLUTTERWAVE_SECRET_KEY = ''
    ids = {m.id for m in run_checks()}
    assert {'flutterwave_wallet.E012', 'flutterwave_wallet.E013', 'flutterwave_wallet.E015',
            'flutterwave_wallet.W012'} <= ids
    settings.FLW_WALLET_API_VERSION = 'v5'
    assert 'flutterwave_wallet.E011' in {m.id for m in run_checks()}


def test_payout_account_validation_uses_v4_lookup_for_ghs(user, service, v4):
    v4.add('POST', 'banks/account-resolve', {'account_number': '1234567890', 'account_name': 'KOFI'})
    account = service.add_bank_account(user, 'GH280100', '1234567890', currency='GHS')
    assert account.account_name == 'KOFI'
    assert BankAccount.objects.get(pk=account.pk).country == 'GH'
