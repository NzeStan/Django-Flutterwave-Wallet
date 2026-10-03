"""
Checkout callback (backend only - no templates).

Flutterwave redirects the customer to ``redirect_url?status=...&tx_ref=...&transaction_id=...``
after checkout. Point ``FLW_WALLET_DEFAULT_REDIRECT_URL`` at this view: it verifies the
payment **server-side** (the query string is attacker-controlled and never trusted) and then
redirects to ``FLW_WALLET_CALLBACK_REDIRECT_URL`` (your frontend) with ``?tx_ref=&status=``,
or answers with JSON when no redirect is configured.
"""
import logging
from urllib.parse import urlencode

from django.core.exceptions import DisallowedRedirect
from django.http import HttpResponseRedirect, JsonResponse
from django.views import View

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import TRANSACTION_TYPE_DEPOSIT
from flutterwave_wallet.exceptions import WalletError
from flutterwave_wallet.models import Transaction
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.deposit_service import DepositService

logger = logging.getLogger('flutterwave_wallet')


class CheckoutCallbackView(View):
    http_method_names = ['get']

    def get(self, request):
        reference = (request.GET.get('tx_ref') or '')[:100]
        transaction_id = request.GET.get('transaction_id') or ''
        state = 'unknown'
        txn = Transaction.objects.filter(reference=reference, transaction_type=TRANSACTION_TYPE_DEPOSIT).first() \
            if reference else None
        if txn is not None:
            # One verification per deposit per 30s: the URL is public, so it must not become a way to
            # make us hammer Flutterwave's API.
            if txn.is_pending and BaseService.claim(f'callback:{txn.pk}', 30):
                try:
                    service = DepositService()
                    if transaction_id.isdigit():
                        service.verify_and_settle(int(transaction_id), expected_reference=reference)
                    else:
                        service.verify_deposit(reference)
                except WalletError as exc:
                    logger.warning("Callback verification failed for %s: %s", reference, exc)
            txn.refresh_from_db()
            state = txn.status

        redirect_to = wallet_settings.CALLBACK_REDIRECT_URL
        if redirect_to:
            separator = '&' if '?' in redirect_to else '?'
            try:
                return HttpResponseRedirect(f"{redirect_to}{separator}{urlencode({'tx_ref': reference, 'status': state})}")
            except DisallowedRedirect:
                logger.error("FLW_WALLET_CALLBACK_REDIRECT_URL is not a valid redirect; answering with JSON")
        return JsonResponse({'tx_ref': reference, 'status': state}, status=200 if txn is not None else 404)
