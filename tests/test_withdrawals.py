from datetime import timedelta

import pytest
from django.utils import timezone

from flutterwave_wallet.exceptions import (
    BankAccountError,
    FlutterwaveAPIError,
    InsufficientFunds,
    InvalidTransactionState,
    TransactionLimitExceeded,
)
from flutterwave_wallet.models import BankAccount, PlatformFee, Transaction
from tests.conftest import money

pytestmark = pytest.mark.django_db


def transfer_data(txn, status='NEW', flw_id=8001, **extra):
    return {'id': flw_id, 'reference': txn.reference if hasattr(txn, 'reference') else txn, 'status': status,
            'amount': 1000, 'currency': 'NGN', 'complete_message': extra.pop('message', ''), **extra}


def start_withdrawal(service, wallet, account, flw, amount='1000', status='NEW'):
    def respond(request):
        import json
        reference = json.loads(request.body)['reference']
        return 200, {}, json.dumps({'status': 'success', 'message': 'Transfer Queued Successfully',
                                    'data': {'id': 8001, 'reference': reference, 'status': status}})
    flw.rsps.add_callback('POST', 'https://api.flutterwave.com/v3/transfers', callback=respond)
    return service.withdraw(wallet, amount, account)


def test_withdrawal_debits_first_and_sends_transfer(funded_wallet, bank_account, service, flw):
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    body = flw.last_body('transfers')
    assert body['account_bank'] == '044' and body['account_number'] == '0690000031'
    assert body['amount'] == 1000 and body['reference'] == txn.reference and body['currency'] == 'NGN'
    assert txn.status == 'processing' and txn.flw_id == '8001' and txn.is_posted
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99000)


def test_webhook_success_completes(funded_wallet, bank_account, service, flw):
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    flw.add('GET', 'transfers/8001', transfer_data(txn, 'SUCCESSFUL'))
    service.handle_transfer_webhook({'id': 8001, 'reference': txn.reference, 'status': 'SUCCESSFUL'})
    txn.refresh_from_db()
    assert txn.status == 'success'
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99000)


def test_webhook_body_is_not_trusted(funded_wallet, bank_account, service, flw):
    """A forged 'FAILED' webhook can't refund a withdrawal that Flutterwave says is still pending."""
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    flw.add('GET', 'transfers/8001', transfer_data(txn, 'PENDING'))
    service.handle_transfer_webhook({'id': 8001, 'reference': txn.reference, 'status': 'FAILED'})
    txn.refresh_from_db()
    assert txn.is_pending
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99000)


def test_verified_failure_refunds_exactly_once(funded_wallet, bank_account, service, flw):
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    flw.add('GET', 'transfers/8001', transfer_data(txn, 'FAILED', message='DISBURSE FAILED: Insufficient funds'))
    for _ in range(3):
        service.handle_transfer_webhook({'id': 8001, 'reference': txn.reference})
    txn.refresh_from_db()
    assert txn.status == 'failed' and 'DISBURSE FAILED' in txn.failed_reason
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)
    assert Transaction.objects.filter(reference=f"{txn.reference}-REV").count() == 1
    assert service.verify_ledger(funded_wallet) == []


def test_definitive_rejection_reverses_immediately(funded_wallet, bank_account, service, flw):
    flw.error('POST', 'transfers', 'Account resolve failed')
    with pytest.raises(FlutterwaveAPIError):
        service.withdraw(funded_wallet, '1000', bank_account)
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)
    assert Transaction.objects.get(transaction_type='withdrawal').status == 'failed'


def test_unknown_outcome_keeps_money_out_and_flags(funded_wallet, bank_account, service, flw):
    flw.add('POST', 'transfers', body={'status': 'error', 'message': 'timeout'}, status=504)
    txn = service.withdraw(funded_wallet, '1000', bank_account)
    assert txn.is_pending and txn.needs_review
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99000)


def test_reconcile_finds_transfer_by_reference(funded_wallet, bank_account, service, flw):
    flw.add('POST', 'transfers', body={'status': 'error', 'message': 'timeout'}, status=504)
    txn = service.withdraw(funded_wallet, '1000', bank_account)
    flw.add('GET', 'transfers', [transfer_data(txn, 'SUCCESSFUL', flw_id=8100)])
    service.verify_withdrawal(txn)
    txn.refresh_from_db()
    assert txn.status == 'success' and txn.flw_id == '8100'


def test_missing_transfer_is_flagged_not_refunded(funded_wallet, bank_account, service, flw):
    flw.add('POST', 'transfers', body={'status': 'error', 'message': 'timeout'}, status=504)
    txn = service.withdraw(funded_wallet, '1000', bank_account)
    Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(hours=2), needs_review=False)
    txn.refresh_from_db()
    flw.add('GET', 'transfers', [])
    service.verify_withdrawal(txn)
    txn.refresh_from_db()
    assert txn.is_pending and txn.needs_review
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(99000)
    # A human confirms it never left, then force-reverses
    service.force_reverse_withdrawal(txn, 'Confirmed on dashboard', performed_by=None)
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_force_reverse_requires_review_flag(funded_wallet, bank_account, service, flw):
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    with pytest.raises(InvalidTransactionState):
        service.force_reverse_withdrawal(txn, 'nope')


