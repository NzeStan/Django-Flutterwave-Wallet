"""
Signals - the main extension point of the package.

Every signal is sent *after* the database transaction commits, so receivers never
see money that could still roll back::

    from django.dispatch import receiver
    from flutterwave_wallet.signals import deposit_completed

    @receiver(deposit_completed)
    def on_deposit(sender, transaction, wallet, **kwargs):
        ...

All transaction signals pass ``transaction`` and ``wallet``; other arguments are
listed next to each signal.
"""
from django.db import transaction as db_transaction
from django.dispatch import Signal

# Wallet lifecycle
wallet_created = Signal()                    # wallet
wallet_locked = Signal()                     # wallet, reason
wallet_unlocked = Signal()                   # wallet

# Deposits (checkout, direct charges, saved cards, virtual accounts)
deposit_initialized = Signal()               # transaction, wallet, link (may be None)
charge_requires_action = Signal()            # transaction, wallet, action (dict: mode/redirect/instruction)
deposit_completed = Signal()                 # transaction, wallet
deposit_failed = Signal()                    # transaction, wallet, reason

# Withdrawals to bank / mobile money
withdrawal_initiated = Signal()              # transaction, wallet
withdrawal_completed = Signal()              # transaction, wallet
withdrawal_failed = Signal()                 # transaction, wallet, reason

# Wallet-to-wallet transfers
transfer_completed = Signal()                # transaction (sender leg), wallet, recipient_transaction, recipient_wallet

# Wallet payments (e-commerce / marketplace, optional escrow)
payment_completed = Signal()                 # transaction, wallet, merchant_wallet (may be None), escrow
payment_released = Signal()                  # transaction, wallet, merchant_wallet, merchant_transaction
payment_cancelled = Signal()                 # transaction, wallet

# Bill payments (airtime, data, cable, electricity, ...)
bill_payment_completed = Signal()            # transaction, wallet, data
bill_payment_failed = Signal()               # transaction, wallet, reason

# Refunds and reversals
refund_initiated = Signal()                  # transaction, wallet, original_transaction
refund_completed = Signal()                  # transaction, wallet, original_transaction
refund_failed = Signal()                     # transaction, wallet, original_transaction, reason
transaction_reversed = Signal()              # transaction (the reversal), wallet, original_transaction

# Every status transition of every transaction
transaction_status_changed = Signal()        # transaction, wallet, old_status, new_status
# Outcome unknown or amounts differ - route this to your ops team
transaction_needs_review = Signal()          # transaction, wallet, reason

# Instruments, accounts, identity
card_saved = Signal()                        # card, user, created
bank_account_added = Signal()                # bank_account, user
virtual_account_created = Signal()           # virtual_account, wallet
kyc_completed = Signal()                     # profile, user, success, data

# Fees
platform_fee_recorded = Signal()             # fee, transaction
fees_swept = Signal()                        # transaction (credit on the fee wallet), wallet, count

# Every Flutterwave webhook after built-in processing: event, data, webhook_event, handled
flutterwave_webhook_received = Signal()
# Specific Flutterwave events with no built-in money movement
subscription_cancelled = Signal()            # data, webhook_event
virtual_card_event = Signal()                # data, webhook_event


def send_on_commit(signal, sender=None, **kwargs):
    """Send ``signal`` once the current DB transaction commits (immediately if none is open)."""
    db_transaction.on_commit(lambda: signal.send(sender=sender, **kwargs))


__all__ = [name for name, value in dict(globals()).items() if isinstance(value, Signal)] + ['send_on_commit']
