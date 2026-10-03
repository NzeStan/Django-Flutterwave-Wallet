from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from flutterwave_wallet.exceptions import FlutterwaveAPIError, InvalidAmount, InvalidTransactionState
from flutterwave_wallet.models import Card, PlatformFee, Transaction, VirtualAccount
from flutterwave_wallet.services.deposit_service import DepositService
from flutterwave_wallet.services.virtual_account_service import VirtualAccountService
from tests.conftest import charge_data, money

pytestmark = pytest.mark.django_db


def start_checkout(service, wallet, flw, amount='5000', **kwargs):
    flw.add('POST', 'payments', {'link': 'https://checkout.flutterwave.com/v3/hosted/pay/abc'})
    return service.initialize_checkout(wallet, amount, **kwargs)


# ----------------------------------------------------------------------
# Checkout
# ----------------------------------------------------------------------

def test_checkout_creates_pending_deposit_and_sends_correct_request(wallet, service, flw):
    result = start_checkout(service, wallet, flw, redirect_url='https://shop.example/paid')
    assert result['link'].startswith('https://checkout.flutterwave.com')
    txn = Transaction.objects.get(reference=result['reference'])
    assert txn.status == 'pending' and txn.transaction_type == 'deposit' and not txn.is_posted
    body = flw.last_body('payments')
    assert body['tx_ref'] == result['reference']
    assert body['amount'] == 5000 and body['currency'] == 'NGN'
    assert body['customer']['email'] == 'ada@example.com'
    assert body['meta']['wallet_id'] == str(wallet.pk)
    assert body['redirect_url'] == 'https://shop.example/paid'
    wallet.refresh_from_db()
    assert wallet.balance == money(0)


def test_checkout_rejection_marks_deposit_failed(wallet, service, flw):
    flw.error('POST', 'payments', 'Invalid currency')
    with pytest.raises(FlutterwaveAPIError):
        service.initialize_checkout(wallet, '5000')
    assert Transaction.objects.get(wallet=wallet).status == 'failed'


def test_checkout_respects_minimum_and_maximum(wallet, service, settings):
    with pytest.raises(InvalidAmount):
        service.initialize_checkout(wallet, '50')
    settings.FLW_WALLET_MAXIMUM_TRANSACTION_AMOUNT = 1000
    from flutterwave_wallet.exceptions import TransactionLimitExceeded
    with pytest.raises(TransactionLimitExceeded):
        service.initialize_checkout(wallet, '1001')


def test_caller_reference_must_be_valid_and_unused(wallet, service, flw):
    from flutterwave_wallet.exceptions import DuplicateReference, InvalidReference
    with pytest.raises(InvalidReference):
        service.initialize_checkout(wallet, '500', reference='bad ref!')
    start_checkout(service, wallet, flw, reference='order-0000001')
    with pytest.raises(DuplicateReference):
        service.initialize_checkout(wallet, '500', reference='order-0000001')


# ----------------------------------------------------------------------
# Settlement
# ----------------------------------------------------------------------

def test_verified_success_credits_exactly_once(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    flw.add('GET', 'transactions/9001/verify', charge_data(reference, 5000))
    service.verify_and_settle(9001)
    service.verify_and_settle(9001)
    service.settle_charge(charge_data(reference, 5000))
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)
    txn = Transaction.objects.get(reference=reference)
    assert txn.status == 'success' and txn.flw_id == '9001' and txn.ledger_sequence == 1
    assert txn.payment_method == 'card'
    assert service.verify_ledger(wallet) == []


