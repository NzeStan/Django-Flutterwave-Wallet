"""
HTTP client for the Flutterwave **v4** API (OAuth 2.0).

    from flutterwave_wallet.flutterwave.v4 import FlutterwaveV4Client

    flw = FlutterwaveV4Client()          # FLUTTERWAVE_CLIENT_ID / FLUTTERWAVE_CLIENT_SECRET
    customer = flw.customers.create(email='ada@example.com', name={'first': 'Ada', 'last': 'Obi'})
    flw.transfers.direct(action='instant', reference='wd-0001', payment_instruction={...})

What it does for you:

* **Access tokens** (10-minute lifetime) are fetched with client credentials, cached in-process and
  in Django's cache (so every worker shares one token), refreshed a minute before expiry, and
  refreshed once more if Flutterwave answers 401.
* Every request carries ``X-Trace-Id``; every POST carries ``X-Idempotency-Key`` (yours, or one
  generated per call). Because Flutterwave replays the original response for a repeated key,
  **POSTs are safely retried** on connection errors, 429 and 5xx - unlike v3.
* ``X-Scenario-Key`` (sandbox simulations) is supported per call.
* Errors raise :class:`~flutterwave_wallet.exceptions.FlutterwaveAPIError` with ``is_definitive``.
* Card payment methods are refused: card data never reaches your server (use hosted checkout).
"""
import hashlib
import logging
import random
import threading
import time
import uuid

import requests
from django.core.cache import cache
from requests.adapters import HTTPAdapter

from flutterwave_wallet import __version__
from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.exceptions import (
    FlutterwaveAPIError,
    FlutterwaveConfigurationError,
    InvalidFlutterwaveResponse,
)
from flutterwave_wallet.flutterwave.v4 import resources

logger = logging.getLogger('flutterwave_wallet.client')

SANDBOX_URL = 'https://developersandbox-api.flutterwave.com'
PRODUCTION_URL = 'https://f4bexperience.flutterwave.com'
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_TOKEN_SAFETY_SECONDS = 60


def clean(params):
    return {key: value for key, value in (params or {}).items() if value is not None}


def new_trace_id():
    return f"flw-wallet-{uuid.uuid4().hex}"


class TokenManager:
    """Thread-safe OAuth token cache (process memory first, then Django's shared cache)."""

    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0

    @property
    def cache_key(self):
        digest = hashlib.sha256(f"{self.client.client_id}|{self.client.token_url}".encode()).hexdigest()[:32]
        return f"flw-wallet-v4-token:{digest}"

    def get(self):
        now = time.time()
        if self._token and now < self._expires_at - _TOKEN_SAFETY_SECONDS:
            return self._token
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - _TOKEN_SAFETY_SECONDS:
                return self._token
            cached = self._cache_get()
            if cached and now < cached['expires_at'] - _TOKEN_SAFETY_SECONDS:
                self._token, self._expires_at = cached['token'], cached['expires_at']
                return self._token
            token, expires_in = self._fetch()
            self._token, self._expires_at = token, time.time() + expires_in
            self._cache_set({'token': token, 'expires_at': self._expires_at},
                            max(int(expires_in) - _TOKEN_SAFETY_SECONDS, 1))
            return token

    def invalidate(self):
        with self._lock:
            self._token, self._expires_at = None, 0.0
            try:
                cache.delete(self.cache_key)
            except Exception:
                pass

    def _cache_get(self):
        try:
            return cache.get(self.cache_key)
        except Exception:  # a cache outage must not stop payments
            return None

    def _cache_set(self, value, timeout):
        try:
            cache.set(self.cache_key, value, timeout)
        except Exception:
            pass

    def _fetch(self):
        client = self.client
        if not client.client_id or not client.client_secret:
            raise FlutterwaveConfigurationError(
                "Flutterwave v4 is not configured. Set FLUTTERWAVE_CLIENT_ID and FLUTTERWAVE_CLIENT_SECRET.")
        try:
            response = client.session.post(
                client.token_url,
                data={'client_id': client.client_id, 'client_secret': client.client_secret,
                      'grant_type': 'client_credentials'},
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
                timeout=client.timeout,
            )
        except requests.RequestException as exc:
            raise FlutterwaveAPIError(message=f"Could not reach the Flutterwave identity server "
                                              f"({type(exc).__name__})") from exc
        try:
            body = response.json()
        except ValueError:
            raise InvalidFlutterwaveResponse(message="non-JSON token response", status_code=response.status_code)
        if response.status_code >= 400 or not body.get('access_token'):
            # Never echo the response: it can contain details about the credentials
            raise FlutterwaveAPIError(message="Flutterwave rejected the v4 client credentials",
                                      status_code=response.status_code if response.status_code >= 400 else 401)
        return body['access_token'], int(body.get('expires_in') or 600)


