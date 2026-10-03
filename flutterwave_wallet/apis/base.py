"""Shared API plumbing: permissions, error mapping, pagination, PIN checks."""
import ipaddress
import logging

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import transaction as db_transaction
from django.utils.module_loading import import_string
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from flutterwave_wallet.apis import protection
from flutterwave_wallet.conf import is_feature_enabled, wallet_settings
from flutterwave_wallet.exceptions import FeatureDisabled, FlutterwaveAPIError, PinRequired, WalletError
from flutterwave_wallet.models import IdempotencyRecord

logger = logging.getLogger('flutterwave_wallet')


def _valid_ip(value):
    try:
        return str(ipaddress.ip_address(value.strip()))
    except (ValueError, AttributeError):
        return None


def get_client_ip(request):
    """Client IP. ``X-Forwarded-For`` is only trusted with ``FLW_WALLET_TRUST_X_FORWARDED_FOR``."""
    if wallet_settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if forwarded:
            ip = _valid_ip(forwarded.split(',')[0])
            if ip:
                return ip
    return _valid_ip(request.META.get('REMOTE_ADDR', '') or '')


def request_context(request):
    return {'ip_address': get_client_ip(request), 'user_agent': request.META.get('HTTP_USER_AGENT', '')}


def error_response(message, status_code, code=None, **extra):
    payload = {'detail': str(message)}
    if code:
        payload['code'] = code
    payload.update(extra)
    return Response(payload, status=status_code)


class WalletPagination(PageNumberPagination):
    page_size_query_param = 'page_size'

    @property
    def page_size(self):
        return int(wallet_settings.API_PAGE_SIZE)

    @property
    def max_page_size(self):
        return int(wallet_settings.API_MAX_PAGE_SIZE)


class IsStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


class WalletAPIMixin:
    """
    Mixin for every wallet viewset:

    * permissions come from ``FLW_WALLET_API_PERMISSION_CLASSES`` (``staff_actions`` need staff)
    * :class:`~flutterwave_wallet.exceptions.WalletError` becomes a JSON error with the right
      status; Flutterwave failures never leak internals
    * ``feature`` switches a viewset off with ``FLW_WALLET_ENABLE_<FEATURE>=False``
    * ``throttle_scopes`` maps actions to ``FLW_WALLET_THROTTLE_RATES`` scopes
    * ``idempotent_actions`` honour the ``Idempotency-Key`` header
    """

    pagination_class = WalletPagination
    feature = None

    @classmethod
    def as_view(cls, *args, **kwargs):
        # Services manage their own transactions; ATOMIC_REQUESTS would turn committed debits into savepoints
        return db_transaction.non_atomic_requests(super().as_view(*args, **kwargs))
    staff_actions = ()
    throttle_scopes = {}
    idempotent_actions = ()

    def get_permissions(self):
        if self.action in self.staff_actions:
            return [IsStaff()]
        classes = [import_string(path) if isinstance(path, str) else path
                   for path in wallet_settings.API_PERMISSION_CLASSES]
        return [permission() for permission in classes]

    def get_throttles(self):
        throttles = list(super().get_throttles())
        scope = self.throttle_scopes.get(getattr(self, 'action', None))
        if scope:
            throttles.append(protection.WalletRateThrottle(scope))
        return throttles

    def initial(self, request, *args, **kwargs):
        self._idempotency_record = None
        super().initial(request, *args, **kwargs)
        if self.feature and not is_feature_enabled(self.feature):
            raise FeatureDisabled()
        if self.action in self.idempotent_actions:
            self._idempotency_record = protection.begin(request)

    def finalize_response(self, request, response, *args, **kwargs):
        record = getattr(self, '_idempotency_record', None)
        if record is not None:
            self._idempotency_record = None
            protection.finish(record, response)
        return super().finalize_response(request, response, *args, **kwargs)

    def handle_exception(self, exc):
        try:
            return self._handle_exception(exc)
        except BaseException:
            # Unhandled error: release the idempotency key or every retry would be "in progress"
            record = getattr(self, '_idempotency_record', None)
            if record is not None:
                self._idempotency_record = None
                IdempotencyRecord.objects.filter(pk=record.pk).delete()
            raise

    def _handle_exception(self, exc):
        if isinstance(exc, protection.IdempotentReplay):
            return Response(exc.record.response_body, status=exc.record.response_status,
                            headers={'Idempotent-Replayed': 'true'})
        if isinstance(exc, FlutterwaveAPIError):
            logger.warning("Flutterwave error in %s: %s", type(self).__name__, exc)
            detail = exc.flutterwave_message if exc.is_definitive and exc.flutterwave_message else \
                'Payment provider error. Please try again.'
            return error_response(detail, exc.http_status, exc.api_code)
        if isinstance(exc, WalletError):
            return Response(exc.as_dict(), status=exc.http_status)
        if isinstance(exc, DjangoValidationError):
            return Response({'detail': exc.messages}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, ObjectDoesNotExist):
            return error_response('Not found.', status.HTTP_404_NOT_FOUND, 'not_found')
        return super().handle_exception(exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def check_pin(self, pin):
        """Enforce the transaction PIN when ``FLW_WALLET_REQUIRE_TRANSACTION_PIN`` is on."""
        if not wallet_settings.REQUIRE_TRANSACTION_PIN:
            return
        if not pin:
            raise PinRequired()
        from flutterwave_wallet.services.wallet_service import WalletService
        WalletService().verify_pin(self.request.user, pin)

    def validated(self, serializer_class, data=None):
        context = self.get_serializer_context() if hasattr(self, 'get_serializer_context') else             {'request': self.request, 'view': self}
        serializer = serializer_class(data=self.request.data if data is None else data, context=context)
        if not serializer.is_valid():
            raise ValidationError(serializer.errors)
        return serializer.validated_data

    def paginated(self, queryset, serializer_class):
        context = self.get_serializer_context()
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(serializer_class(page, many=True, context=context).data)
        return Response(serializer_class(queryset, many=True, context=context).data)
