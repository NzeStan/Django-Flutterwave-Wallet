"""
HTTP client for the Flutterwave v3 API.

    from flutterwave_wallet.flutterwave import FlutterwaveClient

    flw = FlutterwaveClient()                                  # uses FLUTTERWAVE_SECRET_KEY
    flw.payments.create(tx_ref='ord-1', amount=5000, currency='NGN', redirect_url='https://...',
                        customer={'email': 'ada@example.com'})
    flw.transactions.verify(123456)
    flw.transfers.create(account_bank='044', account_number='0690000040', amount=500, currency='NGN',
                         reference='wd-123', narration='Payout')
    for transfer in flw.paginate('transfers'):                 # walk every page
        ...

Every resource method returns the ``data`` part of the response. Use
``flw.request(..., raw=True)`` when you also need ``meta`` or ``message``.

Safety rules baked in:

* only GET requests are retried (connection errors, 429, 5xx) - a POST that
  timed out may still have moved money, so it is reconciled, never resent;
* errors carry ``is_definitive`` (Flutterwave said no) vs unknown outcome;
* the secret key never appears in logs or exceptions.
"""
import base64
import hashlib
import hmac
import logging
import random
import time

import requests
from requests.adapters import HTTPAdapter

from flutterwave_wallet import __version__
from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.exceptions import (
    FlutterwaveAPIError,
    FlutterwaveConfigurationError,
    InvalidFlutterwaveResponse,
    InvalidWebhookSignature,
)
from flutterwave_wallet.flutterwave import resources

logger = logging.getLogger('flutterwave_wallet.client')

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def clean_params(params):
    """Drop None values so optional arguments are not sent."""
    return {key: value for key, value in (params or {}).items() if value is not None}


# ----------------------------------------------------------------------
# Webhook signatures
# ----------------------------------------------------------------------

def compute_signature(payload, secret_hash=None):
    """``flutterwave-signature`` value: base64(HMAC-SHA256(raw body, secret hash))."""
    key = (secret_hash if secret_hash is not None else wallet_settings.FLUTTERWAVE_SECRET_HASH or '').encode('utf-8')
    return base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode('ascii')


def verify_hmac_signature(payload, signature, secret_hash=None):
    secret = secret_hash if secret_hash is not None else wallet_settings.FLUTTERWAVE_SECRET_HASH
    if not signature or not secret or payload is None:
        return False
    return hmac.compare_digest(compute_signature(payload, secret), str(signature).strip())


def verify_hash_header(value, secret_hash=None):
    """Constant-time check of the ``verif-hash`` header against your secret hash."""
    secret = secret_hash if secret_hash is not None else wallet_settings.FLUTTERWAVE_SECRET_HASH
    if not value or not secret:
        return False
    return hmac.compare_digest(str(value).strip().encode('utf-8'), str(secret).encode('utf-8'))


def verify_webhook(payload, headers, secret_hash=None, mode=None):
    """
    Return True if a webhook request is authentic. ``headers`` is a case-insensitive
    mapping (e.g. ``request.headers``). ``mode``: 'auto', 'hmac' or 'hash'.
    """
    mode = (mode or wallet_settings.WEBHOOK_SIGNATURE_MODE or 'auto').lower()
    signature = headers.get('flutterwave-signature')
    verif_hash = headers.get('verif-hash')
    if mode in ('auto', 'hmac') and signature and verify_hmac_signature(payload, signature, secret_hash):
        return True
    if mode in ('auto', 'hash') and verif_hash and verify_hash_header(verif_hash, secret_hash):
        return True
    return False


