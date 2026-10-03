"""
Gateways: one interface for the money operations, implemented on Flutterwave v3 and v4.

The services never talk to a specific API version directly for these operations. They ask a
gateway, which returns data in **one normalised shape** (the v3 field names the wallet logic
was built on), so every safety rule - verification, exactly-once settlement, debit-then-call,
reconciliation - applies identically on both versions.

    gateway = get_gateway('v4')
    charge = gateway.verify_charge('chg_abc')   # {'id', 'tx_ref', 'status': 'successful', 'amount', ...}

Which gateway is used:

* new direct charges, virtual accounts, withdrawals, bank lookups: ``FLW_WALLET_API_VERSION``;
* anything about an existing record: the version that created it (``Transaction.api_version``),
  or the id format (v3 ids are numeric, v4 ids look like ``chg_...``);
* hosted checkout, saved-card top-ups, bills and BVN: always v3 (v4 has no equivalent).
"""
import logging
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import BENEFICIARY_TYPE_MOBILE_MONEY, CARD_CHARGE_TYPES
from flutterwave_wallet.exceptions import CardDataNotAllowed, FlutterwaveAPIError
from flutterwave_wallet.utils.money import api_amount

logger = logging.getLogger('flutterwave_wallet')

V3 = 'v3'
V4 = 'v4'

# Direct-charge payment method types accepted on v4 (no card-based types, by design)
V4_CHARGE_TYPES = ('mobile_money', 'ussd', 'bank_account', 'bank_transfer', 'opay')


def version_for_id(flw_id):
    """v3 ids are numeric; v4 ids are prefixed strings such as ``chg_x1y2``."""
    return V3 if str(flw_id).strip().isdigit() else V4


def _sum_fees(fees, kind):
    total = Decimal('0')
    for fee in fees or []:
        if isinstance(fee, dict) and (kind is None or fee.get('type') == kind):
            total += Decimal(str(fee.get('amount') or 0))
    return total


def _split_name(full_name):
    parts = str(full_name or '').split()
    if not parts:
        return None
    return {'first': parts[0][:50], 'last': (parts[-1] if len(parts) > 1 else parts[0])[:50]}


