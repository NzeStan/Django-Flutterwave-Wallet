"""
Wallet endpoints (the logged-in user's wallets).

``{id}`` is a wallet UUID or ``me`` (the default-currency wallet; ``?currency=USD`` picks another).

    GET    wallets/                              the user's wallets
    POST   wallets/                              open a wallet {currency}
    GET    wallets/me/                           details
    GET    wallets/me/balance/
    GET    wallets/me/transactions/              ?type=&status=&direction=&start_date=&end_date=
    GET    wallets/me/statement/                 ledger order, ?start_date=&end_date=
    POST   wallets/me/checkout/                  Flutterwave Standard checkout link
    POST   wallets/me/charge/                    direct charge (card, bank_transfer, ussd, mpesa, mobile money...)
    POST   wallets/me/validate-charge/           {reference, otp}
    POST   wallets/me/bank-transfer-deposit/     one-off account number for a deposit
    POST   wallets/me/verify-deposit/            {reference}
    POST   wallets/me/charge-card/               saved-card top-up
    GET    wallets/me/virtual-account/           permanent account number
    POST   wallets/me/virtual-account/           create it {bvn|nin}
    POST   wallets/me/withdraw/                  to a payout account
    POST   wallets/me/transfer/                  {recipient: id|@tag|phone|email, amount}
    POST   wallets/me/pay/                       wallet checkout, optional escrow
    POST   wallets/me/pay-bill/                  airtime, data, cable, electricity...
    GET    wallets/lookup/?recipient=            confirm a recipient before sending
    POST   wallets/fee-quote/
"""
import uuid
from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.translation import gettext_lazy as _
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from flutterwave_wallet.apis.base import WalletAPIMixin, error_response, request_context
from flutterwave_wallet.conf import is_feature_enabled
from flutterwave_wallet.exceptions import FeatureDisabled, WalletNotFound
from flutterwave_wallet.models import BankAccount, Card, Transaction, Wallet
from flutterwave_wallet.serializers import (
    BankTransferDepositSerializer,
    BillPaymentSerializer,
    ChargeCardSerializer,
    CheckoutSerializer,
    DirectChargeSerializer,
    FeeQuoteSerializer,
    OpenWalletSerializer,
    PaySerializer,
    RecipientLookupSerializer,
    TransactionDetailSerializer,
    TransactionFilterSerializer,
    TransactionSerializer,
    TransferSerializer,
    ValidateChargeSerializer,
    VerifyDepositSerializer,
    VirtualAccountRequestSerializer,
    VirtualAccountSerializer,
    WalletDetailSerializer,
    WalletSerializer,
    WithdrawSerializer,
)
from flutterwave_wallet.services.fee_service import get_fee_calculator
from flutterwave_wallet.services.wallet_service import WalletService
from flutterwave_wallet.utils.money import to_money

ME = ('me', 'default')


def parse_when(value, end=False):
    """``2026-09-30`` or an ISO datetime -> aware datetime (dates cover the whole day)."""
    if not value:
        return None
    try:
        day = parse_date(value)
    except ValueError:
        day = None
    if day is not None:
        return timezone.make_aware(datetime.combine(day, time.max if end else time.min))
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


