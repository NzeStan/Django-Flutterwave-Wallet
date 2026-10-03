"""
Identity verification (Nigeria BVN, consent flow).

    url = service.start_bvn_verification(user, bvn, 'Ada', 'Obi', redirect_url=...)['url']
    # send the customer to `url`; Flutterwave sends `bvn.completed` when done

The BVN is passed to Flutterwave and never stored. Only a safe subset of the result
(names, verification status) is kept on the user's :class:`WalletProfile`.
"""
import logging

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.constants import (
    KYC_STATUS_FAILED,
    KYC_STATUS_PENDING,
    KYC_STATUS_VERIFIED,
)
from flutterwave_wallet.exceptions import KYCError
from flutterwave_wallet.models import WalletProfile
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.signals import kyc_completed, send_on_commit

logger = logging.getLogger('flutterwave_wallet')

SAFE_KYC_FIELDS = ('first_name', 'middle_name', 'last_name', 'status', 'nationality', 'enrollment_bank')


class KYCService(BaseService):

    def start_bvn_verification(self, user, bvn, firstname, lastname, redirect_url=None):
        self.require_feature('KYC')
        bvn = str(bvn or '').strip()
        if not bvn.isdigit() or len(bvn) != 11:
            raise KYCError(_("A BVN is 11 digits"))
        data = self.flutterwave.verification.bvn_consent(bvn=bvn, firstname=firstname, lastname=lastname,
                                                         redirect_url=redirect_url) or {}
        profile, _created = WalletProfile.objects.get_or_create(user=user)
        profile.bvn_reference = data.get('reference')
        profile.kyc_status = KYC_STATUS_PENDING
        profile.save(update_fields=['bvn_reference', 'kyc_status', 'updated_at'])
        self.audit('kyc.bvn_started', user, reference=profile.bvn_reference)
        return {'url': data.get('url'), 'reference': data.get('reference')}

    def refresh_bvn_verification(self, profile):
        """Fetch the verification result for a pending profile and record it."""
        if not profile.bvn_reference:
            raise KYCError(_("No BVN verification in progress"))
        data = self.flutterwave.verification.bvn_details(profile.bvn_reference) or {}
        return self._record(profile, data)

    def handle_bvn_webhook(self, data, webhook_event=None):
        reference = data.get('reference') or data.get('bvn_reference')
        profile = WalletProfile.objects.filter(bvn_reference=reference).first() if reference else None
        if profile is None:
            return None
        return self.refresh_bvn_verification(profile)

    def _record(self, profile, data):
        status = str(data.get('status') or '').upper()
        if status == 'COMPLETED':
            profile.kyc_status = KYC_STATUS_VERIFIED
            profile.kyc_verified_at = timezone.now()
        elif status in ('FAILED', 'EXPIRED', 'REJECTED', 'CANCELLED'):
            profile.kyc_status = KYC_STATUS_FAILED
        else:
            return profile
        profile.kyc_data = {key: data.get(key) for key in SAFE_KYC_FIELDS if data.get(key) is not None}
        profile.save(update_fields=['kyc_status', 'kyc_verified_at', 'kyc_data', 'updated_at'])
        send_on_commit(kyc_completed, sender=WalletProfile, profile=profile, user=profile.user,
                       success=profile.kyc_status == KYC_STATUS_VERIFIED, data=profile.kyc_data)
        return profile