def test_success_after_refund_is_flagged(funded_wallet, bank_account, service, flw):
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    service.apply_transfer_state(txn, transfer_data(txn, 'FAILED'))
    service.apply_transfer_state(txn, transfer_data(txn, 'SUCCESSFUL'))
    txn.refresh_from_db()
    assert txn.status == 'failed' and txn.needs_review


def test_mismatched_reference_is_ignored(funded_wallet, bank_account, service, flw):
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    service.apply_transfer_state(txn, transfer_data('someone-else', 'FAILED'))
    txn.refresh_from_db()
    assert txn.is_pending


def test_withdrawal_guards(funded_wallet, bank_account, other_user, service, flw):
    with pytest.raises(InsufficientFunds):
        start_withdrawal(service, funded_wallet, bank_account, flw, amount='200000')
    foreign = BankAccount.objects.create(user=other_user, account_bank='044', account_number='1', currency='NGN',
                                         account_name='X')
    with pytest.raises(BankAccountError):
        service.withdraw(funded_wallet, '1000', foreign)
    usd_account = BankAccount.objects.create(user=funded_wallet.user, account_bank='044', account_number='2',
                                             currency='USD', account_name='X')
    with pytest.raises(BankAccountError):
        service.withdraw(funded_wallet, '1000', usd_account)
    bank_account.is_active = False
    bank_account.save()
    with pytest.raises(BankAccountError):
        service.withdraw(funded_wallet, '1000', bank_account)


def test_daily_limit(funded_wallet, bank_account, service, flw, settings):
    settings.FLW_WALLET_MAXIMUM_DAILY_TRANSACTION = 1500
    start_withdrawal(service, funded_wallet, bank_account, flw)
    with pytest.raises(TransactionLimitExceeded):
        service.withdraw(funded_wallet, '1000', bank_account)


def test_withdrawal_fee_and_reversal_voids_it(funded_wallet, bank_account, service, flw, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'withdrawal': {'flat': '50', 'bearer': 'customer'}}
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    assert txn.total_amount == money(1050)
    assert flw.last_body('transfers')['amount'] == 1000
    fee = PlatformFee.objects.get(transaction=txn)
    service.apply_transfer_state(txn, transfer_data(txn, 'FAILED'))
    fee.refresh_from_db()
    assert fee.voided
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_flutterwave_transfer_fee_source(funded_wallet, bank_account, service, flw, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'withdrawal': {'source': 'flutterwave', 'bearer': 'customer'}}
    flw.add('GET', 'transfers/fee', [{'currency': 'NGN', 'fee_type': 'value', 'fee': 26.875}])
    txn = start_withdrawal(service, funded_wallet, bank_account, flw)
    assert txn.fees == money('26.88') and txn.total_amount == money('1026.88')


def test_mobile_money_withdrawal(user, service, flw):
    from tests.conftest import fund
    wallet = fund(service.get_wallet(user, 'KES'), 5000)
    account = service.add_mobile_money_account(user, 'MPS', '254700000000', 'KES', 'Ada Obi')
    txn = start_withdrawal(service, wallet, account, flw, amount='500')
    assert txn.payment_method == 'mobile_money'
    body = flw.last_body('transfers')
    assert body['account_bank'] == 'MPS' and body['currency'] == 'KES'


def test_add_bank_account_uses_resolved_name(user, bank, service, flw):
    flw.add('POST', 'accounts/resolve', {'account_number': '0690000031', 'account_name': 'ADA OBI'})
    account = service.add_bank_account(user, '044', '0690000031', account_name='Hacker Name')
    assert account.account_name == 'ADA OBI' and account.is_verified and account.is_default
    assert account.bank == bank


def test_add_bank_account_resolution_failure(user, service, flw):
    flw.error('POST', 'accounts/resolve', 'Sorry, recipient account could not be validated')
    with pytest.raises(BankAccountError):
        service.add_bank_account(user, '044', '0000000000')


def test_sync_banks(service, flw):
    flw.add('GET', 'banks/NG', [{'id': 1, 'code': '044', 'name': 'Access Bank'},
                                {'id': 2, 'code': '058', 'name': 'GTBank'}])
    assert service.sync_banks(['NG']) == (2, 0)
    assert service.sync_banks(['NG']) == (0, 2)


@pytest.mark.django_db(transaction=True)
def test_money_out_refuses_to_run_inside_outer_transaction(user, bank, flw):
    from django.db import transaction as db_transaction

    from flutterwave_wallet.services.wallet_service import WalletService
    from tests.conftest import fund
    service = WalletService()
    wallet = fund(service.get_wallet(user), 5000)
    account = BankAccount.objects.create(user=user, bank=bank, account_bank='044', account_number='0690000031',
                                         account_name='ADA OBI', currency='NGN')
    with pytest.raises(RuntimeError, match='must not run inside'):
        with db_transaction.atomic():
            service.withdraw(wallet, '1000', account)
    assert not flw.calls('transfers')
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)
