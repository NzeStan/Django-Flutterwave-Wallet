import json

import pytest

from flutterwave_wallet.exceptions import FlutterwaveAPIError, InvalidAmount, InvalidTransactionState, KYCError, WalletError
from flutterwave_wallet.models import Transaction, WalletProfile
from tests.conftest import charge_data, money

pytestmark = pytest.mark.django_db

PAY_URL = 'billers/BIL099/items/AT099/payment'


def validate_ok(flw):
    flw.add('GET', 'bill-items/AT099/validate', {'name': 'MTN', 'response_code': '00'})


# ----------------------------------------------------------------------
# Bills
# ----------------------------------------------------------------------

def test_bill_payment_success(funded_wallet, service, flw):
    validate_ok(flw)
    flw.add('POST', PAY_URL, {'phone_number': '+2348031234567', 'amount': 500, 'flw_ref': 'BPUSSD1',
                              'tx_ref': 'x', 'status': 'success'})
    txn = service.pay_bill(funded_wallet, 'BIL099', 'AT099', '08031234567', '500')
    assert txn.status == 'success' and txn.flw_ref == 'BPUSSD1'
    body = flw.last_body(PAY_URL)
    assert body == {'customer_id': '08031234567', 'amount': 500, 'country': 'NG', 'reference': txn.reference}
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99500)


def test_bill_payment_pending_then_verified_failure_refunds(funded_wallet, service, flw):
    validate_ok(flw)
    flw.add('POST', PAY_URL, {'flw_ref': 'BP2'})
    txn = service.pay_bill(funded_wallet, 'BIL099', 'AT099', '08031234567', '500')
    assert txn.status == 'processing'
    flw.add('GET', f'bills/{txn.reference}', {'status': 'failed', 'message': 'Provider down'})
    service.handle_bill_webhook({'tx_ref': txn.reference, 'status': 'successful'})  # body not trusted
    txn.refresh_from_db()
    assert txn.status == 'failed'
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_bill_rejection_and_unknown_outcome(funded_wallet, service, flw):
    validate_ok(flw)
    flw.error('POST', PAY_URL, 'Invalid customer')
    with pytest.raises(FlutterwaveAPIError):
        service.pay_bill(funded_wallet, 'BIL099', 'AT099', '08031234567', '500')
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)

    flw.rsps.reset()
    validate_ok(flw)
    flw.add('POST', PAY_URL, body={'status': 'error', 'message': 'timeout'}, status=503)
    txn = service.pay_bill(funded_wallet, 'BIL099', 'AT099', '08031234567', '500')
    assert txn.is_pending and txn.needs_review


def test_bill_customer_validation_failure_moves_no_money(funded_wallet, service, flw):
    flw.error('GET', 'bill-items/AT099/validate', 'Invalid customer number')
    with pytest.raises(WalletError):
        service.pay_bill(funded_wallet, 'BIL099', 'AT099', '0000', '500')
    assert not Transaction.objects.filter(transaction_type='bill_payment').exists()


def test_bill_catalogue_passthrough(service, flw):
    flw.add('GET', 'top-bill-categories', [{'code': 'AIRTIME'}])
    flw.add('GET', 'bills/AIRTIME/billers', [{'biller_code': 'BIL099'}])
    flw.add('GET', 'billers/BIL099/items', [{'item_code': 'AT099'}])
    assert service.bill_categories() == [{'code': 'AIRTIME'}]
    assert flw.calls('top-bill-categories')[0].request.url.endswith('country=NG')
    assert service.billers('AIRTIME') == [{'biller_code': 'BIL099'}]
    assert service.biller_items('BIL099') == [{'item_code': 'AT099'}]


# ----------------------------------------------------------------------
# Refunds
# ----------------------------------------------------------------------

@pytest.fixture
def deposit(wallet, service, flw):
    flw.add('POST', 'payments', {'link': 'https://x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    return service.settle_charge(charge_data(reference, 5000, flw_id=6001))


def test_partial_and_full_refunds(deposit, service, flw, wallet):
    flw.add('POST', 'transactions/6001/refund', {'id': 77, 'status': 'completed', 'amount_refunded': 2000})
    refund = service.refund_deposit(deposit, '2000', reason='Customer request')
    assert refund.status == 'success' and refund.flw_id == '77'
    assert flw.last_body('transactions/6001/refund') == {'amount': 2000, 'comments': 'Customer request'}
    with pytest.raises(InvalidAmount):
        service.refund_deposit(deposit, '3000.01')
    flw.rsps.reset()
    flw.add('POST', 'transactions/6001/refund', {'id': 78, 'status': 'pending'})
    second = service.refund_deposit(deposit)
    assert second.status == 'processing' and second.total_amount == money(3000)
    wallet.refresh_from_db()
    assert wallet.balance == money(0)
    flw.add('GET', 'refunds/78', {'id': 78, 'status': 'failed', 'comments': 'Card closed'})
    service.verify_refund(second)
    second.refresh_from_db()
    assert second.status == 'failed'
    wallet.refresh_from_db()
    assert wallet.balance == money(3000)
    assert service.verify_ledger(wallet) == []


def test_refund_rejected_puts_money_back(deposit, service, flw, wallet):
    flw.error('POST', 'transactions/6001/refund', 'Transaction is too old to refund')
    with pytest.raises(FlutterwaveAPIError):
        service.refund_deposit(deposit)
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)


def test_refund_requires_successful_deposit(funded_wallet, service):
    with pytest.raises(InvalidTransactionState):
        service.refund_deposit(Transaction.objects.get(wallet=funded_wallet))


def test_refund_webhook(deposit, service, flw):
    flw.add('POST', 'transactions/6001/refund', {'id': 79, 'status': 'pending'})
    refund = service.refund_deposit(deposit, '100')
    flw.add('GET', 'refunds/79', {'id': 79, 'status': 'completed'})
    service.handle_refund_webhook({'id': 79})
    refund.refresh_from_db()
    assert refund.status == 'success'


# ----------------------------------------------------------------------
# KYC
# ----------------------------------------------------------------------

def test_bvn_verification_flow(user, service, flw):
    flw.add('POST', 'bvn/verifications', {'url': 'https://nibss/consent', 'reference': 'FLWBVN1'})
    result = service.start_bvn_verification(user, '12345678901', 'Ada', 'Obi')
    assert result['url'] == 'https://nibss/consent'
    profile = WalletProfile.objects.get(user=user)
    assert profile.kyc_status == 'pending' and profile.bvn_reference == 'FLWBVN1'
    flw.add('GET', 'bvn/verifications/FLWBVN1', {'status': 'COMPLETED', 'first_name': 'ADA', 'last_name': 'OBI',
                                                 'bvn_data': {'bvn': '12345678901', 'dob': '1990-01-01'}})
    service.handle_bvn_webhook({'reference': 'FLWBVN1'})
    profile.refresh_from_db()
    assert profile.kyc_status == 'verified' and profile.kyc_data == {'first_name': 'ADA', 'last_name': 'OBI',
                                                                       'status': 'COMPLETED'}
    assert '12345678901' not in json.dumps(profile.kyc_data)


def test_bvn_must_be_11_digits(user, service):
    with pytest.raises(KYCError):
        service.start_bvn_verification(user, '123', 'A', 'B')
