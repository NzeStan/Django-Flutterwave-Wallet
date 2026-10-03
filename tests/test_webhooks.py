import json

import pytest
import responses as responses_lib
from rest_framework.test import APIClient

from flutterwave_wallet.models import Transaction, WebhookDeliveryAttempt, WebhookEndpoint, WebhookEvent
from flutterwave_wallet.services.webhook_service import WebhookService
from flutterwave_wallet.signals import flutterwave_webhook_received
from tests.conftest import charge_data, money, webhook

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def pending_deposit(wallet, service, flw):
    flw.add('POST', 'payments', {'link': 'https://x'})
    return service.initialize_checkout(wallet, '5000')['reference']


def charge_event(reference, flw_id=9001, **kwargs):
    return {'event': 'charge.completed', 'data': charge_data(reference, 5000, flw_id=flw_id, **kwargs)}


@pytest.mark.parametrize('mode', ['hash', 'hmac'])
def test_valid_webhook_verifies_with_api_and_credits_once(client, wallet, pending_deposit, flw, mode):
    flw.add('GET', 'transactions/9001/verify', charge_data(pending_deposit, 5000))
    payload = charge_event(pending_deposit)
    first = webhook(client, payload, mode=mode)
    second = webhook(client, payload, mode=mode)
    assert first.status_code == 200 and first.json()['duplicate'] is False
    assert second.status_code == 200 and second.json()['duplicate'] is True
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)
    event = WebhookEvent.objects.get()
    assert event.processed and event.transaction.reference == pending_deposit
    assert len(flw.calls('transactions/9001/verify')) == 1


def test_bad_signature_rejected(client, pending_deposit):
    response = webhook(client, charge_event(pending_deposit), secret='wrong-secret')
    assert response.status_code == 401
    assert not WebhookEvent.objects.exists()
    response = client.post('/wallet/webhook/', data=json.dumps(charge_event(pending_deposit)),
                           content_type='application/json')
    assert response.status_code == 401


def test_signature_mode_restricts_header(client, pending_deposit, settings):
    settings.FLW_WALLET_WEBHOOK_SIGNATURE_MODE = 'hmac'
    assert webhook(client, charge_event(pending_deposit), mode='hash').status_code == 401


def test_missing_secret_hash_refuses_everything(client, pending_deposit, settings):
    settings.FLUTTERWAVE_SECRET_HASH = ''
    assert webhook(client, charge_event(pending_deposit), secret='').status_code == 401


def test_forged_amount_in_body_is_ignored(client, wallet, pending_deposit, flw):
    """An attacker with the secret hash claims 5,000,000 was paid; the API says 5,000."""
    flw.add('GET', 'transactions/9001/verify', charge_data(pending_deposit, 5000))
    payload = {'event': 'charge.completed', 'data': charge_data(pending_deposit, 5000000)}
    webhook(client, payload)
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)


def test_forged_success_for_failed_payment_is_ignored(client, wallet, pending_deposit, flw):
    flw.add('GET', 'transactions/9001/verify', charge_data(pending_deposit, 5000, status='failed'))
    webhook(client, charge_event(pending_deposit))
    wallet.refresh_from_db()
    assert wallet.balance == money(0)
    assert Transaction.objects.get(reference=pending_deposit).status == 'failed'


def test_ip_allowlist(client, pending_deposit, settings):
    settings.FLW_WALLET_WEBHOOK_VERIFY_IP = True
    settings.FLW_WALLET_WEBHOOK_ALLOWED_IPS = ['10.0.0.1']
    assert webhook(client, charge_event(pending_deposit)).status_code == 401
    assert webhook(client, charge_event(pending_deposit), REMOTE_ADDR='10.0.0.1').status_code == 200


def test_malformed_payloads(client):
    from django.conf import settings
    body = b'not json'
    response = client.post('/wallet/webhook/', data=body, content_type='application/json',
                           HTTP_VERIF_HASH=settings.FLUTTERWAVE_SECRET_HASH)
    assert response.status_code == 400
    response = client.post('/wallet/webhook/', data=b'[1,2]', content_type='application/json',
                           HTTP_VERIF_HASH=settings.FLUTTERWAVE_SECRET_HASH)
    assert response.status_code == 400


