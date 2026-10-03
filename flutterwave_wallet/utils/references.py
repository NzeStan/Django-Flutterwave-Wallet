"""Reference and tag generators (cryptographically random, collision resistant)."""
import re
import secrets
import string
import time
import uuid

from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.exceptions import InvalidReference

_ALPHABET = string.ascii_uppercase + string.digits
# Flutterwave accepts letters, digits, '-' and '_' in tx_ref / transfer references
REFERENCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def random_string(length=10, alphabet=_ALPHABET):
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def generate_reference(prefix='FLW'):
    """``PREFIX-<unix time>-<16 random chars>``: sortable, unguessable, globally unique."""
    return f"{prefix}-{int(time.time())}-{random_string(16)}"


def validate_reference(reference):
    """Validate a caller-supplied reference (format only)."""
    if reference is None:
        return None
    reference = str(reference).strip()
    if not REFERENCE_RE.match(reference):
        raise InvalidReference(_("References are 8-80 characters: letters, digits, '-' or '_'"))
    return reference


def generate_wallet_tag(user=None):
    base = ''
    if user is not None:
        base = getattr(user, 'username', None) or (getattr(user, 'email', None) or '').split('@')[0]
    base = ''.join(c for c in str(base) if c.isalnum()).lower()[:12]
    if len(base) < 3:
        base = f"w{uuid.uuid4().hex[:6]}"
    return base
