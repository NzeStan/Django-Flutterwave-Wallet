from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import KYC_STATUS_NONE, KYC_STATUSES
from flutterwave_wallet.exceptions import InvalidPin, PinLocked, PinNotSet
from flutterwave_wallet.models.base import BaseModel


class WalletProfile(BaseModel):
    """
    Per-user wallet identity and security, shared by all of the user's wallets
    (one wallet per currency): the public tag and phone number others send money
    to, the transaction PIN and KYC state.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet_profile', verbose_name=_('User'),
    )
    tag = models.CharField(
        max_length=30, unique=True, blank=True, null=True, verbose_name=_('Tag'),
        help_text=_('Public handle other users can send money to'),
    )
    phone_number = models.CharField(
        max_length=20, unique=True, blank=True, null=True, verbose_name=_('Phone number'),
        help_text=_('E.164 phone number other users can send money to'),
    )

    pin_hash = models.CharField(max_length=128, blank=True, default='', editable=False)
    failed_pin_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    pin_locked_until = models.DateTimeField(null=True, blank=True, editable=False)

    flw_customer_id = models.CharField(max_length=64, blank=True, null=True, unique=True,
                                       verbose_name=_('Flutterwave customer id (v4)'))
    kyc_status = models.CharField(max_length=20, choices=KYC_STATUSES, default=KYC_STATUS_NONE, db_index=True,
                                  verbose_name=_('KYC status'))
    bvn_reference = models.CharField(max_length=100, blank=True, null=True, db_index=True,
                                     verbose_name=_('BVN verification reference'))
    kyc_verified_at = models.DateTimeField(null=True, blank=True, verbose_name=_('KYC verified at'))
    # Never the raw BVN: only what is safe to show (e.g. names returned by the verification)
    kyc_data = models.JSONField(default=dict, blank=True, verbose_name=_('KYC data'))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_('Metadata'))

    class Meta:
        verbose_name = _('Wallet profile')
        verbose_name_plural = _('Wallet profiles')

    def __str__(self):
        return f"@{self.tag}" if self.tag else str(self.user)

    @property
    def has_pin(self):
        return bool(self.pin_hash)

    @property
    def pin_is_locked(self):
        return bool(self.pin_locked_until and self.pin_locked_until > timezone.now())

    def set_pin(self, raw_pin):
        self.pin_hash = make_password(str(raw_pin))
        self.failed_pin_attempts = 0
        self.pin_locked_until = None
        self.save(update_fields=['pin_hash', 'failed_pin_attempts', 'pin_locked_until', 'updated_at'])

    def clear_pin(self):
        self.pin_hash = ''
        self.failed_pin_attempts = 0
        self.pin_locked_until = None
        self.save(update_fields=['pin_hash', 'failed_pin_attempts', 'pin_locked_until', 'updated_at'])

    def verify_pin(self, raw_pin):
        """
        Check the PIN, counting failures and locking out after too many. Raises on failure.

        The attempt counter is updated under a row lock, so parallel guesses cannot
        slip past the lockout.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if not locked.has_pin:
                raise PinNotSet()
            if locked.pin_is_locked:
                raise PinLocked()
            if raw_pin is not None and check_password(str(raw_pin), locked.pin_hash):
                if locked.failed_pin_attempts:
                    locked.failed_pin_attempts = 0
                    locked.pin_locked_until = None
                    locked.save(update_fields=['failed_pin_attempts', 'pin_locked_until', 'updated_at'])
                self.failed_pin_attempts, self.pin_locked_until = 0, None
                return True

            locked.failed_pin_attempts += 1
            lock_out = locked.failed_pin_attempts >= int(wallet_settings.PIN_MAX_ATTEMPTS)
            if lock_out:
                locked.pin_locked_until = timezone.now() + timedelta(minutes=int(wallet_settings.PIN_LOCKOUT_MINUTES))
                locked.failed_pin_attempts = 0
            locked.save(update_fields=['failed_pin_attempts', 'pin_locked_until', 'updated_at'])
            self.failed_pin_attempts, self.pin_locked_until = locked.failed_pin_attempts, locked.pin_locked_until
        # Raised outside the atomic block so the failed attempt is committed
        raise PinLocked() if lock_out else InvalidPin()
