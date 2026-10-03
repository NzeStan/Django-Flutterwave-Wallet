"""
Pluggable notifications driven by wallet signals.

Nothing is sent unless ``FLW_WALLET_NOTIFICATION_BACKENDS`` is configured. See
:mod:`flutterwave_wallet.notifications.backends` for writing your own backend.
"""
import logging

from flutterwave_wallet import signals
from flutterwave_wallet.conf import wallet_settings

logger = logging.getLogger('flutterwave_wallet.notifications')


def get_backends():
    return [cls() if isinstance(cls, type) else cls for cls in wallet_settings.import_list('NOTIFICATION_BACKENDS')]


def notify(event, user, **context):
    """Send ``event`` to ``user`` through every configured backend. Never raises."""
    if user is None:
        return
    for backend in get_backends():
        if not backend.handles(event):
            continue
        try:
            backend.send(event, user, context)
        except Exception:
            logger.exception("Notification backend %s failed for %s", type(backend).__name__, event)


def _ctx(transaction, wallet):
    return {
        'transaction': transaction, 'wallet': wallet, 'amount': transaction.amount,
        'total_amount': transaction.total_amount, 'fees': transaction.fees, 'reference': transaction.reference,
        'balance': wallet.balance if wallet is not None else None, 'description': transaction.description,
    }


def _simple(event):
    def receiver(sender, transaction, wallet, **kwargs):
        notify(event, wallet.user, reason=kwargs.get('reason'), **_ctx(transaction, wallet))
    receiver.__name__ = f'_on_{event}'
    return receiver


def _on_transfer_completed(sender, transaction, wallet, recipient_transaction, recipient_wallet, **kwargs):
    notify('transfer_sent', wallet.user, recipient=recipient_wallet, **_ctx(transaction, wallet))
    notify('transfer_received', recipient_wallet.user, sender_wallet=wallet,
           **_ctx(recipient_transaction, recipient_wallet))


def _on_payment_released(sender, transaction, wallet, merchant_wallet, merchant_transaction, **kwargs):
    notify('payment_received', merchant_wallet.user, payer_wallet=wallet,
           **_ctx(merchant_transaction, merchant_wallet))


def _on_virtual_account_created(sender, virtual_account, wallet, **kwargs):
    notify('virtual_account_created', wallet.user, wallet=wallet, account_number=virtual_account.account_number,
           bank_name=virtual_account.bank_name)


def _on_wallet_locked(sender, wallet, reason='', **kwargs):
    notify('wallet_locked', wallet.user, wallet=wallet, reason=reason)


_CONNECTIONS = (
    (signals.deposit_completed, _simple('deposit_completed')),
    (signals.deposit_failed, _simple('deposit_failed')),
    (signals.withdrawal_completed, _simple('withdrawal_completed')),
    (signals.withdrawal_failed, _simple('withdrawal_failed')),
    (signals.transfer_completed, _on_transfer_completed),
    (signals.payment_completed, _simple('payment_made')),
    (signals.payment_released, _on_payment_released),
    (signals.payment_cancelled, _simple('payment_cancelled')),
    (signals.bill_payment_completed, _simple('bill_payment_completed')),
    (signals.bill_payment_failed, _simple('bill_payment_failed')),
    (signals.refund_completed, _simple('refund_completed')),
    (signals.refund_failed, _simple('refund_failed')),
    (signals.virtual_account_created, _on_virtual_account_created),
    (signals.wallet_locked, _on_wallet_locked),
)


def connect():
    """Connect notification receivers (idempotent). Called from the app's ``ready()``."""
    for signal, receiver in _CONNECTIONS:
        signal.connect(receiver, dispatch_uid=f'flutterwave_wallet.notifications.{receiver.__name__}', weak=False)


def disconnect():
    for signal, receiver in _CONNECTIONS:
        signal.disconnect(dispatch_uid=f'flutterwave_wallet.notifications.{receiver.__name__}')
