"""
Race-condition tests. They need real row locks (``SELECT ... FOR UPDATE``), so they are
skipped on SQLite. Run them with ``pytest --ds=tests.settings_postgres``.
"""
import threading
from decimal import Decimal

import pytest
from django.db import connection, connections

from flutterwave_wallet.exceptions import InsufficientFunds, InvalidPin, PinLocked, TransactionLimitExceeded
from flutterwave_wallet.models import Transaction, Wallet
from flutterwave_wallet.services.ledger import verify_wallet_ledger
from flutterwave_wallet.services.wallet_service import WalletService
from tests.conftest import charge_data, fund, money

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor == 'sqlite', reason='SQLite has no row-level locking'),
]

THREADS = 10


def race(target, count=THREADS):
    """Run ``target(i)`` in ``count`` threads released at the same instant; return (results, errors)."""
    barrier = threading.Barrier(count)
    results, errors = [], []

    def run(i):
        try:
            barrier.wait()
            results.append(target(i))
        except Exception as exc:  # collected and asserted on by the caller
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, errors


def test_concurrent_transfers_cannot_overspend(user, other_user):
    service = WalletService()
    sender = fund(service.get_wallet(user), 1000)
    receiver = service.get_wallet(other_user)

    successes, errors = race(lambda i: WalletService().transfer(Wallet.objects.get(pk=sender.pk), receiver, '300'))

    assert len(successes) == 3
    assert errors and all(isinstance(e, InsufficientFunds) for e in errors)
    sender.refresh_from_db()
    receiver.refresh_from_db()
    assert sender.balance == money(100) and receiver.balance == money(900)
    assert verify_wallet_ledger(sender) == [] and verify_wallet_ledger(receiver) == []


def test_concurrent_debits_respect_daily_limit(user, other_user, settings):
    settings.FLW_WALLET_MAXIMUM_DAILY_TRANSACTION = 1000
    service = WalletService()
    sender = fund(service.get_wallet(user), 10000)
    receiver = service.get_wallet(other_user)

    successes, errors = race(lambda i: WalletService().transfer(Wallet.objects.get(pk=sender.pk), receiver, '400'))

    assert len(successes) == 2
    assert all(isinstance(e, TransactionLimitExceeded) for e in errors)


def test_duplicate_settlements_in_parallel_credit_once(user, flw):
    service = WalletService()
    wallet = service.get_wallet(user)
    flw.add('POST', 'payments', {'link': 'x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    data = charge_data(reference, 5000)

    race(lambda i: WalletService().settle_charge(dict(data)))

    wallet.refresh_from_db()
    assert wallet.balance == money(5000)
    assert Transaction.objects.filter(wallet=wallet, ledger_sequence__isnull=False).count() == 1


def test_parallel_virtual_account_webhooks_credit_once(user, flw):
    from flutterwave_wallet.models import VirtualAccount

    service = WalletService()
    wallet = service.get_wallet(user)
    VirtualAccount.objects.create(wallet=wallet, account_number='7824822527', tx_ref='VA-static-tx-ref-1')
    data = charge_data('VA-static-tx-ref-1', 2500, flw_id=7001, payment_type='bank_transfer')

    successes, errors = race(lambda i: WalletService().settle_charge(dict(data)))

    assert not errors
    wallet.refresh_from_db()
    assert wallet.balance == money(2500)


def test_ledger_balances_under_mixed_load(user, other_user):
    service = WalletService()
    a = fund(service.get_wallet(user), 5000)
    b = fund(service.get_wallet(other_user), 5000)

    def worker(i):
        source, target = (a, b) if i % 2 else (b, a)
        return WalletService().transfer(Wallet.objects.get(pk=source.pk), Wallet.objects.get(pk=target.pk), '700')

    race(worker, count=16)
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.balance.amount + b.balance.amount == Decimal('10000')
    assert verify_wallet_ledger(a) == [] and verify_wallet_ledger(b) == []


def test_parallel_withdrawal_failure_refunds_once(user, bank_account, flw):
    import json
    service = WalletService()
    wallet = fund(service.get_wallet(user), 5000)

    def respond(request):
        ref = json.loads(request.body)['reference']
        return 200, {}, json.dumps({'status': 'success', 'data': {'id': 8001, 'reference': ref, 'status': 'NEW'}})
    flw.rsps.add_callback('POST', 'https://api.flutterwave.com/v3/transfers', callback=respond)
    txn = service.withdraw(wallet, '1000', bank_account)
    failed = {'id': 8001, 'reference': txn.reference, 'status': 'FAILED'}

    race(lambda i: WalletService().apply_transfer_state(Transaction.objects.get(pk=txn.pk), dict(failed)))

    wallet.refresh_from_db()
    assert wallet.balance == money(5000)
    assert Transaction.objects.filter(reference=f"{txn.reference}-REV").count() == 1


def test_parallel_pin_guessing_hits_lockout(user, settings):
    settings.FLW_WALLET_PIN_MAX_ATTEMPTS = 5
    WalletService().set_pin(user, '1357')

    results, errors = race(lambda i: WalletService().verify_pin(user, f'{i:04d}' if i != 7 else '0000'), count=12)

    assert not results  # no guess succeeded
    assert sum(isinstance(e, PinLocked) for e in errors) >= 1
    assert all(isinstance(e, (InvalidPin, PinLocked)) for e in errors)
    with pytest.raises(PinLocked):
        WalletService().verify_pin(user, '1357')


def test_parallel_v4_settlements_credit_once(user, settings, flw4):
    from flutterwave_wallet.gateways import V4Gateway
    from tests.conftest import v4_charge
    settings.FLW_WALLET_API_VERSION = 'v4'
    service = WalletService()
    wallet = service.get_wallet(user)
    flw4.add('POST', 'orchestration/direct-charges', {'id': 'chg_race01', 'status': 'pending', 'reference': 'x'})
    reference = service.direct_charge(wallet, '2000', 'mobile_money', {'network': 'MTN'})['reference']
    data = V4Gateway(None).normalize_charge(v4_charge(reference, 2000, 'chg_race01'))

    successes, errors = race(lambda i: WalletService().settle_charge(dict(data)))

    assert not errors
    wallet.refresh_from_db()
    assert wallet.balance == money(2000)
    assert verify_wallet_ledger(wallet) == []
