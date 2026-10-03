from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from djmoney.money import Money

from flutterwave_wallet.exceptions import (
    CurrencyMismatchError,
    InsufficientFunds,
    InvalidAmount,
    MinimumBalanceViolation,
    UnsupportedCurrency,
    WalletLocked,
)
from flutterwave_wallet.models import Transaction, Wallet
from flutterwave_wallet.services import ledger
from tests.conftest import fund, money

pytestmark = pytest.mark.django_db


def test_wallet_created_automatically_with_profile(django_user_model, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        user = django_user_model.objects.create_user(username='ada', email='ada@example.com')
    wallet = Wallet.objects.get(user=user)
    assert wallet.currency == 'NGN'
    assert wallet.balance == money(0)
    assert user.wallet_profile.tag == 'ada'


def test_one_wallet_per_currency(user, service):
    ngn = service.get_wallet(user)
    usd = service.get_wallet(user, 'usd')
    assert usd.currency == 'USD' and usd.pk != ngn.pk
    assert service.get_wallet(user, 'USD').pk == usd.pk
    assert service.get_wallets(user).count() == 2


def test_disallowed_currency_rejected(user, service):
    with pytest.raises(UnsupportedCurrency):
        service.get_wallet(user, 'GBP')
    with pytest.raises(UnsupportedCurrency):
        service.get_wallet(user, 'XYZ')


def test_post_credit_and_debit_records_sequence_and_hash(wallet, service):
    first = service.credit_wallet(wallet, '1000')
    second = service.debit_wallet(wallet, '250.50')
    wallet.refresh_from_db()
    assert wallet.balance == money('749.50')
    assert (first.ledger_sequence, second.ledger_sequence) == (1, 2)
    assert first.balance_after == money(1000) and second.balance_after == money('749.50')
    assert second.previous_hash == first.entry_hash
    assert wallet.ledger_head_hash == second.entry_hash
    assert service.verify_ledger(wallet) == []


def test_verify_detects_tampering(funded_wallet, service):
    entry = Transaction.objects.get(wallet=funded_wallet)
    Transaction.objects.filter(pk=entry.pk).update(total_amount=Decimal('999999'))
    problems = service.verify_ledger(funded_wallet)
    assert any('hash mismatch' in p for p in problems)
    assert any('running total' in p for p in problems)


def test_verify_detects_deleted_entry(wallet, service):
    fund(wallet, 100)
    fund(wallet, 200)
    Transaction.objects.filter(wallet=wallet, ledger_sequence=1).delete()
    problems = service.verify_ledger(wallet)
    assert any('gap' in p for p in problems)


def test_debit_cannot_overdraw(funded_wallet, service):
    with pytest.raises(InsufficientFunds):
        service.debit_wallet(funded_wallet, '100000.01')
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000)


def test_minimum_balance_is_enforced(funded_wallet, service, settings):
    settings.FLW_WALLET_MINIMUM_BALANCE = 500
    with transaction.atomic():
        locked = ledger.lock_wallet(funded_wallet)
        with pytest.raises(MinimumBalanceViolation):
            locked.apply_debit(Money('99600', 'NGN'))


def test_database_refuses_negative_balance(wallet):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).update(balance=Decimal('-1'))


@pytest.mark.django_db(transaction=True)
def test_balance_changes_require_a_transaction(wallet):
    with pytest.raises(RuntimeError):
        wallet.apply_credit('10')
    with pytest.raises(RuntimeError):
        ledger.lock_wallet(wallet)


def test_post_rejects_already_posted_and_foreign_entries(funded_wallet, other_wallet):
    entry = Transaction.objects.get(wallet=funded_wallet)
    with transaction.atomic():
        locked = ledger.lock_wallet(funded_wallet)
        with pytest.raises(ValueError):
            ledger.post(locked, entry)
        with pytest.raises(ValueError):
            ledger.post(ledger.lock_wallet(other_wallet), Transaction(wallet=funded_wallet, amount=money(1),
                                                                      total_amount=money(1)))


def test_zero_amount_entries_are_not_posted(wallet):
    with transaction.atomic():
        locked = ledger.lock_wallet(wallet)
        txn = ledger.post(locked, Transaction(wallet=locked, amount=money(5), total_amount=money(0),
                                              reference='ZERO-ENTRY-1'))
    assert txn.ledger_sequence is None
    wallet.refresh_from_db()
    assert wallet.ledger_sequence == 0


def test_validate_amount(wallet):
    assert wallet.validate_amount('10.005') == money('10.01')
    for bad in ('0', '-5', 'abc', None, True):
        with pytest.raises(InvalidAmount):
            wallet.validate_amount(bad)
    with pytest.raises(InvalidAmount):
        wallet.validate_amount(10.5)  # floats are refused for money
    with pytest.raises(CurrencyMismatchError):
        wallet.validate_amount(Money(5, 'USD'))


def test_locked_wallet_rejects_debit_but_force_credit_works(funded_wallet, service):
    service.lock_wallet_account(funded_wallet, 'fraud check')
    with pytest.raises(WalletLocked):
        service.debit_wallet(funded_wallet, '10')
    with transaction.atomic():
        locked = ledger.lock_wallet(funded_wallet)
        ledger.post(locked, Transaction(wallet=locked, amount=money(5), total_amount=money(5),
                                        reference='FORCED-CREDIT-1', direction='credit'), force=True)
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100005)


def test_hash_chain_can_be_disabled(wallet, service, settings):
    settings.FLW_WALLET_LEDGER_HASH_CHAIN = False
    txn = service.credit_wallet(wallet, '10')
    assert txn.entry_hash == ''
    assert service.verify_ledger(wallet) == []


def test_statement_is_in_ledger_order(wallet, service):
    for amount in ('10', '20', '30'):
        service.credit_wallet(wallet, amount)
    assert [t.ledger_sequence for t in service.get_statement(wallet)] == [1, 2, 3]