class FlutterwaveClient:
    """Thin, well-behaved wrapper around the Flutterwave v3 REST API."""

    def __init__(self, secret_key=None, base_url=None, timeout=None, max_retries=None, session=None):
        self._secret_key = secret_key
        self._base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=100)
            session.mount('https://', adapter)
            session.mount('http://', adapter)
        self.session = session

        # Collections
        self.payments = resources.Payments(self)
        self.charges = resources.Charges(self)
        self.preauth = resources.Preauthorization(self)
        self.tokenized_charges = resources.TokenizedCharges(self)
        self.transactions = resources.Transactions(self)
        self.refunds = resources.Refunds(self)
        self.virtual_accounts = resources.VirtualAccounts(self)
        self.payment_plans = resources.PaymentPlans(self)
        self.subscriptions = resources.Subscriptions(self)
        self.subaccounts = resources.Subaccounts(self)
        self.settlements = resources.Settlements(self)
        self.chargebacks = resources.Chargebacks(self)
        # Payouts
        self.transfers = resources.Transfers(self)
        self.beneficiaries = resources.Beneficiaries(self)
        self.payout_subaccounts = resources.PayoutSubaccounts(self)
        # Value-added services
        self.bills = resources.Bills(self)
        self.remita = resources.Remita(self)
        self.fx = resources.FX(self)
        self.otps = resources.OTPs(self)
        # Reference data, KYC, balances
        self.banks = resources.Banks(self)
        self.verification = resources.Verification(self)
        self.balances = resources.Balances(self)

    # ------------------------------------------------------------------
    # Configuration (resolved lazily so settings overrides are honoured)
    # ------------------------------------------------------------------

    @property
    def secret_key(self):
        return self._secret_key or wallet_settings.FLUTTERWAVE_SECRET_KEY

    @property
    def public_key(self):
        return wallet_settings.FLUTTERWAVE_PUBLIC_KEY

    @property
    def base_url(self):
        return (self._base_url or wallet_settings.FLUTTERWAVE_API_URL).rstrip('/')

    @property
    def timeout(self):
        return self._timeout or wallet_settings.FLUTTERWAVE_TIMEOUT

    @property
    def max_retries(self):
        return self._max_retries if self._max_retries is not None else wallet_settings.FLUTTERWAVE_MAX_RETRIES

    @property
    def is_test_mode(self):
        return '_TEST' in str(self.secret_key or '').upper()

    def __repr__(self):
        return f"<FlutterwaveClient base_url={self.base_url!r} test_mode={self.is_test_mode}>"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _headers(self, json_body=True):
        if not self.secret_key:
            raise FlutterwaveConfigurationError()
        headers = {
            'Authorization': f'Bearer {self.secret_key}',
            'Accept': 'application/json',
            'User-Agent': f'django-flutterwave-wallet/{__version__}',
        }
        if json_body:
            headers['Content-Type'] = 'application/json'
        return headers

    @staticmethod
    def _backoff(attempt):
        # Exponential backoff with jitter, capped at 4s
        return min(0.5 * (2 ** (attempt - 1)), 4) * (0.5 + random.random() / 2)

    def request(self, method, path, params=None, json=None, raw=False, files=None):
        """
        Call ``{base_url}/{path}`` and return ``data`` (or the whole body when ``raw``).

        GET requests are retried on connection errors and 429/5xx. Other methods are
        never retried: a timed-out POST may still have succeeded and must be reconciled.
        """
        method = method.upper()
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._headers(json_body=files is None)
        attempts = 1 + (max(int(self.max_retries), 0) if method == 'GET' else 0)
        params = clean_params(params) if params else None
        if isinstance(json, dict):
            json = clean_params(json)

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, headers=headers, params=params, json=None if files else json,
                    files=files, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                logger.warning("Flutterwave %s %s failed (attempt %s/%s): %s", method, path, attempt, attempts,
                               type(exc).__name__)
                if attempt < attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise FlutterwaveAPIError(message=f"Could not reach Flutterwave ({type(exc).__name__})") from exc

            logger.debug("Flutterwave %s %s -> %s in %.0fms", method, path, response.status_code,
                         (time.monotonic() - started) * 1000)
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                logger.warning("Flutterwave %s %s returned %s, retrying", method, path, response.status_code)
                time.sleep(self._backoff(attempt))
                continue
            return self._handle_response(response, method, path, raw)
        raise AssertionError('unreachable')  # pragma: no cover

    def _handle_response(self, response, method, path, raw):
        if response.status_code == 204 or not response.content:
            if response.status_code >= 400:
                raise FlutterwaveAPIError(message=f"HTTP {response.status_code}", status_code=response.status_code)
            return {'status': 'success', 'data': {}} if raw else {}

        try:
            body = response.json()
        except ValueError:
            logger.error("Flutterwave %s %s returned non-JSON (%s)", method, path, response.status_code)
            raise InvalidFlutterwaveResponse(message=f"non-JSON response (HTTP {response.status_code})",
                                             status_code=response.status_code)
        if not isinstance(body, dict):
            raise InvalidFlutterwaveResponse(message="unexpected response shape", status_code=response.status_code)

        ok = str(body.get('status', '')).lower() == 'success'
        if response.status_code >= 400 or not ok:
            message = body.get('message') or 'Unknown Flutterwave error'
            # A 2xx with status=error is a rejection of the request
            status_code = response.status_code if response.status_code >= 400 else 400
            logger.info("Flutterwave %s %s rejected (%s): %s", method, path, response.status_code, message)
            raise FlutterwaveAPIError(message=message, status_code=status_code, response=body)

        return body if raw else body.get('data')

    def get(self, path, **params):
        return self.request('GET', path, params=params)

    def post(self, path, data=None, **params):
        return self.request('POST', path, params=params or None, json=data or {})

    def put(self, path, data=None):
        return self.request('PUT', path, json=data or {})

    def delete(self, path, data=None):
        return self.request('DELETE', path, json=data)

    def paginate(self, path, params=None, max_pages=None):
        """Yield every item of a paginated list endpoint (``meta.page_info``)."""
        page = 1
        params = dict(params or {})
        while True:
            body = self.request('GET', path, params={**params, 'page': page}, raw=True)
            items = body.get('data') or []
            yield from items
            info = (body.get('meta') or {}).get('page_info') or {}
            total_pages = int(info.get('total_pages') or 0)
            if not items or page >= total_pages or (max_pages and page >= max_pages):
                return
            page += 1

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    @staticmethod
    def verify_webhook(payload, headers, secret_hash=None):
        """Raise InvalidWebhookSignature unless the request is authentic."""
        if not verify_webhook(payload, headers, secret_hash):
            raise InvalidWebhookSignature()
        return True


_default_client = None


def get_flutterwave_client():
    """Shared client instance (thread-safe: settings are resolved lazily, sessions pool connections)."""
    global _default_client
    if _default_client is None:
        _default_client = FlutterwaveClient()
    return _default_client
