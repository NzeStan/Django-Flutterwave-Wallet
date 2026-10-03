"""
Platform fee accounting.

Fees are accrued as :class:`~flutterwave_wallet.models.PlatformFee` rows on the
transaction that earned them, then swept in one ledger posting per currency into the
fee collection wallet (``FLW_WALLET_FEE_COLLECTION_WALLET_ID``). Run the sweep from
Celery beat (``sweep_platform_fees_task``) or cron (``manage.py sweep_platform_fees``).
"""
import logging
from collections import defaultdict
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_TYPE_FEE,
)
from flutterwave_wallet.models import PlatformFee, Transaction, Wallet
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.signals import fees_swept, send_on_commit
from flutterwave_wallet.utils.references import generate_reference

logger = logging.getLogger('flutterwave_wallet')


class FeeService(BaseService):

    @staticmethod
    def void_fees(txn):
        """Void the fees of a reversed transaction (call inside the reversal's DB transaction)."""
        for fee in PlatformFee.objects.select_for_update().filter(transaction=txn, voided=False,
                                                                  amount__gt=0):
            if fee.swept:
                PlatformFee.objects.get_or_create(
                    transaction=txn, operation=f"clawback:{fee.operation}"[:20],
                    defaults={'amount': Money(-fee.amount.amount, fee.amount.currency), 'bearer': fee.bearer,
                              'details': {'clawback_of': str(fee.pk)}},
                )
            fee.voided = True
            fee.save(update_fields=['voided', 'updated_at'])

    @staticmethod
    def fee_wallet():
        wallet_id = wallet_settings.FEE_COLLECTION_WALLET_ID
        return Wallet.objects.filter(pk=wallet_id).first() if wallet_id else None

    def sweep(self, batch_size=5000):
        """
        Move accrued fees into the fee collection wallet. Returns ``{currency: amount}`` swept.
        Safe to run concurrently: rows are claimed with ``SKIP LOCKED`` under the wallet lock.
        """
        wallet = self.fee_wallet()
        if wallet is None:
            return {}
        result = {}
        with db_transaction.atomic():
            locked = self.lock_wallet(wallet)
            fees = list(PlatformFee.objects.select_for_update(skip_locked=True).filter(
                swept=False, voided=False, amount_currency=locked.currency,
            ).order_by('created_at')[:batch_size])
            if not fees:
                return result
            net = sum((fee.amount.amount for fee in fees), Decimal('0'))
            if net == 0:
                PlatformFee.objects.filter(pk__in=[f.pk for f in fees]).update(swept=True, updated_at=timezone.now())
                return result
            direction = DIRECTION_CREDIT if net > 0 else DIRECTION_DEBIT
            if direction == DIRECTION_DEBIT and locked.balance.amount < -net:
                logger.warning("Fee wallet cannot cover clawbacks of %s yet", -net)
                return result
            entry = Transaction(
                wallet=locked, amount=Money(abs(net), locked.currency), total_amount=Money(abs(net), locked.currency),
                transaction_type=TRANSACTION_TYPE_FEE, direction=direction, status=TRANSACTION_STATUS_SUCCESS,
                completed_at=timezone.now(), reference=generate_reference('FEE'),
                description=str(_("Platform fees ({count} transactions)").format(count=len(fees))),
                metadata={'fee_count': len(fees)},
            )
            self.post(locked, entry, force=True, enforce_minimum_balance=False)
            PlatformFee.objects.filter(pk__in=[f.pk for f in fees]).update(
                swept=True, sweep_transaction=entry, updated_at=timezone.now())
            result[locked.currency] = net
            send_on_commit(fees_swept, sender=PlatformFee, transaction=entry, wallet=locked, count=len(fees))
        return result

    @staticmethod
    def totals(start_date=None, end_date=None):
        """Revenue per currency and operation (voided fees excluded)."""
        queryset = PlatformFee.objects.filter(voided=False)
        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__lte=end_date)
        totals = defaultdict(dict)
        for row in queryset.values('amount_currency', 'operation').annotate(total=Sum('amount')):
            totals[row['amount_currency']][row['operation']] = row['total']
        return dict(totals)
