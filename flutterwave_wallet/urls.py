"""
Include in your project::

    path('wallet/', include('flutterwave_wallet.urls')),

which gives ``/wallet/api/...``, ``/wallet/webhook/`` and ``/wallet/callback/``.

Only need some parts? Build your own urlpatterns from ``router`` or register the
viewsets from :mod:`flutterwave_wallet.apis` yourself.
"""
from django.db.transaction import non_atomic_requests
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from flutterwave_wallet.apis.account_api import (
    BankAccountViewSet,
    BankViewSet,
    BillViewSet,
    CardViewSet,
    ProfileViewSet,
)
from flutterwave_wallet.apis.transaction_api import TransactionViewSet
from flutterwave_wallet.apis.wallet_api import WalletViewSet
from flutterwave_wallet.apis.webhook_api import (
    WebhookDeliveryAttemptViewSet,
    WebhookEndpointViewSet,
    WebhookEventViewSet,
    flutterwave_webhook,
)
from flutterwave_wallet.views import CheckoutCallbackView

app_name = 'flutterwave_wallet'

router = DefaultRouter()
router.register(r'wallets', WalletViewSet, basename='wallet')
router.register(r'transactions', TransactionViewSet, basename='transaction')
router.register(r'cards', CardViewSet, basename='card')
router.register(r'banks', BankViewSet, basename='bank')
router.register(r'bank-accounts', BankAccountViewSet, basename='bank-account')
router.register(r'bills', BillViewSet, basename='bill')
router.register(r'webhook-events', WebhookEventViewSet, basename='webhook-event')
router.register(r'webhook-endpoints', WebhookEndpointViewSet, basename='webhook-endpoint')
router.register(r'webhook-deliveries', WebhookDeliveryAttemptViewSet, basename='webhook-delivery')

profile_patterns = [
    path('', ProfileViewSet.as_view({'get': 'retrieve_profile', 'patch': 'update_profile'}), name='profile'),
    path('set-pin/', ProfileViewSet.as_view({'post': 'set_pin'}), name='profile-set-pin'),
    path('verify-bvn/', ProfileViewSet.as_view({'post': 'verify_bvn'}), name='profile-verify-bvn'),
    path('refresh-kyc/', ProfileViewSet.as_view({'post': 'refresh_kyc'}), name='profile-refresh-kyc'),
]

urlpatterns = [
    path('api/profile/', include(profile_patterns)),
    path('api/', include(router.urls)),
    path('webhook/', flutterwave_webhook, name='flutterwave-webhook'),
    path('callback/', non_atomic_requests(CheckoutCallbackView.as_view()), name='checkout-callback'),
]