class V3Gateway:
    version = V3

    def __init__(self, client):
        self.client = client

    # Charges ----------------------------------------------------------

    def verify_charge(self, charge_id):
        return self.client.transactions.verify(charge_id) or {}

    def verify_charge_by_reference(self, reference):
        return self.client.transactions.verify_by_reference(reference) or {}

    def direct_charge(self, txn, charge_type, payload, customer, meta):
        if str(charge_type).lower() in CARD_CHARGE_TYPES:
            raise CardDataNotAllowed()
        body = {
            **payload,
            'tx_ref': txn.reference,
            'amount': api_amount(Decimal(txn.metadata.get('charge_amount') or txn.amount.amount)),
            'currency': txn.wallet.currency,
            'email': payload.get('email') or customer.get('email'),
            'fullname': payload.get('fullname') or customer.get('name'),
            'phone_number': payload.get('phone_number') or customer.get('phonenumber'),
            'meta': meta,
        }
        return self.client.charges.create(charge_type, {k: v for k, v in body.items() if v is not None}, raw=True)

    def validate_charge(self, txn, otp):
        charge_type = (txn.metadata or {}).get('charge_type')
        return self.client.charges.validate(otp, txn.flw_ref, type='account' if charge_type == 'account' else None,
                                            raw=True)

    # Transfers --------------------------------------------------------

    def create_transfer(self, txn, account, amount, narration):
        return self.client.transfers.create(
            account_bank=account.account_bank, account_number=account.account_number, amount=api_amount(amount),
            currency=txn.wallet.currency, reference=txn.reference, narration=narration,
            debit_currency=txn.wallet.currency, beneficiary_name=account.account_name,
            destination_branch_code=account.branch_code or None,
            meta={'wallet_id': str(txn.wallet_id), 'transaction_id': str(txn.pk)},
        ) or {}

    def fetch_transfer(self, transfer_id):
        return self.client.transfers.fetch(transfer_id) or {}

    def find_transfer(self, txn):
        matches = [item for item in (self.client.transfers.list(reference=txn.reference) or [])
                   if isinstance(item, dict) and item.get('reference') == txn.reference]
        return matches[0] if matches else None

    def transfer_fee(self, amount, currency, transfer_type='account'):
        data = self.client.transfers.fee(amount=str(amount), currency=currency, type=transfer_type)
        fees = []
        for entry in data if isinstance(data, list) else [data or {}]:
            if not isinstance(entry, dict):
                continue
            if entry.get('fee_type') == 'percentage':
                fees.append(Decimal(str(amount)) * Decimal(str(entry.get('fee') or 0)) / 100)
            else:
                fees.append(Decimal(str(entry.get('fee') or 0)))
        return max(fees) if fees else Decimal('0')

    # Refunds ----------------------------------------------------------

    def create_refund(self, original, refund, amount, reason):
        return self.client.transactions.refund(original.flw_id, amount=api_amount(amount),
                                               comments=(reason or '')[:200] or None) or {}

    def fetch_refund(self, refund_id):
        return self.client.refunds.fetch(refund_id) or {}

    # Virtual accounts -------------------------------------------------

    def create_static_account(self, wallet, tx_ref, bvn=None, nin=None, narration=None, firstname=None,
                              lastname=None, phone_number=None):
        user = wallet.user
        data = self.client.virtual_accounts.create(
            email=user.email, currency=wallet.currency, tx_ref=tx_ref, is_permanent=True, bvn=bvn, nin=nin,
            firstname=firstname, lastname=lastname, phonenumber=phone_number, narration=narration,
        ) or {}
        return self._account(data)

    def create_dynamic_account(self, wallet, reference, amount, expiry, narration=None):
        data = self.client.virtual_accounts.create(
            email=wallet.user.email, currency=wallet.currency, tx_ref=reference, is_permanent=False,
            amount=api_amount(amount), expires=expiry, narration=narration,
        ) or {}
        return self._account(data)

    @staticmethod
    def _account(data):
        return {
            'account_number': str(data.get('account_number') or ''),
            'bank_name': str(data.get('bank_name') or '')[:255],
            'account_name': str(data.get('account_name') or data.get('note') or '')[:255],
            'order_ref': str(data.get('order_ref') or ''),
            'flw_ref': str(data.get('flw_ref') or ''),
            'raw': {k: v for k, v in data.items() if k not in ('bvn', 'nin')},
        }

    def deactivate_account(self, account):
        if account.order_ref:
            self.client.virtual_accounts.deactivate(account.order_ref)

    def reactivate_account(self, account):
        if account.order_ref:
            self.client.virtual_accounts.reactivate(account.order_ref)

    # Banks & balances -------------------------------------------------

    def list_banks(self, country):
        return [{'id': item.get('id'), 'code': item.get('code'), 'name': item.get('name')}
                for item in (self.client.banks.list(country) or [])]

    @staticmethod
    def can_resolve(currency, country):
        return country in ('NG', 'GH')

    def resolve_account(self, account_number, bank_code, currency):
        data = self.client.verification.resolve_account(account_number=str(account_number),
                                                        account_bank=str(bank_code)) or {}
        return {'account_number': data.get('account_number') or str(account_number),
                'account_name': data.get('account_name') or ''}

    def balances(self):
        return self.client.balances.all()