class WalletViewSet(WalletAPIMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = WalletSerializer
    throttle_scopes = {
        'lookup': 'lookup', 'validate_charge': 'otp',
        'checkout': 'money', 'charge': 'money', 'bank_transfer_deposit': 'money', 'charge_card': 'money',
        'withdraw': 'money', 'transfer': 'money', 'pay': 'money', 'pay_bill': 'money', 'virtual_account': 'kyc',
        'verify_deposit': 'resolve',
    }
    idempotent_actions = ('checkout', 'charge', 'bank_transfer_deposit', 'charge_card', 'withdraw', 'transfer',
                          'pay', 'pay_bill', 'validate_charge')

    @property
    def service(self):
        return WalletService()

    def get_queryset(self):
        return Wallet.objects.filter(user=self.request.user).select_related('user').prefetch_related(
            'virtual_accounts').order_by('created_at')

    def get_object(self):
        pk = self.kwargs.get(self.lookup_url_kwarg or self.lookup_field)
        if pk in ME:
            wallet = self.service.get_wallet(self.request.user, self.request.query_params.get('currency'))
        else:
            wallet = self.get_queryset().filter(pk=pk).first() if _is_uuid(pk) else None
            if wallet is None:
                raise WalletNotFound()
        self.check_object_permissions(self.request, wallet)
        return wallet

    def get_serializer_class(self):
        return WalletDetailSerializer if self.action == 'retrieve' else WalletSerializer

    def list(self, request, *args, **kwargs):
        self.service.get_wallet(request.user)
        return super().list(request, *args, **kwargs)

    def create(self, request):
        data = self.validated(OpenWalletSerializer)
        wallet = self.service.get_wallet(request.user, data['currency'])
        return Response(WalletDetailSerializer(wallet).data, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------
    # Balance & history
    # ------------------------------------------------------------------

    @action(detail=True, methods=['get'])
    def balance(self, request, pk=None):
        wallet = self.get_object()
        wallet.refresh_balance()
        return Response({'balance': f"{wallet.balance.amount:.2f}", 'currency': wallet.currency,
                         'is_operational': wallet.is_operational, 'updated_at': wallet.updated_at})

    @action(detail=True, methods=['get'])
    def transactions(self, request, pk=None):
        wallet = self.get_object()
        filters = self.validated(TransactionFilterSerializer, data=request.query_params)
        queryset = self.service.get_transactions(
            wallet, transaction_type=filters.get('type'), status=filters.get('status'),
            direction=filters.get('direction'), start_date=parse_when(request.query_params.get('start_date')),
            end_date=parse_when(request.query_params.get('end_date'), end=True),
        ).select_related('counterparty_wallet__user__wallet_profile')
        return self.paginated(queryset, TransactionSerializer)

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        wallet = self.get_object()
        queryset = self.service.get_statement(wallet, parse_when(request.query_params.get('start_date')),
                                              parse_when(request.query_params.get('end_date'), end=True))
        return self.paginated(queryset, TransactionSerializer)

    # ------------------------------------------------------------------
    # Deposits
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'])
    def checkout(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(CheckoutSerializer)
        options = data.get('payment_options')
        result = self.service.initialize_checkout(
            wallet, data['amount'], email=data.get('email'), redirect_url=data.get('redirect_url'),
            reference=data.get('reference'), metadata=data.get('metadata'),
            payment_options=','.join(options) if options else None, description=data.get('description'),
            **request_context(request),
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def charge(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(DirectChargeSerializer)
        result = self.service.direct_charge(
            wallet, data.get('amount'), data['charge_type'], payload=data.get('payload'),
            reference=data.get('reference'), metadata=data.get('metadata'), description=data.get('description'),
            **request_context(request),
        )
        return self._charge_response(result)

    @action(detail=True, methods=['post'], url_path='validate-charge')
    def validate_charge(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(ValidateChargeSerializer)
        txn = Transaction.objects.get(wallet=wallet, reference=data['reference'])
        return self._charge_response(self.service.validate_charge(txn, data['otp']))

    @staticmethod
    def _charge_response(result):
        txn = result.pop('transaction')
        result['transaction'] = TransactionDetailSerializer(txn).data
        code = {'success': status.HTTP_200_OK, 'pending': status.HTTP_202_ACCEPTED}.get(
            result['status'], status.HTTP_400_BAD_REQUEST)
        return Response(result, status=code)

    @action(detail=True, methods=['post'], url_path='bank-transfer-deposit')
    def bank_transfer_deposit(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(BankTransferDepositSerializer)
        result = self.service.create_bank_transfer_deposit(
            wallet, data['amount'], reference=data.get('reference'), description=data.get('description'),
            metadata=data.get('metadata'), **request_context(request),
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='verify-deposit')
    def verify_deposit(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(VerifyDepositSerializer)
        txn = Transaction.objects.filter(reference=data['reference'], wallet=wallet).first()
        if txn is None:
            return error_response(_("Deposit not found"), status.HTTP_404_NOT_FOUND, 'not_found')
        self.service.verify_deposit(txn.reference)
        txn.refresh_from_db()
        return Response(TransactionDetailSerializer(txn).data)

    @action(detail=True, methods=['post'], url_path='charge-card')
    def charge_card(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(ChargeCardSerializer)
        self.check_pin(data.get('pin'))
        cards = Card.objects.filter(user=request.user, is_active=True)
        card = cards.filter(pk=data['card_id']).first() if data.get('card_id') else \
            cards.order_by('-is_default', '-created_at').first()
        if card is None:
            return error_response(_("Card not found"), status.HTTP_404_NOT_FOUND, 'card_not_found')
        txn = self.service.charge_saved_card(
            card, data['amount'], wallet=wallet, reference=data.get('reference'), metadata=data.get('metadata'),
            description=data.get('description'), **request_context(request),
        )
        txn.refresh_from_db()
        code = status.HTTP_200_OK if txn.is_successful else (
            status.HTTP_202_ACCEPTED if txn.is_pending else status.HTTP_400_BAD_REQUEST)
        return Response(TransactionDetailSerializer(txn).data, status=code)

    @action(detail=True, methods=['get', 'post'], url_path='virtual-account')
    def virtual_account(self, request, pk=None):
        if not is_feature_enabled('VIRTUAL_ACCOUNTS'):
            raise FeatureDisabled()
        wallet = self.get_object()
        if request.method == 'POST':
            data = self.validated(VirtualAccountRequestSerializer)
            account = self.service.create_static_account(wallet, bvn=data.get('bvn'), nin=data.get('nin'))
            return Response(VirtualAccountSerializer(account).data, status=status.HTTP_201_CREATED)
        account = self.service.get_static_account(wallet)
        if account is None:
            return error_response(_("No virtual account yet"), status.HTTP_404_NOT_FOUND, 'no_virtual_account')
        return Response(VirtualAccountSerializer(account).data)

    # ------------------------------------------------------------------
    # Money out
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'])
    def withdraw(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(WithdrawSerializer)
        self.check_pin(data.get('pin'))
        accounts = BankAccount.objects.filter(user=request.user, is_active=True, currency=wallet.currency)
        account = accounts.filter(pk=data['bank_account_id']).first() if data.get('bank_account_id') else \
            accounts.order_by('-is_default', '-created_at').first()
        if account is None:
            return error_response(_("Payout account not found"), status.HTTP_404_NOT_FOUND, 'bank_account_not_found')
        txn = self.service.withdraw(wallet, data['amount'], account, narration=data.get('narration'),
                                    reference=data.get('reference'), metadata=data.get('metadata'),
                                    **request_context(request))
        txn.refresh_from_db()
        return Response(TransactionDetailSerializer(txn).data,
                        status=status.HTTP_200_OK if txn.is_successful else status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def transfer(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(TransferSerializer)
        self.check_pin(data.get('pin'))
        txn = self.service.transfer(wallet, data['recipient'], data['amount'], description=data.get('description'),
                                    metadata=data.get('metadata'), reference=data.get('reference'),
                                    lookup=data.get('recipient_type'), **request_context(request))
        return Response(TransactionDetailSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(PaySerializer)
        self.check_pin(data.get('pin'))
        txn = self.service.pay(wallet, data['amount'], merchant_wallet=data.get('merchant') or None,
                               description=data.get('description'), reference=data.get('reference'),
                               metadata=data.get('metadata'), escrow=data.get('escrow', False),
                               **request_context(request))
        return Response(TransactionDetailSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='pay-bill')
    def pay_bill(self, request, pk=None):
        wallet = self.get_object()
        data = self.validated(BillPaymentSerializer)
        self.check_pin(data.get('pin'))
        txn = self.service.pay_bill(wallet, data['biller_code'], data['item_code'], data['customer_id'],
                                    data['amount'], reference=data.get('reference'),
                                    description=data.get('description'), metadata=data.get('metadata'),
                                    **request_context(request))
        txn.refresh_from_db()
        return Response(TransactionDetailSerializer(txn).data,
                        status=status.HTTP_200_OK if txn.is_successful else status.HTTP_202_ACCEPTED)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @action(detail=False, methods=['get'])
    def lookup(self, request):
        if not is_feature_enabled('RECIPIENT_LOOKUP'):
            raise FeatureDisabled()
        data = self.validated(RecipientLookupSerializer, data=request.query_params)
        service = self.service
        currency = service.normalize_currency(request.query_params.get('currency'))
        recipient = service.resolve_recipient(data['recipient'], currency, data.get('recipient_type'))
        return Response(service.describe_recipient(recipient))

    @action(detail=False, methods=['post'], url_path='fee-quote')
    def fee_quote(self, request):
        data = self.validated(FeeQuoteSerializer)
        wallet = self.service.get_wallet(request.user, data.get('currency'))
        result = get_fee_calculator(wallet).calculate(to_money(data['amount'], wallet.currency), data['operation'])
        return Response(result.to_dict())
