"""Reconciliation, background tasks, notifications and the admin."""
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from flutterwave_wallet import tasks
from flutterwave_wallet.models import Card, Transaction, WebhookEvent
from flutterwave_wallet.services.reconciliation_service import ReconciliationService
from tests.conftest import charge_data, fund, money
from tests.helpers import RecordingBackend

pytestmark = pytest.mark.django_db


def age(txn, minutes=30):
    Transaction.objects.filter(pk=txn.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes),
                                                 created_at=timezone.now() - timedelta(minutes=minutes))


def test_reconcile_settles_stale_deposit_and_withdrawal(wallet, service, bank_account, flw):
    flw.add('POST', 'payments', {'link': 'x'})
    reference = service.initialize_checkout(wallet, '5000')['reference']
    age(Transaction.objects.get(reference=reference))
    flw.add('GET', 'transactions/verify_by_reference', charge_data(reference, 5000))

    fund(wallet, 2000)
    flw.add('POST', 'transfers', body={'status': 'error', 'message': 't'}, status=504)
    withdrawal = service.withdraw(wallet, '1000', bank_account)
    age(withdrawal)
    flw.add('GET', 'transfers', [{'id': 1, 'reference': withdrawal.reference, 'status': 'SUCCESSFUL'}])

    result = ReconciliationService().reconcile()
    assert result['deposits'] == 1 and result['withdrawals'] == 1
    assert Transaction.objects.get(reference=reference).status == 'success'
    assert Transaction.objects.get(pk=withdrawal.pk).status == 'success'
    wallet.refresh_from_db()
    assert wallet.balance == money(6000)


def test_reconcile_skips_fresh_and_survives_errors(wallet, service, flw):
    flw.add('POST', 'payments', {'link': 'x'})
    fresh = service.initialize_checkout(wallet, '5000')['reference']
    stale = service.initialize_checkout(wallet, '6000')['reference']
    age(Transaction.objects.get(reference=stale))
    flw.add('GET', 'transactions/verify_by_reference', body={'status': 'error', 'message': 'x'}, status=500)
    assert ReconciliationService().reconcile_deposits() == 0  # the error is logged, the batch continues
    assert Transaction.objects.get(reference=fresh).status == 'pending'


def test_reconcile_bills_and_refunds(funded_wallet, service, flw):
    flw.add('GET', 'bill-items/AT099/validate', {'name': 'MTN'})
    flw.add('POST', 'billers/BIL099/items/AT099/payment', {'flw_ref': 'B1'})
    bill = service.pay_bill(funded_wallet, 'BIL099', 'AT099', '08031234567', '500')
    age(bill)
    flw.add('GET', f'bills/{bill.reference}', {'status': 'success'})
    assert ReconciliationService().reconcile_bill_payments() == 1
    assert Transaction.objects.get(pk=bill.pk).status == 'success'


def test_verify_ledgers_reports_problems(funded_wallet):
    assert ReconciliationService.verify_ledgers() == {}
    Transaction.objects.filter(wallet=funded_wallet).update(balance_after=1)
    assert str(funded_wallet.pk) in ReconciliationService.verify_ledgers()


def test_tasks_run_inline(funded_wallet, user, flw):
    assert tasks.reconcile_task.apply().get() == {'deposits': 0, 'withdrawals': 0, 'bill_payments': 0, 'refunds': 0}
    assert tasks.verify_ledgers_task.apply().get() == {}
    assert tasks.sweep_platform_fees_task.apply().get() == {}
    assert tasks.prune_task.apply().get()['webhook_events'] == 0
    assert tasks.create_wallet_for_user_task.apply(args=(user.pk, 'USD')).get()
    flw.add('GET', 'banks/NG', [{'id': 1, 'code': '044', 'name': 'Access'}])
    assert tasks.sync_banks_task.apply(args=(['NG'],)).get() == {'created': 1, 'updated': 0}


def test_single_instance_lock_skips_overlap():
    from django.core.cache import cache
    cache.add('flw-wallet-task-lock:reconcile', 1, 60)
    assert tasks.reconcile_task.apply().get() is None


def test_expired_cards_task(card):
    Card.objects.filter(pk=card.pk).update(expiry='01/20')
    assert tasks.remove_expired_cards_task.apply().get() == 1


def test_webhook_processed_by_celery_task(wallet, service, flw, settings, django_capture_on_commit_callbacks):
    settings.FLW_WALLET_USE_CELERY = True
    settings.CELERY_TASK_ALWAYS_EAGER = True
    from celery import current_app
    current_app.conf.task_always_eager = True
    try:
        flw.add('POST', 'payments', {'link': 'x'})
        reference = service.initialize_checkout(wallet, '5000')['reference']
        flw.add('GET', 'transactions/9001/verify', charge_data(reference, 5000))
        import json
        from django.conf import settings as dj
        body = json.dumps({'event': 'charge.completed', 'data': charge_data(reference, 5000)}).encode()
        with django_capture_on_commit_callbacks(execute=True):
            from flutterwave_wallet.services.webhook_service import WebhookService
            WebhookService().handle_webhook(body, {'verif-hash': dj.FLUTTERWAVE_SECRET_HASH})
        assert WebhookEvent.objects.get().processed
        wallet.refresh_from_db()
        assert wallet.balance == money(5000)
    finally:
        current_app.conf.task_always_eager = False


def test_notifications(funded_wallet, other_wallet, service, settings, django_capture_on_commit_callbacks):
    settings.FLW_WALLET_NOTIFICATION_BACKENDS = ['tests.helpers.RecordingBackend', 'tests.helpers.ExplodingBackend',
                                                 'flutterwave_wallet.notifications.backends.EmailNotificationBackend']
    RecordingBackend.sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        service.transfer(funded_wallet, other_wallet, '100')
    events = [event for event, _user, _ctx in RecordingBackend.sent]
    assert events == ['transfer_sent', 'transfer_received']  # the exploding backend didn't stop anything
    assert {m.subject for m in mail.outbox} == {'You sent money', 'You received money'}
    assert 'You received NGN100.00' in [m.body for m in mail.outbox if m.to == ['bola@example.com']][0]


def test_admin_pages_render(client, staff_user, funded_wallet, card, bank_account):
    client.force_login(staff_user)
    for name in ('wallet', 'transaction', 'walletprofile', 'card', 'bankaccount', 'platformfee', 'webhookevent',
                 'virtualaccount', 'bank'):
        assert client.get(f'/admin/flutterwave_wallet/{name}/').status_code == 200, name
    assert client.get(f'/admin/flutterwave_wallet/wallet/{funded_wallet.pk}/change/').status_code == 200
    response = client.post('/admin/flutterwave_wallet/wallet/', {'action': 'verify_ledgers',
                                                                 '_selected_action': [str(funded_wallet.pk)]})
    assert response.status_code == 302


def test_admin_csv_export_neutralises_formulas(client, staff_user, wallet, service):
    service.credit_wallet(wallet, '10', description='=HYPERLINK("http://evil")')
    client.force_login(staff_user)
    txn = Transaction.objects.get(wallet=wallet)
    from flutterwave_wallet.admin import _csv_safe
    assert _csv_safe('=1+1') == "'=1+1" and _csv_safe('ok') == 'ok'
    response = client.post('/admin/flutterwave_wallet/transaction/', {'action': 'export_csv',
                                                                      '_selected_action': [str(txn.pk)]})
    assert response.status_code == 200 and response['Content-Type'] == 'text/csv'