def test_processing_error_is_stored_and_retried(client, wallet, pending_deposit, flw):
    flw.add('GET', 'transactions/9001/verify', body={'status': 'error', 'message': 'down'}, status=503)
    response = webhook(client, charge_event(pending_deposit))
    assert response.status_code == 200  # Flutterwave must not retry forever
    event = WebhookEvent.objects.get()
    assert not event.processed and 'FlutterwaveAPIError' in event.processing_error
    flw.rsps.reset()
    flw.add('GET', 'transactions/9001/verify', charge_data(pending_deposit, 5000))
    from django.utils import timezone
    WebhookEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timezone.timedelta(minutes=5))
    assert WebhookService().retry_failed_events() == 1
    wallet.refresh_from_db()
    assert wallet.balance == money(5000)


def test_transfer_and_bill_and_subscription_events(client, funded_wallet, bank_account, service, flw,
                                                   django_capture_on_commit_callbacks):
    received = []
    flutterwave_webhook_received.connect(lambda **kw: received.append(kw['event']), weak=False,
                                         dispatch_uid='test-recv')
    try:
        def respond(request):
            ref = json.loads(request.body)['reference']
            return 200, {}, json.dumps({'status': 'success', 'data': {'id': 8001, 'reference': ref, 'status': 'NEW'}})
        flw.rsps.add_callback('POST', 'https://api.flutterwave.com/v3/transfers', callback=respond)
        txn = service.withdraw(funded_wallet, '1000', bank_account)
        flw.add('GET', 'transfers/8001', {'id': 8001, 'reference': txn.reference, 'status': 'SUCCESSFUL'})
        with django_capture_on_commit_callbacks(execute=True):
            webhook(client, {'event': 'transfer.completed', 'data': {'id': 8001, 'reference': txn.reference,
                                                                     'status': 'SUCCESSFUL'}})
            webhook(client, {'event': 'subscription.cancelled', 'data': {'id': 1}})
            webhook(client, {'event': 'something.new', 'data': {'id': 2}})
        txn.refresh_from_db()
        assert txn.status == 'success'
        assert received == ['transfer.completed', 'subscription.cancelled', 'something.new']
    finally:
        flutterwave_webhook_received.disconnect(dispatch_uid='test-recv')


def test_virtual_card_event_without_wrapper(client):
    response = webhook(client, {'event.type': 'CARD_TRANSACTION', 'CardId': 'abc', 'Amount': 5})
    assert response.status_code == 200
    assert WebhookEvent.objects.get().event_type == 'CARD_TRANSACTION'


def test_forwarding_is_signed(client, wallet, pending_deposit, flw, settings):
    settings.FLW_WALLET_ENABLE_WEBHOOK_FORWARDING = True
    endpoint = WebhookEndpoint.objects.create(name='erp', url='https://erp.example/hook',
                                              secret='forwarding-secret-0123456789')
    flw.add('GET', 'transactions/9001/verify', charge_data(pending_deposit, 5000))
    flw.rsps.add(responses_lib.POST, 'https://erp.example/hook', json={'ok': True})
    webhook(client, charge_event(pending_deposit))
    call = [c for c in flw.rsps.calls if c.request.url == 'https://erp.example/hook'][0]
    timestamp = call.request.headers['X-Wallet-Timestamp']
    expected = WebhookService.sign_payload(call.request.body, endpoint.secret, timestamp)
    assert call.request.headers['X-Wallet-Signature'] == expected
    assert WebhookDeliveryAttempt.objects.get().is_success


def test_prune_keeps_unprocessed(db):
    from django.utils import timezone
    old = timezone.now() - timezone.timedelta(days=200)
    WebhookEvent.objects.create(event_type='a', payload={}, idempotency_key='1', processed=True)
    WebhookEvent.objects.create(event_type='b', payload={}, idempotency_key='2', processed=False)
    WebhookEvent.objects.update(created_at=old)
    assert WebhookService.prune()['webhook_events'] == 1
    assert WebhookEvent.objects.get().event_type == 'b'
