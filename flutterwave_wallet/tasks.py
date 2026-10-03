"""
Background tasks.

Celery is optional. With ``FLW_WALLET_USE_CELERY = True`` (and ``pip install
'django-flutterwave-wallet[celery]'``) webhooks, provisioning and deliveries run on your
workers. Without Celery every task can still be called directly (``.delay()`` runs
inline) or scheduled with the management commands.

Suggested Celery beat schedule::

    CELERY_BEAT_SCHEDULE = {
        'flw-reconcile':        {'task': 'flutterwave_wallet.tasks.reconcile_task', 'schedule': 300},
        'flw-retry-webhooks':   {'task': 'flutterwave_wallet.tasks.retry_failed_webhooks_task', 'schedule': 300},
        'flw-sweep-fees':       {'task': 'flutterwave_wallet.tasks.sweep_platform_fees_task', 'schedule': 600},
        'flw-retry-deliveries': {'task': 'flutterwave_wallet.tasks.retry_webhook_deliveries_task', 'schedule': 900},
        'flw-expire-cards':     {'task': 'flutterwave_wallet.tasks.remove_expired_cards_task', 'schedule': 86400},
        'flw-verify-ledgers':   {'task': 'flutterwave_wallet.tasks.verify_ledgers_task', 'schedule': 86400},
        'flw-prune':            {'task': 'flutterwave_wallet.tasks.prune_task', 'schedule': 86400},
    }

Periodic tasks take a cache lock so overlapping runs (slow run + next tick, or several
beat instances) never do the same work twice. Use a shared cache (Redis) in production.
"""
import functools
import logging

from django.contrib.auth import get_user_model
from django.core.cache import cache

try:  # pragma: no cover - depends on the environment
    from celery import shared_task
except ImportError:  # pragma: no cover
    class _InlineTask:
        def retry(self, exc=None, **kwargs):
            raise exc

    def shared_task(*task_args, **task_kwargs):
        """Fallback when Celery isn't installed: ``.delay()`` runs the function inline."""

        def decorator(func):
            call = functools.partial(func, _InlineTask()) if task_kwargs.get('bind') else func
            wrapper = functools.wraps(func)(lambda *args, **kwargs: call(*args, **kwargs))
            wrapper.delay = wrapper
            wrapper.apply_async = lambda args=(), kwargs=None, **_: wrapper(*args, **(kwargs or {}))
            return wrapper

        if len(task_args) == 1 and callable(task_args[0]) and not task_kwargs:
            return decorator(task_args[0])
        return decorator

logger = logging.getLogger('flutterwave_wallet')


def single_instance(lock_name, timeout=900):
    """Skip the run if another worker holds the lock (cache ``add`` is atomic in Redis/Memcached)."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = f'flw-wallet-task-lock:{lock_name}'
            if not cache.add(key, 1, timeout):
                logger.info("Task %s already running; skipped", lock_name)
                return None
            try:
                return func(*args, **kwargs)
            finally:
                cache.delete(key)
        return wrapper
    return decorator


@shared_task
def create_wallet_for_user_task(user_id, currency=None):
    from flutterwave_wallet.services.wallet_service import WalletService

    user = get_user_model().objects.get(pk=user_id)
    return str(WalletService().get_wallet(user, currency).pk)


@shared_task(bind=True, max_retries=8, default_retry_delay=30, acks_late=True)
def process_webhook_event_task(self, event_id):
    """Process a stored webhook; retried with backoff if Flutterwave verification failed."""
    from flutterwave_wallet.models import WebhookEvent
    from flutterwave_wallet.services.webhook_service import WebhookService

    ok = WebhookService().reprocess_webhook_event(event_id)
    if not ok:
        event = WebhookEvent.objects.filter(pk=event_id).first()
        if event is not None and not event.processed and event.processing_error:
            countdown = min(30 * (2 ** self.request.retries), 3600) if hasattr(self, 'request') else 30
            raise self.retry(exc=RuntimeError(event.processing_error), countdown=countdown)
    return ok


@shared_task(acks_late=True)
def deliver_webhook_task(event_id, endpoint_id):
    from flutterwave_wallet.models import WebhookEndpoint, WebhookEvent
    from flutterwave_wallet.services.webhook_service import WebhookService

    attempt = WebhookService().deliver(WebhookEvent.objects.get(pk=event_id), WebhookEndpoint.objects.get(pk=endpoint_id))
    return attempt.is_success


@shared_task
@single_instance('retry-webhooks')
def retry_failed_webhooks_task(limit=200):
    from flutterwave_wallet.services.webhook_service import WebhookService
    return WebhookService().retry_failed_events(limit)


@shared_task
@single_instance('retry-deliveries')
def retry_webhook_deliveries_task():
    from flutterwave_wallet.services.webhook_service import WebhookService
    return WebhookService().retry_failed_deliveries()


@shared_task
@single_instance('reconcile', timeout=1800)
def reconcile_task(older_than_minutes=None):
    """Safety net for missed webhooks: settle stale pending deposits, withdrawals, bills and refunds."""
    from flutterwave_wallet.services.reconciliation_service import ReconciliationService
    return ReconciliationService().reconcile(older_than_minutes)


@shared_task
@single_instance('sweep-fees')
def sweep_platform_fees_task():
    from flutterwave_wallet.services.fee_sweep_service import FeeService
    return {currency: str(amount) for currency, amount in FeeService().sweep().items()}


@shared_task
@single_instance('verify-ledgers', timeout=3600 * 6)
def verify_ledgers_task():
    from flutterwave_wallet.services.reconciliation_service import ReconciliationService
    return ReconciliationService.verify_ledgers()


@shared_task
def sync_banks_task(countries=None):
    from flutterwave_wallet.services.bank_account_service import BankAccountService
    created, updated = BankAccountService().sync_banks(countries)
    return {'created': created, 'updated': updated}


@shared_task
def remove_expired_cards_task():
    from flutterwave_wallet.services.card_service import CardService
    return CardService.remove_expired_cards()


@shared_task
@single_instance('prune')
def prune_task(retention_days=None):
    from flutterwave_wallet.services.webhook_service import WebhookService
    return WebhookService.prune(retention_days)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def create_static_virtual_account_task(self, wallet_id, bvn=None, nin=None):
    """Provision a permanent virtual account in the background (the BVN/NIN is not persisted)."""
    from flutterwave_wallet.exceptions import FlutterwaveAPIError
    from flutterwave_wallet.models import Wallet
    from flutterwave_wallet.services.virtual_account_service import VirtualAccountService

    try:
        account = VirtualAccountService().create_static_account(Wallet.objects.get(pk=wallet_id), bvn=bvn, nin=nin)
    except FlutterwaveAPIError as exc:
        if exc.is_definitive:
            logger.warning("Virtual account for wallet %s rejected: %s", wallet_id, exc)
            return None
        raise self.retry(exc=exc)
    return str(account.pk)
