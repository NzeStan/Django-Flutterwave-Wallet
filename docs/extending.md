# Extending

## Signals

All signals live in `flutterwave_wallet.signals` and fire **after commit**:

| Group | Signals |
| --- | --- |
| Wallets | `wallet_created`, `wallet_locked`, `wallet_unlocked` |
| Deposits | `deposit_initialized`, `charge_requires_action`, `deposit_completed`, `deposit_failed` |
| Withdrawals | `withdrawal_initiated`, `withdrawal_completed`, `withdrawal_failed` |
| Transfers and payments | `transfer_completed`, `payment_completed`, `payment_released`, `payment_cancelled` |
| Bills | `bill_payment_completed`, `bill_payment_failed` |
| Refunds and reversals | `refund_initiated`, `refund_completed`, `refund_failed`, `transaction_reversed` |
| Any transaction | `transaction_status_changed`, `transaction_needs_review` |
| Accounts and KYC | `card_saved`, `bank_account_added`, `virtual_account_created`, `kyc_completed` |
| Fees | `platform_fee_recorded`, `fees_swept` |
| Webhooks | `flutterwave_webhook_received` (every event), `subscription_cancelled`, `virtual_card_event` |

```python
from django.dispatch import receiver
from flutterwave_wallet.signals import payment_released

@receiver(payment_released)
def ship(sender, transaction, wallet, merchant_wallet, merchant_transaction, **kwargs):
    Order.objects.filter(payment_reference=transaction.reference).update(status='paid')
```

## Notifications

```python
FLW_WALLET_NOTIFICATION_BACKENDS = ['flutterwave_wallet.notifications.backends.EmailNotificationBackend',
                                    'myapp.notify.SMSBackend']
```

```python
from flutterwave_wallet.notifications.backends import BaseNotificationBackend

class SMSBackend(BaseNotificationBackend):
    events = {'deposit_completed', 'transfer_received', 'withdrawal_failed'}

    def send(self, event, user, context):
        sms_queue.enqueue(user.profile.phone, self.render_text(event, context))
```

Override `templates/flutterwave_wallet/notifications/<event>.txt` to change the wording. A failing
backend is logged and never affects the others.

## Custom fees

```python
from decimal import Decimal
from flutterwave_wallet.services.fee_service import FeeCalculator

class TieredFees(FeeCalculator):
    def get_fee(self, amount, currency, operation, rule, context):
        if operation == 'transfer' and self.wallet and self.wallet.metadata.get('tier') == 'gold':
            return Decimal('0'), 'custom', {'tier': 'gold'}
        return super().get_fee(amount, currency, operation, rule, context)

FLW_WALLET_FEE_CALCULATOR = 'myapp.fees.TieredFees'
```

## Your own endpoints

The viewsets are ordinary DRF classes. Register only the ones you want:

```python
from flutterwave_wallet.urls import router          # or build your own DefaultRouter
from flutterwave_wallet.apis.wallet_api import WalletViewSet
```

Subclass `WalletAPIMixin` in your own views to get the same error mapping, idempotency and
throttling.

## Webhooks you don't handle

```python
from flutterwave_wallet.signals import flutterwave_webhook_received

@receiver(flutterwave_webhook_received)
def on_event(sender, event, data, webhook_event, handled, **kwargs):
    if event == 'subscription.cancelled':
        ...
```