def test_verify_rejects_reference_mismatch(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    flw.add('GET', 'transactions/9001/verify', charge_data('someone-elses-ref', 5000))
    assert service.verify_and_settle(9001, expected_reference=reference) is None
    wallet.refresh_from_db()
    assert wallet.balance == money(0)


def test_underpayment_credits_what_arrived_and_flags_review(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    txn = service.settle_charge(charge_data(reference, 100))
    wallet.refresh_from_db()
    assert wallet.balance == money(100)
    assert txn.needs_review and 'Expected 5000' in txn.metadata['review_reason']


def test_currency_mismatch_is_never_credited(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    txn = service.settle_charge(charge_data(reference, 5000, currency='USD'))
    wallet.refresh_from_db()
    assert wallet.balance == money(0)
    assert txn.status == 'failed' and txn.needs_review


def test_failed_charge_marks_failed_but_late_success_still_credits(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    service.settle_charge(charge_data(reference, 5000, status='failed'))
    assert Transaction.objects.get(reference=reference).status == 'failed'
    service.settle_charge(charge_data(reference, 5000, flw_id=9002))
    txn = Transaction.objects.get(reference=reference)
    assert txn.status == 'success' and txn.failed_reason == ''
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)


def test_unknown_reference_is_ignored(wallet, service):
    assert service.settle_charge(charge_data('not-ours-1234', 5000)) is None
    assert service.settle_charge({}) is None


def test_card_is_saved_encrypted_and_deduplicated(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    service.settle_charge(charge_data(reference, 5000))
    card = Card.objects.get(user=wallet.user)
    assert card.token == 'flw-t1nf-new-token' and card.is_default and card.last_4digits == '2950'
    raw = Card.objects.filter(pk=card.pk).values_list('token', flat=True)
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT token FROM {Card._meta.db_table} WHERE id = %s", [card.pk.hex])
        stored = cursor.fetchone()[0]
    assert stored.startswith('enc1:') and 'flw-t1nf' not in stored
    assert list(raw) == ['flw-t1nf-new-token']
    second = start_checkout(service, wallet, flw)['reference']
    service.settle_charge(charge_data(second, 5000, flw_id=9010))
    assert Card.objects.filter(user=wallet.user).count() == 1


def test_cards_not_saved_when_disabled(wallet, service, flw, settings):
    settings.FLW_WALLET_SAVE_CARDS = False
    reference = start_checkout(service, wallet, flw)['reference']
    service.settle_charge(charge_data(reference, 5000))
    assert not Card.objects.exists()


# ----------------------------------------------------------------------
# Fees
# ----------------------------------------------------------------------

def test_customer_borne_deposit_fee_is_charged_on_top(wallet, service, flw, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'deposit': {'percentage': '1.5', 'flat': '0', 'cap': '2000',
                                                 'bearer': 'customer'}}
    result = start_checkout(service, wallet, flw, amount='10000')
    assert result['charge_amount'] == '10150.00'
    assert flw.last_body('payments')['amount'] == 10150
    txn = service.settle_charge(charge_data(result['reference'], 10150))
    wallet.refresh_from_db()
    assert wallet.balance == money(10000)
    assert txn.fees == money(150)
    fee = PlatformFee.objects.get(transaction=txn)
    assert fee.amount == money(150) and fee.operation == 'deposit'


def test_merchant_borne_fee_is_deducted(wallet, service, flw, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'deposit': {'percentage': '1', 'bearer': 'merchant'}}
    result = start_checkout(service, wallet, flw, amount='10000')
    assert result['charge_amount'] == '10000.00'
    service.settle_charge(charge_data(result['reference'], 10000))
    wallet.refresh_from_db()
    assert wallet.balance == money(9900)


def test_processing_fee_bearer_customer(wallet, service, flw, settings):
    settings.FLW_WALLET_PROCESSING_FEE_BEARER = 'customer'
    first = start_checkout(service, wallet, flw, amount='10000')['reference']
    # Merchant-borne on Flutterwave: app_fee is taken from the amount
    service.settle_charge(charge_data(first, 10000, charged_amount=10000, app_fee=140))
    wallet.refresh_from_db()
    assert wallet.balance == money(9860)
    # Customer paid Flutterwave's fee on top at checkout: nothing more to deduct
    second = start_checkout(service, wallet, flw, amount='10000')['reference']
    service.settle_charge(charge_data(second, 10000, flw_id=9002, charged_amount=10140, app_fee=140))
    wallet.refresh_from_db()
    assert wallet.balance == money(19860)


# ----------------------------------------------------------------------
# Virtual accounts
# ----------------------------------------------------------------------

def create_static_account(wallet, flw):
    flw.add('POST', 'virtual-account-numbers', {
        'response_code': '02', 'flw_ref': 'FLW-VA-1', 'order_ref': 'URF_1', 'account_number': '7824822527',
        'bank_name': 'WEMA BANK', 'note': 'Please make a bank transfer to Ada Obi', 'amount': None,
    })
    return VirtualAccountService().create_static_account(wallet, bvn='12345678901')


def test_static_virtual_account_creation_never_stores_bvn(wallet, flw):
    account = create_static_account(wallet, flw)
    assert account.account_number == '7824822527' and account.kind == 'static'
    body = flw.last_body('virtual-account-numbers')
    assert body['is_permanent'] is True and body['bvn'] == '12345678901' and body['tx_ref'] == account.tx_ref
    assert '12345678901' not in str(VirtualAccount.objects.filter(pk=account.pk).values().first())
    assert VirtualAccountService().create_static_account(wallet, bvn='12345678901').pk == account.pk


def test_static_account_needs_bvn_or_nin(wallet):
    from flutterwave_wallet.exceptions import VirtualAccountError
    with pytest.raises(VirtualAccountError):
        VirtualAccountService().create_static_account(wallet)
    with pytest.raises(VirtualAccountError):
        VirtualAccountService().create_static_account(wallet, bvn='123')


def test_transfers_into_static_account_credit_each_time_once(wallet, service, flw):
    account = create_static_account(wallet, flw)
    data = charge_data(account.tx_ref, 2500, flw_id=7001, payment_type='bank_transfer')
    service.settle_charge(data)
    service.settle_charge(data)
    service.settle_charge(charge_data(account.tx_ref, 1000, flw_id=7002, payment_type='bank_transfer'))
    wallet.refresh_from_db()
    assert wallet.balance == money(3500)
    assert set(Transaction.objects.filter(wallet=wallet).values_list('reference', flat=True)) == {'VA-7001', 'VA-7002'}
    assert Transaction.objects.get(reference='VA-7001').payment_method == 'virtual_account'


def test_dynamic_bank_transfer_deposit(wallet, service, flw):
    flw.add('POST', 'virtual-account-numbers', {'account_number': '0067100155', 'bank_name': 'Mock Bank',
                                                'flw_ref': 'MockFLWRef', 'order_ref': 'URF_2'})
    result = service.create_bank_transfer_deposit(wallet, '3000')
    body = flw.last_body('virtual-account-numbers')
    assert body['is_permanent'] is False and body['amount'] == 3000 and body['tx_ref'] == result['reference']
    assert result['account_number'] == '0067100155'
    service.settle_charge(charge_data(result['reference'], 3000, payment_type='bank_transfer'))
    wallet.refresh_from_db()
    assert wallet.balance == money(3000)


def test_virtual_accounts_only_for_ngn_ghs(user, service):
    from flutterwave_wallet.exceptions import VirtualAccountError
    with pytest.raises(VirtualAccountError):
        VirtualAccountService().create_static_account(service.get_wallet(user, 'USD'), bvn='12345678901')


# ----------------------------------------------------------------------
# Direct charges
# ----------------------------------------------------------------------

def test_direct_charge_requiring_action_then_otp(wallet, service, flw):
    flw.add('POST', 'charges', body={
        'status': 'success', 'message': 'Charge initiated',
        'data': {'id': 4001, 'flw_ref': 'FLW-USSD-1', 'status': 'pending', 'processor_response': 'Dial *737#'},
        'meta': {'authorization': {'mode': 'otp', 'note': 'Enter the OTP sent to your phone'}},
    })
    result = service.direct_charge(wallet, '2000', 'account', {'account_bank': '044',
                                                               'account_number': '0690000031',
                                                               'amount': '1', 'currency': 'USD'})
    assert result['status'] == 'pending' and result['action']['mode'] == 'otp'
    assert flw.calls('charges')[0].request.url.endswith('type=account')
    sent = flw.last_body('charges')
    assert sent['amount'] == 2000 and sent['currency'] == 'NGN'  # payload can't override these
    txn = result['transaction']
    assert txn.flw_ref == 'FLW-USSD-1' and txn.requires_action

    flw.add('POST', 'validate-charge', {'id': 4001, 'flw_ref': 'FLW-USSD-1', 'status': 'successful'})
    flw.add('GET', 'transactions/4001/verify', charge_data(txn.reference, 2000, flw_id=4001, payment_type='account'))
    result = service.validate_charge(txn, '12345')
    assert result['status'] == 'success'
    wallet.refresh_from_db()
    assert wallet.balance == money(2000)


@pytest.mark.parametrize('charge_type', ['card', 'applepay', 'googlepay', 'CARD'])
def test_card_direct_charges_are_refused(wallet, service, flw, charge_type):
    """Cards are hosted-checkout only: card data must never reach this server."""
    from flutterwave_wallet.exceptions import CardDataNotAllowed

    with pytest.raises(CardDataNotAllowed):
        service.direct_charge(wallet, '1000', charge_type, {'card_number': '5531886652142950', 'cvv': '564'})
    assert not flw.calls('charges')
    assert not Transaction.objects.filter(wallet=wallet).exists()


def test_direct_charge_definitive_failure(wallet, service, flw):
    flw.error('POST', 'charges', 'Invalid phone number')
    with pytest.raises(FlutterwaveAPIError):
        service.direct_charge(wallet, '1000', 'mpesa', {'phone_number': 'x'})
    assert Transaction.objects.get(wallet=wallet).status == 'failed'


def test_direct_charge_unknown_outcome_is_flagged(wallet, service, flw):
    flw.add('POST', 'charges', body={'status': 'error', 'message': 'Gateway timeout'}, status=504)
    result = service.direct_charge(wallet, '1000', 'mpesa', {'phone_number': '254700000000'})
    assert result['status'] == 'pending'
    assert result['transaction'].needs_review


def test_unknown_charge_type_rejected(wallet, service):
    with pytest.raises(InvalidAmount):
        service.direct_charge(wallet, '1000', 'bitcoin')


def test_continuing_a_finished_charge_is_refused(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    service.settle_charge(charge_data(reference, 5000))
    with pytest.raises(InvalidTransactionState):
        service.direct_charge(wallet, None, 'mpesa', {}, reference=reference)


# ----------------------------------------------------------------------
# Saved cards
# ----------------------------------------------------------------------

def test_saved_card_charge(wallet, card, service, flw):
    flw.add('POST', 'tokenized-charges', {'id': 5001, 'tx_ref': 'x', 'status': 'successful'})
    def verify(request):
        import json as _json
        txn = Transaction.objects.filter(card=card).first()
        return 200, {}, _json.dumps({'status': 'success', 'data': charge_data(txn.reference, 7000, flw_id=5001)})
    flw.rsps.add_callback('GET', 'https://api.flutterwave.com/v3/transactions/5001/verify', callback=verify)
    txn = service.charge_saved_card(card, '7000')
    body = flw.last_body('tokenized-charges')
    assert body['token'] == 'flw-t1nf-secret-token' and body['email'] == 'ada@example.com'
    assert txn.status == 'success'
    wallet.refresh_from_db()
    assert wallet.balance == money(7000)


def test_saved_card_unknown_outcome_stays_pending(wallet, card, service, flw):
    flw.add('POST', 'tokenized-charges', body={'status': 'error', 'message': 'upstream'}, status=502)
    txn = service.charge_saved_card(card, '7000')
    assert txn.status == 'pending' and txn.needs_review


def test_expired_or_removed_card_refused(wallet, card, service):
    from flutterwave_wallet.exceptions import CardError
    card.expiry = '01/20'
    card.save()
    with pytest.raises(CardError):
        service.charge_saved_card(card, '100')
    service.remove_card(card)
    card.refresh_from_db()
    assert card.token == '' and not card.is_active


def test_cannot_charge_another_users_card_into_wallet(other_wallet, card, service):
    with pytest.raises(InvalidTransactionState):
        service.charge_saved_card(card, '100', wallet=other_wallet)


# ----------------------------------------------------------------------
# Verification, cancellation, reconciliation
# ----------------------------------------------------------------------

def test_verify_deposit_by_reference(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    flw.add('GET', 'transactions/verify_by_reference', charge_data(reference, 5000))
    txn = service.verify_deposit(reference)
    assert txn.status == 'success'
    assert service.verify_deposit(reference).pk == txn.pk  # no second API call needed


def test_abandoned_deposit_cancelled_after_window(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    txn = Transaction.objects.get(reference=reference)
    Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(hours=30))
    txn.refresh_from_db()
    flw.error('GET', 'transactions/verify_by_reference', 'No transaction was found for this id')
    DepositService().reconcile_deposit(txn)
    txn.refresh_from_db()
    assert txn.status == 'cancelled'


def test_recent_unpaid_deposit_not_cancelled(wallet, service, flw):
    reference = start_checkout(service, wallet, flw)['reference']
    flw.error('GET', 'transactions/verify_by_reference', 'No transaction was found for this id')
    with pytest.raises(FlutterwaveAPIError):
        DepositService().reconcile_deposit(Transaction.objects.get(reference=reference))
    assert Transaction.objects.get(reference=reference).status == 'pending'


def test_cancel_only_pending_deposits(funded_wallet, service):
    with pytest.raises(InvalidTransactionState):
        service.cancel_deposit(Transaction.objects.get(wallet=funded_wallet))


def test_deposits_feature_switch(wallet, service, settings):
    from flutterwave_wallet.exceptions import FeatureDisabled
    settings.FLW_WALLET_ENABLE_DEPOSITS = False
    with pytest.raises(FeatureDisabled):
        service.initialize_checkout(wallet, '500')


def test_fee_consuming_whole_deposit_is_flagged(wallet, service, flw, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'deposit': {'flat': '50', 'bearer': 'merchant'}}
    account = create_static_account(wallet, flw)
    txn = service.settle_charge(charge_data(account.tx_ref, 40, flw_id=7100, payment_type='bank_transfer'))
    wallet.refresh_from_db()
    assert wallet.balance == money(40)
    assert txn.status == 'success' and txn.needs_review and 'smaller than its fee' in txn.metadata['review_reason']
    assert Decimal(txn.metadata['paid_amount']) == Decimal('40')
