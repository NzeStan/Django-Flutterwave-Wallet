"""
Reconciliation: the safety net for webhooks that never arrived.

Finds stale pending deposits, withdrawals, bill payments and refunds and asks
Flutterwave for the truth. Each transaction is claimed in the cache before it is
checked, so any number of workers can run this at once without duplicate API calls;
correctness never depends on the claim (settlement is idempotent under row locks).
"""
import logging
from datetime import timedelta

from django.utils import timezone

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    ACTIVE_STATUSES,
    TRANSACTION_TYPE_BILL_PAYMENT,
    TRANSACTION_TYPE_DEPOSIT,
    TRANSACTION_TYPE_REFUND,
    TRANSACTION_TYPE_WITHDRAWAL,
)
from flutterwave_wallet.exceptions import WalletError
from flutterwave_wallet.models import Transaction, Wallet
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.ledger import verify_wallet_ledger

logger = logging.getLogger('flutterwave_wallet')


class ReconciliationService(BaseService):

    def _stale(self, transaction_type, older_than_minutes, limit):
        minutes = older_than_minutes if older_than_minutes is not None else wallet_settings.RECONCILE_AFTER_MINUTES
        cutoff = timezone.now() - timedelta(minutes=int(minutes))
        return (Transaction.objects.filter(transaction_type=transaction_type, status__in=ACTIVE_STATUSES,
                                           updated_at__lte=cutoff)
                .select_related('wallet').order_by('updated_at')[:limit or int(wallet_settings.RECONCILE_BATCH_SIZE)])

    def _run(self, transaction_type, check, older_than_minutes=None, limit=None):
        checked = 0
        for txn in self._stale(transaction_type, older_than_minutes, limit):
            key = f'reconcile:{txn.pk}'
            if not self.claim(key, 300):
                continue
            try:
                check(txn)
                checked += 1
            except (WalletError, Exception) as exc:  # one bad transaction must not stop the batch
                logger.warning("Reconciliation of %s %s failed: %s", transaction_type, txn.reference, exc)
            finally:
                self.release(key)
            # Touch so a permanently-unresolvable row doesn't block the head of the queue
            Transaction.objects.filter(pk=txn.pk, status__in=ACTIVE_STATUSES).update(updated_at=timezone.now())
        return checked

    def reconcile_deposits(self, older_than_minutes=None, limit=None):
        from flutterwave_wallet.services.deposit_service import DepositService
        return self._run(TRANSACTION_TYPE_DEPOSIT, DepositService(**self.clients()).reconcile_deposit,
                         older_than_minutes, limit)

    def reconcile_withdrawals(self, older_than_minutes=None, limit=None):
        from flutterwave_wallet.services.withdrawal_service import WithdrawalService
        return self._run(TRANSACTION_TYPE_WITHDRAWAL, WithdrawalService(**self.clients()).verify_withdrawal,
                         older_than_minutes, limit)

    def reconcile_bill_payments(self, older_than_minutes=None, limit=None):
        from flutterwave_wallet.services.bill_service import BillPaymentService
        return self._run(TRANSACTION_TYPE_BILL_PAYMENT, BillPaymentService(**self.clients()).verify_bill_payment,
                         older_than_minutes, limit)

    def reconcile_refunds(self, older_than_minutes=None, limit=None):
        from flutterwave_wallet.services.refund_service import RefundService
        return self._run(TRANSACTION_TYPE_REFUND, RefundService(**self.clients()).verify_refund,
                         older_than_minutes, limit)

    def reconcile(self, older_than_minutes=None, limit=None):
        return {
            'deposits': self.reconcile_deposits(older_than_minutes, limit),
            'withdrawals': self.reconcile_withdrawals(older_than_minutes, limit),
            'bill_payments': self.reconcile_bill_payments(older_than_minutes, limit),
            'refunds': self.reconcile_refunds(older_than_minutes, limit),
        }

    @staticmethod
    def verify_ledgers(wallets=None):
        """Check every wallet's ledger (balances, sequence, hash chain). Returns ``{wallet_id: [problems]}``."""
        report = {}
        queryset = wallets if wallets is not None else Wallet.objects.all()
        for wallet in queryset.iterator() if hasattr(queryset, 'iterator') else queryset:
            problems = verify_wallet_ledger(wallet)
            if problems:
                report[str(wallet.pk)] = problems
                logger.error("Ledger problems in wallet %s: %s", wallet.pk, problems)
        return report