class V4Gateway:
    version = V4

    CHARGE_STATUS = {'succeeded': 'successful', 'failed': 'failed', 'voided': 'failed', 'cancelled': 'failed',
                     'pending': 'pending'}
    REFUND_STATUS = {'succeeded': 'completed', 'completed': 'completed', 'failed': 'failed', 'cancelled': 'failed'}

    def __init__(self, client):
        self.client = client

    # Customers --------------------------------------------------------

    def ensure_customer(self, user):
        """The user's v4 customer id, created once and stored on their WalletProfile."""
        from flutterwave_wallet.models import WalletProfile

        profile, _created = WalletProfile.objects.get_or_create(user=user)
        if profile.flw_customer_id:
            return profile.flw_customer_id
        email = getattr(user, 'email', None)
        if not email:
            raise ImproperlyConfigured("Flutterwave v4 needs an email address for every customer")
        name = {k: v for k, v in {'first': (getattr(user, 'first_name', '') or '')[:50] or None,
                                  'last': (getattr(user, 'last_name', '') or '')[:50] or None}.items() if v}
        try:
            data = self.client.customers.create(email=email, name=name or None,
                                                idempotency_key=f"flw-cus-{profile.pk}") or {}
            customer_id = data.get('id')
        except FlutterwaveAPIError as exc:
            if exc.status_code != 409:
                raise
            found = self.client.customers.search(email) or []
            customer_id = found[0].get('id') if found else None
        if not customer_id:
            raise FlutterwaveAPIError(message="Flutterwave did not return a customer id")
        try:
            type(profile).objects.filter(pk=profile.pk, flw_customer_id__isnull=True).update(
                flw_customer_id=customer_id)
        except IntegrityError:
            pass
        profile.refresh_from_db(fields=['flw_customer_id'])
        return profile.flw_customer_id

    # Normalisation ----------------------------------------------------

    def normalize_charge(self, charge):
        charge = charge or {}
        method = charge.get('payment_method_details') or charge.get('payment_method') or {}
        method = method if isinstance(method, dict) else {}
        customer = charge.get('customer')
        customer_id = charge.get('customer_id') or (customer if isinstance(customer, str) else
                                                    (customer or {}).get('id'))
        issuer = charge.get('issuer_response') if isinstance(charge.get('issuer_response'), dict) else {}
        return {
            'id': charge.get('id'),
            'tx_ref': charge.get('reference'),
            'flw_ref': charge.get('id'),
            'status': self.CHARGE_STATUS.get(str(charge.get('status') or '').lower(),
                                             str(charge.get('status') or '').lower()),
            'amount': charge.get('amount'),
            'charged_amount': charge.get('amount'),
            'app_fee': str(_sum_fees(charge.get('fees'), 'app')),
            'currency': charge.get('currency'),
            'payment_type': method.get('type') or '',
            'customer_id': customer_id,
            'customer': {'id': customer_id, **((customer or {}) if isinstance(customer, dict) else {})},
            'processor_response': issuer.get('message') or issuer.get('type') or '',
            'virtual_account_id': charge.get('virtual_account_id'),
            'api_version': V4,
        }

    def _charge_response(self, charge, message=''):
        """A v4 charge as the v3-shaped raw response the deposit service understands."""
        data = self.normalize_charge(charge)
        next_action = charge.get('next_action') if isinstance(charge.get('next_action'), dict) else {}
        authorization = {}
        kind = next_action.get('type')
        if kind:
            detail = next_action.get(kind) if isinstance(next_action.get(kind), dict) else {}
            authorization = {'mode': kind}
            if kind == 'redirect_url':
                authorization['redirect'] = detail.get('url')
            elif kind == 'payment_instruction':
                authorization['note'] = detail.get('note')
            elif kind == 'requires_bank_transfer':
                authorization.update({
                    'transfer_account': detail.get('account_number'),
                    'transfer_bank': detail.get('bank_name') or detail.get('account_bank_name'),
                    'transfer_amount': detail.get('amount'),
                    'account_expiration': detail.get('account_expiration_datetime') or detail.get('expiry'),
                    'transfer_note': detail.get('note'),
                })
            elif kind == 'requires_additional_fields':
                authorization['fields'] = detail.get('fields')
            elif kind == 'qr_code':
                authorization['note'] = detail.get('image') or detail.get('url')
        return {'status': 'success', 'message': message, 'data': data,
                'meta': {'authorization': authorization} if authorization else {}}

    # Charges ----------------------------------------------------------

    def verify_charge(self, charge_id):
        return self.normalize_charge(self.client.charges.get(charge_id))

    def verify_charge_by_reference(self, reference):
        found = [c for c in (self.client.charges.list(reference=reference) or [])
                 if isinstance(c, dict) and c.get('reference') == reference]
        if not found:
            raise FlutterwaveAPIError(message="No charge was found for this reference", status_code=404)
        return self.normalize_charge(found[0])

    def direct_charge(self, txn, charge_type, payload, customer, meta):
        if str(charge_type).lower() in CARD_CHARGE_TYPES or charge_type not in V4_CHARGE_TYPES:
            raise CardDataNotAllowed() if str(charge_type).lower() in CARD_CHARGE_TYPES else \
                ValueError(f"Unsupported v4 payment method {charge_type!r}")
        payload = dict(payload or {})
        authorization = payload.pop('authorization', None)
        redirect_url = payload.pop('redirect_url', None)
        customer_payload = {'email': customer.get('email')}
        name = _split_name(customer.get('name'))
        if name:
            customer_payload['name'] = name
        body = self.client.orchestration.direct_charge(
            reference=txn.reference,
            amount=api_amount(Decimal(txn.metadata.get('charge_amount') or txn.amount.amount)),
            currency=txn.wallet.currency, customer=customer_payload,
            payment_method={'type': charge_type, charge_type: payload},
            redirect_url=redirect_url, meta={k: str(v) for k, v in (meta or {}).items()},
            authorization=authorization, idempotency_key=f"flw-chg-{txn.reference}",
        )
        return self._charge_response(body or {})

    def validate_charge(self, txn, otp):
        return self._charge_response(self.client.charges.authorize_otp(txn.flw_ref or txn.flw_id, otp) or {})

    # Transfers --------------------------------------------------------

    @staticmethod
    def normalize_transfer(transfer):
        transfer = transfer or {}
        status = str(transfer.get('status') or '').upper()
        if status in ('INITIATED',):
            status = 'PENDING'
        if status in ('CANCELLED',):
            status = 'FAILED'
        return {'id': transfer.get('id'), 'reference': transfer.get('reference'), 'status': status,
                'complete_message': transfer.get('message') or transfer.get('narration') or '',
                'fee': transfer.get('fee'), 'api_version': V4}

    def _transfer_request(self, txn, account, amount, narration):
        currency = txn.wallet.currency
        if account.beneficiary_type == BENEFICIARY_TYPE_MOBILE_MONEY:
            transfer_type = 'mobile_money'
            recipient = {'mobile_money': {k: v for k, v in {
                'network': account.account_bank, 'msisdn': account.account_number,
                'country': account.country or None}.items() if v}}
            name = _split_name(account.account_name)
            if name:
                recipient['name'] = name
        else:
            transfer_type = 'bank'
            recipient = {'bank': {'code': account.account_bank, 'account_number': account.account_number}}
            if account.branch_code:
                recipient['bank']['branch'] = account.branch_code
            if currency != 'NGN':
                name = _split_name(account.account_name)
                if name:
                    recipient['name'] = name
        return dict(
            reference=txn.reference, type=transfer_type, narration=narration[:180],
            payment_instruction={
                'source_currency': currency, 'destination_currency': currency,
                'amount': {'value': api_amount(amount), 'applies_to': 'destination_currency'},
                'recipient': recipient,
            },
            meta={'wallet_id': str(txn.wallet_id), 'transaction_id': str(txn.pk)},
            idempotency_key=f"flw-trf-{txn.reference}",
        )

    def create_transfer(self, txn, account, amount, narration):
        return self.normalize_transfer(self.client.transfers.direct(**self._transfer_request(
            txn, account, amount, narration)))

    def fetch_transfer(self, transfer_id):
        return self.normalize_transfer(self.client.transfers.get(transfer_id))

    def find_transfer(self, txn):
        """
        v4 cannot search transfers by reference, but POSTs are idempotent: re-sending the original
        request with the same idempotency key returns the original transfer if it was created (and
        Flutterwave rejects a second transfer with the same reference anyway). If it was never
        created, this creates it now - which is what the debited user asked for.
        """
        account = txn.bank_account
        if account is None:
            return None
        payout = Decimal(str((txn.metadata or {}).get('payout_amount') or txn.amount.amount))
        try:
            return self.create_transfer(txn, account, payout, txn.description or 'Wallet withdrawal')
        except FlutterwaveAPIError as exc:
            if exc.is_definitive:
                raise
            return None

    def transfer_fee(self, amount, currency, transfer_type='account'):
        raise ImproperlyConfigured("Flutterwave v4 has no transfer fee quote endpoint; use a percentage/flat "
                                   "withdrawal fee rule instead of \"source\": \"flutterwave\"")

    # Refunds ----------------------------------------------------------

    def normalize_refund(self, refund):
        refund = refund or {}
        return {'id': refund.get('id'), 'status': self.REFUND_STATUS.get(str(refund.get('status') or '').lower(),
                                                                         'pending'),
                'amount_refunded': refund.get('amount_refunded'), 'comments': refund.get('reason') or '',
                'api_version': V4}

    def create_refund(self, original, refund, amount, reason):
        return self.normalize_refund(self.client.refunds.create(
            charge_id=original.flw_id, amount=api_amount(amount), reason='requested_by_customer',
            meta={'refund_reference': refund.reference, 'note': (reason or '')[:200]},
            idempotency_key=f"flw-rfd-{refund.reference}",
        ))

    def fetch_refund(self, refund_id):
        return self.normalize_refund(self.client.refunds.get(refund_id))

    # Virtual accounts -------------------------------------------------

    def create_static_account(self, wallet, tx_ref, bvn=None, nin=None, narration=None, firstname=None,
                              lastname=None, phone_number=None):
        data = self.client.virtual_accounts.create(
            customer_id=self.ensure_customer(wallet.user), reference=tx_ref, currency=wallet.currency,
            account_type='static', amount=0, bvn=bvn, nin=nin, narration=narration,
        ) or {}
        return self._account(data)

    def create_dynamic_account(self, wallet, reference, amount, expiry, narration=None):
        data = self.client.virtual_accounts.create(
            customer_id=self.ensure_customer(wallet.user), reference=reference, currency=wallet.currency,
            account_type='dynamic', amount=api_amount(amount), expiry=int(expiry), narration=narration,
        ) or {}
        return self._account(data)

    @staticmethod
    def _account(data):
        return {
            'account_number': str(data.get('account_number') or ''),
            'bank_name': str(data.get('account_bank_name') or '')[:255],
            'account_name': str(data.get('note') or '')[:255],
            'order_ref': str(data.get('id') or ''),
            'flw_ref': '',
            'raw': {k: v for k, v in data.items() if k not in ('bvn', 'nin')},
        }

    def deactivate_account(self, account):
        if account.order_ref:
            self.client.virtual_accounts.deactivate(account.order_ref)

    def reactivate_account(self, account):
        raise ImproperlyConfigured("Flutterwave v4 does not support reactivating a virtual account")

    # Banks & balances -------------------------------------------------

    def list_banks(self, country):
        return [{'id': item.get('id'), 'code': item.get('code'), 'name': item.get('name')}
                for item in (self.client.banks.list(country) or [])]

    @staticmethod
    def can_resolve(currency, country):
        return currency in ('NGN', 'GHS', 'UGX')

    def resolve_account(self, account_number, bank_code, currency):
        data = self.client.banks.resolve_account(currency=currency, code=str(bank_code),
                                                 number=str(account_number)) or {}
        return {'account_number': data.get('account_number') or str(account_number),
                'account_name': data.get('account_name') or ''}

    def balances(self):
        return self.client.wallets.balances()


def get_gateway(version=None, v3_client=None, v4_client=None):
    version = (version or wallet_settings.API_VERSION or V3).lower()
    if version == V4:
        if v4_client is None:
            from flutterwave_wallet.flutterwave.v4 import get_flutterwave_v4_client
            v4_client = get_flutterwave_v4_client()
        return V4Gateway(v4_client)
    if v3_client is None:
        from flutterwave_wallet.flutterwave import get_flutterwave_client
        v3_client = get_flutterwave_client()
    return V3Gateway(v3_client)
