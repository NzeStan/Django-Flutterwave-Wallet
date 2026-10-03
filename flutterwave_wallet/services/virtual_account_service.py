"""
Virtual account numbers: fund a wallet by plain bank transfer.

* :meth:`create_static_account` - a permanent account number per wallet (NGN needs
  the customer's BVN or NIN; it is sent to Flutterwave and never stored here).
* :meth:`create_bank_transfer_deposit` - a one-off account for a single deposit of
  a fixed amount, expiring after ``FLW_WALLET_DYNAMIC_ACCOUNT_EXPIRY_SECONDS``.

Transfers into either arrive as ``charge.completed`` webhooks and are credited by
the deposit service after verification.
"""
import logging
from datetime import timedelta

from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import (
    PAYMENT_METHOD_BANK_TRANSFER,
    VIRTUAL_ACCOUNT_DYNAMIC,
    VIRTUAL_ACCOUNT_STATIC,
)
from flutterwave_wallet.exceptions import FlutterwaveAPIError, VirtualAccountError
from flutterwave_wallet.models import VirtualAccount
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.services.deposit_service import DepositService
from flutterwave_wallet.services.fee_service import get_fee_calculator
from flutterwave_wallet.signals import deposit_initialized, send_on_commit, virtual_account_created
from flutterwave_wallet.utils.references import generate_reference

logger = logging.getLogger('flutterwave_wallet')

VIRTUAL_ACCOUNT_CURRENCIES = {'NGN', 'GHS'}


class VirtualAccountService(BaseService):

    def _check(self, wallet):
        self.require_feature('VIRTUAL_ACCOUNTS')
        wallet.check_active()
        if wallet.currency not in VIRTUAL_ACCOUNT_CURRENCIES:
            raise VirtualAccountError(_("Virtual accounts are available for NGN and GHS wallets"))

    @staticmethod
    def get_static_account(wallet):
        return VirtualAccount.objects.filter(wallet=wallet, kind=VIRTUAL_ACCOUNT_STATIC, is_active=True).first()

    def create_static_account(self, wallet, bvn=None, nin=None, narration=None, firstname=None, lastname=None,
                              phone_number=None):
        """Create (or return) the wallet's permanent virtual account."""
        self._check(wallet)
        existing = self.get_static_account(wallet)
        if existing is not None:
            return existing
        if wallet.currency == 'NGN' and not (bvn or nin):
            raise VirtualAccountError(_("A BVN or NIN is required for a permanent NGN account"))
        for label, value in (('BVN', bvn), ('NIN', nin)):
            if value and (not str(value).isdigit() or len(str(value)) != 11):
                raise VirtualAccountError(_("{label} must be 11 digits").format(label=label))

        if not self.claim(f'static-va:{wallet.pk}', 60):
            raise VirtualAccountError(_("An account is already being created for this wallet"))
        try:
            user = wallet.user
            tx_ref = generate_reference('VA')
            profile_phone = getattr(getattr(user, 'wallet_profile', None), 'phone_number', None)
            gateway = self.gateway()
            fields = gateway.create_static_account(
                wallet, tx_ref, bvn=bvn, nin=nin,
                narration=narration or wallet_settings.VIRTUAL_ACCOUNT_NARRATION or None,
                firstname=firstname or getattr(user, 'first_name', None) or None,
                lastname=lastname or getattr(user, 'last_name', None) or None,
                phone_number=phone_number or profile_phone,
            )
            if not fields['account_number']:
                raise VirtualAccountError(_("Flutterwave did not return an account number"))
            raw = fields.pop('raw')
            account = VirtualAccount.objects.create(
                wallet=wallet, kind=VIRTUAL_ACCOUNT_STATIC, currency=wallet.currency, tx_ref=tx_ref,
                api_version=gateway.version, flw_response=raw, **fields,
            )
        finally:
            self.release(f'static-va:{wallet.pk}')
        send_on_commit(virtual_account_created, sender=VirtualAccount, virtual_account=account, wallet=wallet)
        return account

    def create_bank_transfer_deposit(self, wallet, amount, reference=None, description=None, metadata=None,
                                     ip_address=None, user_agent=None):
        """
        A one-off account number for a single deposit. Returns the account details; the
        wallet is credited when the transfer arrives.
        """
        self._check(wallet)
        deposits = DepositService(**self.clients())
        amount = deposits._prepare(wallet, amount)
        reference = self.new_reference(reference, lambda: generate_reference('DVA'))
        fee = get_fee_calculator(wallet, self.flutterwave).calculate(amount, 'deposit')
        txn = deposits._create_pending_deposit(
            wallet, amount, reference, fee, description or str(_("Bank transfer deposit")), metadata,
            payment_method=PAYMENT_METHOD_BANK_TRANSFER, ip_address=ip_address, user_agent=user_agent,
            api_version=self.api_version,
        )
        expiry = int(wallet_settings.DYNAMIC_ACCOUNT_EXPIRY_SECONDS)
        gateway = self.gateway(txn.api_version)
        try:
            fields = gateway.create_dynamic_account(wallet, reference, fee.customer_pays.amount, expiry,
                                                    narration=wallet_settings.VIRTUAL_ACCOUNT_NARRATION or None)
        except FlutterwaveAPIError as exc:
            deposits._fail_initialization(txn, exc)
            raise
        raw = fields.pop('raw')
        with db_transaction.atomic():
            account = VirtualAccount.objects.create(
                wallet=wallet, kind=VIRTUAL_ACCOUNT_DYNAMIC, currency=wallet.currency, tx_ref=reference,
                amount=fee.customer_pays.amount, expires_at=timezone.now() + timedelta(seconds=expiry),
                transaction=txn, flw_response=raw, api_version=gateway.version, **fields,
            )
            txn.metadata = {**txn.metadata, 'virtual_account': fields['account_number'],
                            'bank_name': fields['bank_name']}
            txn.save(update_fields=['metadata', 'updated_at'])
        send_on_commit(deposit_initialized, sender=type(txn), transaction=txn, wallet=wallet, link=None)
        return {
            'reference': reference, 'transaction_id': str(txn.pk), 'account_number': account.account_number,
            'bank_name': account.bank_name, 'account_name': account.account_name,
            'amount': str(fee.customer_pays.amount), 'currency': wallet.currency,
            'expires_at': account.expires_at.isoformat(), 'fee_breakdown': fee.to_dict(),
        }

    def deactivate(self, account):
        self.gateway(account.api_version).deactivate_account(account)
        account.is_active = False
        account.save(update_fields=['is_active', 'updated_at'])
        return account

    def reactivate(self, account):
        self.gateway(account.api_version).reactivate_account(account)
        account.is_active = True
        account.save(update_fields=['is_active', 'updated_at'])
        return account
