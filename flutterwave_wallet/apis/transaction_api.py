"""
Transactions of the logged-in user, plus staff operations.

    GET    transactions/                         the user's transactions (all wallets)
    GET    transactions/{id}/
    POST   transactions/{id}/release/            buyer (or staff) releases an escrow payment
    POST   transactions/{id}/cancel/             merchant (or staff) cancels an escrow payment, refunding the buyer
    POST   transactions/{id}/refund/             staff: refund a deposit to the payer's card/account
    POST   transactions/{id}/reverse/            staff: reverse a transfer/payment
    POST   transactions/{id}/requery/            staff: re-check with Flutterwave
    POST   transactions/{id}/force-reverse/      staff: refund a withdrawal confirmed never paid out
    GET    transactions/review-queue/            staff: transactions flagged for review
"""
from django.db.models import Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from flutterwave_wallet.apis.base import IsStaff, WalletAPIMixin
from flutterwave_wallet.constants import (
    TRANSACTION_TYPE_BILL_PAYMENT,
    TRANSACTION_TYPE_DEPOSIT,
    TRANSACTION_TYPE_REFUND,
    TRANSACTION_TYPE_WITHDRAWAL,
)
from flutterwave_wallet.models import Transaction
from flutterwave_wallet.serializers import (
    ReasonSerializer,
    RefundSerializer,
    StaffTransactionSerializer,
    TransactionDetailSerializer,
    TransactionFilterSerializer,
    TransactionSerializer,
)
from flutterwave_wallet.services.wallet_service import WalletService


class TransactionViewSet(WalletAPIMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = TransactionSerializer
    staff_actions = ('refund', 'reverse', 'requery', 'force_reverse', 'review_queue')
    throttle_scopes = {'release': 'money', 'cancel': 'money'}
    idempotent_actions = ('refund', 'reverse', 'release', 'cancel', 'force_reverse')

    def get_queryset(self):
        if self.action in self.staff_actions or (self.request.user.is_staff and self.action in ('release', 'cancel')):
            queryset = Transaction.objects.all()
        else:
            user = self.request.user
            # Users see their own entries; merchants may act on escrow payments addressed to them
            own = Q(wallet__user=user)
            if self.action == 'cancel':
                own |= Q(counterparty_wallet__user=user)
            queryset = Transaction.objects.filter(own)
        return queryset.select_related('wallet', 'counterparty_wallet__user', 'bank_account', 'card').order_by(
            '-created_at')

    def get_serializer_class(self):
        if self.action in self.staff_actions:
            return StaffTransactionSerializer
        return TransactionDetailSerializer if self.action == 'retrieve' else TransactionSerializer

    def list(self, request, *args, **kwargs):
        filters = self.validated(TransactionFilterSerializer, data=request.query_params)
        queryset = self.get_queryset()
        if filters.get('type'):
            queryset = queryset.filter(transaction_type=filters['type'])
        if filters.get('status'):
            queryset = queryset.filter(status=filters['status'])
        if filters.get('direction'):
            queryset = queryset.filter(direction=filters['direction'])
        if request.query_params.get('currency'):
            queryset = queryset.filter(amount_currency=request.query_params['currency'].upper()[:3])
        return self.paginated(queryset, TransactionSerializer)

    def _detail(self, txn, code=status.HTTP_200_OK):
        txn.refresh_from_db()
        serializer = StaffTransactionSerializer if self.action in self.staff_actions else TransactionDetailSerializer
        return Response(serializer(txn).data, status=code)

    # ------------------------------------------------------------------
    # Escrow
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        txn = self.get_object()
        if txn.wallet.user_id != request.user.pk and not request.user.is_staff:
            raise PermissionDenied()
        WalletService().release_payment(txn, performed_by=request.user)
        return self._detail(txn)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        txn = self.get_object()
        is_merchant = txn.counterparty_wallet_id and txn.counterparty_wallet.user_id == request.user.pk
        if not (is_merchant or request.user.is_staff):
            raise PermissionDenied()
        data = self.validated(ReasonSerializer)
        WalletService().cancel_payment(txn, reason=data.get('reason'), performed_by=request.user)
        return self._detail(txn)

    # ------------------------------------------------------------------
    # Staff
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        data = self.validated(RefundSerializer)
        refund = WalletService().refund_deposit(self.get_object(), amount=data.get('amount'),
                                                reason=data.get('reason'), performed_by=request.user)
        return self._detail(refund, status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def reverse(self, request, pk=None):
        data = self.validated(ReasonSerializer)
        reversal = WalletService().reverse_transaction(self.get_object(), reason=data.get('reason'),
                                                       performed_by=request.user)
        return self._detail(reversal, status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='force-reverse')
    def force_reverse(self, request, pk=None):
        data = self.validated(ReasonSerializer)
        txn = WalletService().force_reverse_withdrawal(self.get_object(), reason=data.get('reason') or 'Staff reversal',
                                                       performed_by=request.user)
        return self._detail(txn)

    @action(detail=True, methods=['post'])
    def requery(self, request, pk=None):
        txn = self.get_object()
        service = WalletService()
        handlers = {
            TRANSACTION_TYPE_DEPOSIT: service.reconcile_deposit,
            TRANSACTION_TYPE_WITHDRAWAL: service.verify_withdrawal,
            TRANSACTION_TYPE_BILL_PAYMENT: service.verify_bill_payment,
            TRANSACTION_TYPE_REFUND: service.verify_refund,
        }
        handler = handlers.get(txn.transaction_type)
        if handler is not None and txn.is_pending:
            handler(txn)
        return self._detail(txn)

    @action(detail=False, methods=['get'], url_path='review-queue', permission_classes=[IsStaff])
    def review_queue(self, request):
        return self.paginated(Transaction.objects.needing_review().order_by('-created_at'),
                              StaffTransactionSerializer)
