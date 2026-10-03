"""
Cryptography helpers.

* :func:`encrypt_value` / :func:`decrypt_value` - authenticated encryption
  (Fernet: AES-128-CBC + HMAC-SHA256) for secrets stored in the database, such
  as card tokens. Keys come from ``FLW_WALLET_FIELD_ENCRYPTION_KEYS`` (rotate by
  prepending a new key) or are derived from Django's ``SECRET_KEY`` with HKDF.
* :func:`fingerprint` - keyed, non-reversible identifier (HMAC-SHA256) used to
  de-duplicate sensitive values without storing them.
"""
import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from flutterwave_wallet.conf import wallet_settings

ENCRYPTED_PREFIX = 'enc1:'


def _derived_key(secret):
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'flutterwave-wallet-field-encryption',
                info=b'fernet-key-v1')
    return base64.urlsafe_b64encode(hkdf.derive(secret.encode('utf-8')))


@lru_cache(maxsize=8)
def _fernet(keys, secret_key):
    if keys:
        try:
            return MultiFernet([Fernet(key) for key in keys])
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                "FLW_WALLET_FIELD_ENCRYPTION_KEYS must contain Fernet keys "
                "(generate one with cryptography.fernet.Fernet.generate_key())"
            ) from exc
    if not secret_key:
        raise ImproperlyConfigured("SECRET_KEY or FLW_WALLET_FIELD_ENCRYPTION_KEYS is required to encrypt data")
    return MultiFernet([Fernet(_derived_key(secret_key))])


def get_fernet():
    keys = tuple(str(k) for k in (wallet_settings.FIELD_ENCRYPTION_KEYS or []))
    return _fernet(keys, getattr(settings, 'SECRET_KEY', ''))


def encrypt_value(value):
    if value in (None, ''):
        return value
    token = get_fernet().encrypt(str(value).encode('utf-8')).decode('ascii')
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_value(value):
    if value in (None, '') or not str(value).startswith(ENCRYPTED_PREFIX):
        return value
    try:
        return get_fernet().decrypt(str(value)[len(ENCRYPTED_PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "Could not decrypt a stored value: the encryption key changed. Add the old key to "
            "FLW_WALLET_FIELD_ENCRYPTION_KEYS (after the new one) to keep reading old data."
        ) from exc


def fingerprint(*parts):
    """Keyed hash of ``parts`` - stable per installation, useless outside it."""
    key = hashlib.sha256(f"flw-wallet-fp:{getattr(settings, 'SECRET_KEY', '')}".encode()).digest()
    message = '\x1f'.join(str(p or '') for p in parts).encode('utf-8')
    return hmac.new(key, message, hashlib.sha256).hexdigest()

