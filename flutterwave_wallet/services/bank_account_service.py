"""Banks, account-name resolution and users' payout accounts (bank or mobile money)."""
import logging

from django.db import IntegrityError, transaction as db_transaction
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import BENEFICIARY_TYPE_BANK, BENEFICIARY_TYPE_MOBILE_MONEY, BENEFICIARY_TYPES
from flutterwave_wallet.exceptions import BankAccountError, FlutterwaveAPIError
from flutterwave_wallet.models import Bank, BankAccount
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.signals import bank_account_added, send_on_commit

logger = logging.getLogger('flutterwave_wallet')

CURRENCY_COUNTRY = {'NGN': 'NG', 'GHS': 'GH', 'KES': 'KE', 'UGX': 'UG', 'TZS': 'TZ', 'RWF': 'RW', 'ZAR': 'ZA',
                    'ZMW': 'ZM', 'MWK': 'MW', 'XAF': 'CM', 'XOF': 'SN', 'EGP': 'EG', 'SLL': 'SL'}


class BankAccountService(BaseService):

    # ------------------------------------------------------------------
    # Banks
    # ------------------------------------------------------------------

    def sync_banks(self, countries=None):
        """Fetch Flutterwave's bank lists into the Bank table. Returns ``(created, updated)``."""
        created = updated = 0
        for country in countries or wallet_settings.BANK_COUNTRIES or ['NG']:
            country = str(country).upper()
            for item in self.gateway().list_banks(country):
                code = str(item.get('code') or '').strip()
                if not code:
                    continue
                _bank, was_created = Bank.objects.update_or_create(
                    code=code, country=country,
                    defaults={'name': str(item.get('name') or code)[:255],
                              'flw_id': str(item['id']) if item.get('id') is not None else None,
                              'is_active': True},
                )
                created += was_created
                updated += not was_created
        return created, updated

    @staticmethod
    def list_banks(country=None):
        queryset = Bank.objects.active()
        return queryset.for_country(country) if country else queryset

    def resolve_account(self, account_number, account_bank, currency=None):
        """Return ``{'account_number', 'account_name'}`` from Flutterwave."""
        return self.gateway().resolve_account(account_number, account_bank,
                                              str(currency or wallet_settings.CURRENCY).upper())

    # ------------------------------------------------------------------
    # Payout accounts
    # ------------------------------------------------------------------

    def add_bank_account(self, user, account_bank, account_number, currency=None, account_name=None,
                         beneficiary_type=BENEFICIARY_TYPE_BANK, country=None, branch_code=None, make_default=None,
                         resolve=True, create_beneficiary=False, metadata=None):
        """
        Save a payout destination for ``user``.

        Bank accounts in Nigeria/Ghana are verified with Flutterwave and the *resolved* name
        is stored (a caller-supplied name is ignored there), so payouts can't be sent to an
        account that does not match. Mobile money accounts use the operator code as
        ``account_bank`` (e.g. ``MPS`` for M-Pesa) and the phone number as ``account_number``.
        """
        self.require_feature('WITHDRAWALS')
        if beneficiary_type not in dict(BENEFICIARY_TYPES):
            raise BankAccountError(_("Unknown payout account type"))
        account_bank = str(account_bank or '').strip()
        account_number = ''.join(str(account_number or '').split())
        if not account_bank or not account_number or not account_number.lstrip('+').isalnum():
            raise BankAccountError(_("Bank code and account number are required"))
        currency = str(currency or wallet_settings.CURRENCY).upper()
        country = str(country or CURRENCY_COUNTRY.get(currency, wallet_settings.COUNTRY)).upper()
        bank = Bank.objects.filter(code=account_bank, country=country).first()

        verified = False
        if resolve and beneficiary_type == BENEFICIARY_TYPE_BANK and self.gateway().can_resolve(currency, country):
            try:
                resolved = self.resolve_account(account_number, account_bank, currency)
            except FlutterwaveAPIError as exc:
                if exc.is_definitive:
                    raise BankAccountError(_("Could not verify this account: {reason}").format(
                        reason=exc.flutterwave_message or exc.message)) from exc
                raise
            account_name, verified = resolved['account_name'], bool(resolved['account_name'])
        if not account_name:
            raise BankAccountError(_("Account name is required"))

        with db_transaction.atomic():
            existing = BankAccount.objects.select_for_update().filter(
                user=user, account_bank=account_bank, account_number=account_number, currency=currency).first()
            has_default = BankAccount.objects.filter(user=user, is_default=True, is_active=True).exists()
            fields = {
                'beneficiary_type': beneficiary_type, 'bank': bank, 'bank_name': bank.name if bank else '',
                'account_name': str(account_name)[:255], 'country': country, 'branch_code': branch_code or '',
                'is_verified': verified, 'is_active': True, 'metadata': metadata or {},
            }
            if existing is not None:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.save()
                account = existing
            else:
                try:
                    with db_transaction.atomic():
                        account = BankAccount.objects.create(
                            user=user, account_bank=account_bank, account_number=account_number, currency=currency,
                            is_default=make_default if make_default is not None else not has_default, **fields)
                except IntegrityError:
                    account = BankAccount.objects.get(user=user, account_bank=account_bank,
                                                      account_number=account_number, currency=currency)
            if make_default:
                self.set_default(account)

        if create_beneficiary and not account.flw_beneficiary_id:
            self.create_beneficiary(account)
        send_on_commit(bank_account_added, sender=BankAccount, bank_account=account, user=user)
        return account

    def add_mobile_money_account(self, user, operator_code, phone_number, currency, account_name, **kwargs):
        return self.add_bank_account(user, operator_code, phone_number, currency=currency, account_name=account_name,
                                     beneficiary_type=BENEFICIARY_TYPE_MOBILE_MONEY, resolve=False, **kwargs)

    def create_beneficiary(self, account):
        """Register the account as a Flutterwave v3 transfer beneficiary (optional)."""
        data = self.flutterwave.beneficiaries.create(
            account_bank=account.account_bank, account_number=account.account_number,
            beneficiary_name=account.account_name, currency=account.currency, bank_name=account.bank_name or None,
        ) or {}
        if data.get('id'):
            account.flw_beneficiary_id = str(data['id'])
            account.save(update_fields=['flw_beneficiary_id', 'updated_at'])
        return account

    @staticmethod
    def set_default(account):
        with db_transaction.atomic():
            BankAccount.objects.filter(user_id=account.user_id, is_default=True).exclude(pk=account.pk).update(
                is_default=False)
            account.is_default = True
            account.save(update_fields=['is_default', 'updated_at'])
        return account

    def remove_bank_account(self, account):
        """Deactivate (history keeps pointing at it); also removes the Flutterwave beneficiary if any."""
        if account.flw_beneficiary_id and str(account.flw_beneficiary_id).isdigit():
            try:
                self.flutterwave.beneficiaries.delete(account.flw_beneficiary_id)
            except FlutterwaveAPIError as exc:
                logger.warning("Could not delete beneficiary %s: %s", account.flw_beneficiary_id, exc)
        account.is_active = False
        account.is_default = False
        account.save(update_fields=['is_active', 'is_default', 'updated_at'])
        return account
