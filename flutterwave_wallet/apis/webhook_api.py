"""
Flutterwave webhook receiver and (staff-only) webhook administration.

On the Flutterwave dashboard (Settings -> Webhooks) set the URL to::

    https://your-domain.com/<prefix>/webhook/

and a long random **secret hash** - the same value as ``FLUTTERWAVE_SECRET_HASH``.
"""
import logging

from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from flutterwave_wallet.apis.base import IsStaff, WalletPagination, get_client_ip
from flutterwave_wallet.exceptions import InvalidWebhookSignature, WalletError
from flutterwave_wallet.models import WebhookDeliveryAttempt, WebhookEndpoint, WebhookEvent
from flutterwave_wallet.serializers import (
    WebhookDeliveryAttemptSerializer,
    WebhookEndpointSerializer,
    WebhookEventSerializer,
)
from flutterwave_wallet.services.webhook_service import WebhookService

logger = logging.getLogger('flutterwave_wallet')


@method_decorator(csrf_exempt, name='dispatch')
class FlutterwaveWebhookView(APIView):
    """
    Receives Flutterwave events. Answers 200 for every authentic event (even if processing
    failed - it is stored and retried) so Flutterwave doesn't keep retrying; 401 for bad
    signatures; 400 for malformed payloads. Bodies are never echoed back.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request, *args, **kwargs):
        ip = get_client_ip(request)
        try:
            event, created = WebhookService().handle_webhook(request.body, request.headers, ip)
        except InvalidWebhookSignature as exc:
            logger.warning("Rejected webhook from %s: %s", ip, exc.message)
            return Response({'status': 'error', 'detail': 'invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)
        except ValueError:
            return Response({'status': 'error', 'detail': 'invalid payload'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'event_id': str(event.pk), 'duplicate': not created})


flutterwave_webhook = transaction.non_atomic_requests(FlutterwaveWebhookView.as_view())


class WebhookEventViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = WalletPagination
    serializer_class = WebhookEventSerializer
    permission_classes = [IsStaff]

    def get_queryset(self):
        queryset = WebhookEvent.objects.all()
        params = self.request.query_params
        if params.get('event_type'):
            queryset = queryset.filter(event_type=params['event_type'])
        if params.get('processed') in ('true', 'false'):
            queryset = queryset.filter(processed=params['processed'] == 'true')
        return queryset.order_by('-created_at')

    @action(detail=True, methods=['post'])
    def reprocess(self, request, pk=None):
        event = self.get_object()
        ok = WebhookService().process_event(event)
        event.refresh_from_db()
        return Response(WebhookEventSerializer(event).data, status=200 if ok else 500)


class WebhookEndpointViewSet(viewsets.ModelViewSet):
    pagination_class = WalletPagination
    serializer_class = WebhookEndpointSerializer
    permission_classes = [IsStaff]
    queryset = WebhookEndpoint.objects.all().order_by('name')

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        endpoint = self.get_object()
        event = WebhookEvent.objects.order_by('-created_at').first()
        if event is None:
            return Response({'detail': 'No webhook events to send yet'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WebhookDeliveryAttemptSerializer(WebhookService().deliver(event, endpoint)).data)


class WebhookDeliveryAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = WalletPagination
    serializer_class = WebhookDeliveryAttemptSerializer
    permission_classes = [IsStaff]
    queryset = WebhookDeliveryAttempt.objects.select_related('webhook_event', 'webhook_endpoint').order_by(
        '-created_at')

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        try:
            attempt = WebhookService().retry_delivery(self.get_object())
        except WalletError as exc:
            return Response(exc.as_dict(), status=status.HTTP_400_BAD_REQUEST)
        return Response(WebhookDeliveryAttemptSerializer(attempt).data)