class FlutterwaveV4Client:
    """Wrapper around the Flutterwave v4 REST API."""

    def __init__(self, client_id=None, client_secret=None, base_url=None, token_url=None, timeout=None,
                 max_retries=None, session=None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url
        self._token_url = token_url
        self._timeout = timeout
        self._max_retries = max_retries
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=100)
            session.mount('https://', adapter)
            session.mount('http://', adapter)
        self.session = session
        self.tokens = TokenManager(self)

        self.customers = resources.Customers(self)
        self.payment_methods = resources.PaymentMethods(self)
        self.charges = resources.Charges(self)
        self.orchestration = resources.Orchestration(self)
        self.orders = resources.Orders(self)
        self.refunds = resources.Refunds(self)
        self.transfers = resources.Transfers(self)
        self.transfer_recipients = resources.TransferRecipients(self)
        self.transfer_senders = resources.TransferSenders(self)
        self.transfer_rates = resources.TransferRates(self)
        self.virtual_accounts = resources.VirtualAccounts(self)
        self.banks = resources.Banks(self)
        self.mobile_networks = resources.MobileNetworks(self)
        self.wallets = resources.Wallets(self)
        self.settlements = resources.Settlements(self)
        self.chargebacks = resources.Chargebacks(self)
        self.fees = resources.Fees(self)
        self.webhooks = resources.Webhooks(self)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def client_id(self):
        return self._client_id or wallet_settings.FLUTTERWAVE_CLIENT_ID

    @property
    def client_secret(self):
        return self._client_secret or wallet_settings.FLUTTERWAVE_CLIENT_SECRET

    @property
    def environment(self):
        return str(wallet_settings.FLUTTERWAVE_V4_ENVIRONMENT or 'sandbox').lower()

    @property
    def base_url(self):
        explicit = self._base_url or wallet_settings.FLUTTERWAVE_V4_API_URL
        if explicit:
            return explicit.rstrip('/')
        return PRODUCTION_URL if self.environment == 'production' else SANDBOX_URL

    @property
    def token_url(self):
        return self._token_url or wallet_settings.FLUTTERWAVE_V4_TOKEN_URL

    @property
    def timeout(self):
        return self._timeout or wallet_settings.FLUTTERWAVE_TIMEOUT

    @property
    def max_retries(self):
        return self._max_retries if self._max_retries is not None else wallet_settings.FLUTTERWAVE_MAX_RETRIES

    @property
    def is_test_mode(self):
        return self.base_url == SANDBOX_URL

    def __repr__(self):
        return f"<FlutterwaveV4Client base_url={self.base_url!r}>"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @staticmethod
    def _backoff(attempt):
        return min(0.5 * (2 ** (attempt - 1)), 4) * (0.5 + random.random() / 2)

    def request(self, method, path, params=None, json=None, raw=False, idempotency_key=None, scenario_key=None,
                trace_id=None):
        """
        Call ``{base_url}/{path}``; return ``data`` (or the whole body when ``raw``).

        GET is retried on connection errors, 429 and 5xx. POST is retried the same way because it
        always carries an idempotency key (Flutterwave replays the first response). PUT/DELETE
        are never retried.
        """
        method = method.upper()
        url = f"{self.base_url}/{path.lstrip('/')}"
        params = clean(params) if params else None
        if isinstance(json, dict):
            json = clean(json)
        if method == 'POST' and not idempotency_key:
            idempotency_key = f"flw-wallet-{uuid.uuid4().hex}"
        retryable = method in ('GET', 'POST')
        attempts = 1 + (max(int(self.max_retries), 0) if retryable else 0)
        refreshed = False
        trace_id = trace_id or new_trace_id()

        attempt = 0
        while True:
            attempt += 1
            headers = {
                'Authorization': f'Bearer {self.tokens.get()}',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': f'django-flutterwave-wallet/{__version__}',
                'X-Trace-Id': trace_id,
            }
            if idempotency_key:
                headers['X-Idempotency-Key'] = str(idempotency_key)
            if scenario_key:
                headers['X-Scenario-Key'] = str(scenario_key)
            try:
                response = self.session.request(method, url, headers=headers, params=params, json=json,
                                                timeout=self.timeout)
            except requests.RequestException as exc:
                logger.warning("Flutterwave v4 %s %s failed (attempt %s/%s, trace %s): %s", method, path, attempt,
                               attempts, trace_id, type(exc).__name__)
                if attempt < attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise FlutterwaveAPIError(message=f"Could not reach Flutterwave ({type(exc).__name__})") from exc

            if response.status_code == 401 and not refreshed:
                # The token was revoked or expired early: it was rejected, so resending is safe
                self.tokens.invalidate()
                refreshed = True
                attempt -= 1
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                logger.warning("Flutterwave v4 %s %s returned %s, retrying (trace %s)", method, path,
                               response.status_code, trace_id)
                time.sleep(self._backoff(attempt))
                continue
            return self._handle_response(response, method, path, raw, trace_id)

    @staticmethod
    def _handle_response(response, method, path, raw, trace_id):
        if response.status_code == 204 or not response.content:
            if response.status_code >= 400:
                raise FlutterwaveAPIError(message=f"HTTP {response.status_code}", status_code=response.status_code)
            return {'status': 'success', 'data': {}} if raw else {}
        try:
            body = response.json()
        except ValueError:
            logger.error("Flutterwave v4 %s %s returned non-JSON (%s, trace %s)", method, path,
                         response.status_code, trace_id)
            raise InvalidFlutterwaveResponse(message=f"non-JSON response (HTTP {response.status_code})",
                                             status_code=response.status_code)
        if not isinstance(body, dict):
            raise InvalidFlutterwaveResponse(message="unexpected response shape", status_code=response.status_code)

        ok = str(body.get('status', '')).lower() == 'success'
        if response.status_code >= 400 or not ok:
            error = body.get('error') if isinstance(body.get('error'), dict) else {}
            message = error.get('message') or body.get('message') or 'Unknown Flutterwave error'
            status_code = response.status_code if response.status_code >= 400 else 400
            logger.info("Flutterwave v4 %s %s rejected (%s, %s, trace %s): %s", method, path, response.status_code,
                        error.get('type') or '-', trace_id, message)
            raise FlutterwaveAPIError(message=message, status_code=status_code, response=body)
        if raw:
            body.setdefault('_idempotent_replay',
                            response.headers.get('X-Idempotency-Cache-Hit', '').lower() == 'true')
            return body
        return body.get('data')

    def get(self, path, **params):
        return self.request('GET', path, params=params)

    def post(self, path, data=None, idempotency_key=None, **params):
        return self.request('POST', path, params=params or None, json=data or {}, idempotency_key=idempotency_key)

    def put(self, path, data=None):
        return self.request('PUT', path, json=data or {})

    def delete(self, path):
        return self.request('DELETE', path)

    def paginate(self, path, params=None, size=50, max_pages=None):
        """Yield every item of a list endpoint (page-numbered or cursor-based)."""
        params = dict(params or {})
        page, cursor, pages = 1, None, 0
        while True:
            query = {**params, 'size': size}
            if cursor:
                query['next'] = cursor
            else:
                query['page'] = page
            body = self.request('GET', path, params=query, raw=True)
            items = body.get('data') or []
            yield from items
            pages += 1
            meta = body.get('meta') or {}
            info = meta.get('page_info') or {}
            next_cursor = (meta.get('cursor') or {}).get('next') if isinstance(meta.get('cursor'), dict) else None
            next_cursor = next_cursor or meta.get('next')
            if not items or (max_pages and pages >= max_pages):
                return
            if next_cursor:
                cursor = next_cursor
            elif info and page < int(info.get('total_pages') or 0):
                page += 1
            else:
                return


_default_client = None


def get_flutterwave_v4_client():
    global _default_client
    if _default_client is None:
        _default_client = FlutterwaveV4Client()
    return _default_client
