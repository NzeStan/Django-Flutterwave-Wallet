"""
Profile, cards, banks, payout accounts and bills.

    GET    profile/                 tag, phone, PIN set?, KYC status
    PATCH  profile/                 {tag, phone_number}  (routes are wired in urls.py)
    POST   profile/set-pin/         {pin, current_pin}
    POST   profile/verify-bvn/      start BVN consent -> {url}
    POST   profile/refresh-kyc/
    GET    cards/                   DELETE cards/{id}/   POST cards/{id}/set-default/
    GET    banks/?country=NG
    GET    bank-accounts/           POST bank-accounts/   DELETE bank-accounts/{id}/
    POST   bank-accounts/resolve/   POST bank-accounts/{id}/set-default/
    GET    bills/categories/  bills/billers/?category=  bills/items/?biller_code=
    POST   bills/validate/
"""
from django.utils.translation import gettext_lazy as _
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from flutterwave_wallet.apis.base import WalletAPIMixin, error_response
from flutterwave_wallet.models import Bank, BankAccount, Card
from flutterwave_wallet.serializers import (
    BankAccountCreateSerializer,
    BankAccountSerializer,
    BankSerializer,
    BillValidateSerializer,
    BVNVerificationSerializer,
    CardSerializer,
    ProfileSerializer,
    ProfileUpdateSerializer,
    ResolveAccountSerializer,
    SetPinSerializer,
)
from flutterwave_wallet.services.wallet_service import WalletService


class ProfileViewSet(WalletAPIMixin, viewsets.ViewSet):
    throttle_scopes = {'set_pin': 'pin', 'verify_bvn': 'kyc', 'refresh_kyc': 'kyc', 'update_profile': 'lookup'}

    def retrieve_profile(self, request):
        return Response(ProfileSerializer(WalletService().get_profile(request.user)).data)

    def update_profile(self, request):
        data = self.validated(ProfileUpdateSerializer)
        service = WalletService()
        if 'tag' in data:
            service.set_tag(request.user, data['tag'])
        if 'phone_number' in data:
            service.set_phone_number(request.user, data['phone_number'] or None)
        return Response(ProfileSerializer(service.get_profile(request.user)).data)

    def set_pin(self, request):
        data = self.validated(SetPinSerializer)
        WalletService().set_pin(request.user, data['pin'], data.get('current_pin'))
        return Response({'detail': _("PIN saved"), 'has_pin': True})

    def verify_bvn(self, request):
        data = self.validated(BVNVerificationSerializer)
        result = WalletService().start_bvn_verification(request.user, data['bvn'], data['firstname'],
                                                        data['lastname'], data.get('redirect_url'))
        return Response(result, status=status.HTTP_202_ACCEPTED)

    def refresh_kyc(self, request):
        service = WalletService()
        profile = service.refresh_bvn_verification(service.get_profile(request.user))
        return Response(ProfileSerializer(profile).data)


class CardViewSet(WalletAPIMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                  viewsets.GenericViewSet):
    feature = 'CARDS'
    serializer_class = CardSerializer

    def get_queryset(self):
        return Card.objects.filter(user=self.request.user, is_active=True)

    def perform_destroy(self, instance):
        WalletService.remove_card(instance)

    @action(detail=True, methods=['post'], url_path='set-default')
    def set_default(self, request, pk=None):
        return Response(CardSerializer(WalletService.set_default_card(self.get_object())).data)


class BankViewSet(WalletAPIMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = BankSerializer
    pagination_class = None

    def get_queryset(self):
        queryset = Bank.objects.active()
        country = self.request.query_params.get('country')
        return queryset.for_country(country[:2]) if country else queryset


class BankAccountViewSet(WalletAPIMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                         viewsets.GenericViewSet):
    feature = 'WITHDRAWALS'
    serializer_class = BankAccountSerializer
    throttle_scopes = {'create': 'resolve', 'resolve': 'resolve'}

    def get_queryset(self):
        return BankAccount.objects.filter(user=self.request.user, is_active=True)

    def create(self, request):
        data = self.validated(BankAccountCreateSerializer)
        # Adding a payout destination is sensitive: an attacker with a session would add their own account
        self.check_pin(data.get('pin'))
        account = WalletService().add_bank_account(
            request.user, data['account_bank'], data['account_number'], currency=data.get('currency'),
            account_name=data.get('account_name'), beneficiary_type=data.get('beneficiary_type', 'bank'),
            country=data.get('country'), branch_code=data.get('branch_code'), make_default=data.get('is_default'),
        )
        return Response(BankAccountSerializer(account).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        WalletService().remove_bank_account(instance)

    @action(detail=False, methods=['post'])
    def resolve(self, request):
        data = self.validated(ResolveAccountSerializer)
        return Response(WalletService().resolve_account(data['account_number'], data['account_bank']))

    @action(detail=True, methods=['post'], url_path='set-default')
    def set_default(self, request, pk=None):
        return Response(BankAccountSerializer(WalletService.set_default(self.get_object())).data)


class BillViewSet(WalletAPIMixin, viewsets.ViewSet):
    feature = 'BILL_PAYMENTS'
    throttle_scopes = {'validate': 'resolve', 'categories': 'lookup', 'billers': 'lookup', 'items': 'lookup'}

    @action(detail=False, methods=['get'])
    def categories(self, request):
        return Response(WalletService().bill_categories(request.query_params.get('country')))

    @action(detail=False, methods=['get'])
    def billers(self, request):
        category = request.query_params.get('category')
        if not category:
            return error_response(_("category is required"), status.HTTP_400_BAD_REQUEST, 'invalid')
        return Response(WalletService().billers(category[:50], request.query_params.get('country')))

    @action(detail=False, methods=['get'])
    def items(self, request):
        biller_code = request.query_params.get('biller_code')
        if not biller_code:
            return error_response(_("biller_code is required"), status.HTTP_400_BAD_REQUEST, 'invalid')
        return Response(WalletService().biller_items(biller_code[:50]))

    @action(detail=False, methods=['post'])
    def validate(self, request):
        data = self.validated(BillValidateSerializer)
        return Response(WalletService().validate_bill_customer(data['item_code'], data['customer_id']))
