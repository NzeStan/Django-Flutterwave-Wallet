import pytest

from flutterwave_wallet.exceptions import (
    CurrencyMismatchError,
    FeatureDisabled,
    InsufficientFunds,
    InvalidPin,
    InvalidTransactionState,
    PinLocked,
    PinNotSet,
    RecipientError,
    RecipientNotFound,
    WalletLocked,
)
from flutterwave_wallet.models import PlatformFee, Transaction, Wallet
from flutterwave_wallet.services.fee_sweep_service import FeeService
from tests.conftest import fund, money

pytestmark = pytest.mark.django_db


def test_transfer_by_tag_phone_email_and_id(funded_wallet, other_wallet, other_user, service):
    service.set_phone_number(other_user, '0803 123 4567')
    for recipient in ('@bola', 'bola', '08031234567', 'bola@example.com', str(other_wallet.pk)):
        service.transfer(funded_wallet, recipient, '100')
    other_wallet.refresh_from_db()
    funded_wallet.refresh_from_db()
    assert other_wallet.balance == money(500)
    assert funded_wallet.balance == money(99500)
    assert service.verify_ledger(funded_wallet) == [] and service.verify_ledger(other_wallet) == []


def test_transfer_writes_two_linked_legs(funded_wallet, other_wallet, service):
    debit = service.transfer(funded_wallet, other_wallet, '250', description='Lunch')
    credit = debit.related_transaction
    assert debit.direction == 'debit' and credit.direction == 'credit'
    assert credit.related_transaction_id == debit.pk and credit.wallet_id == other_wallet.pk
    assert credit.reference == f"{debit.reference}-CR"


def test_transfer_guards(funded_wallet, other_wallet, user, service, settings):
    with pytest.raises(RecipientError):
        service.transfer(funded_wallet, funded_wallet, '10')
    with pytest.raises(RecipientNotFound):
        service.transfer(funded_wallet, '@nobody', '10')
    with pytest.raises(InsufficientFunds):
        service.transfer(funded_wallet, other_wallet, '100001')
    usd = service.get_wallet(user, 'USD')
    with pytest.raises(CurrencyMismatchError):
        service.transfer(funded_wallet, str(service.get_wallet(other_wallet.user, 'USD').pk), '10')
    with pytest.raises(RecipientError):
        service.transfer(funded_wallet, usd, '10')
    settings.FLW_WALLET_TRANSFER_RECIPIENT_LOOKUP_FIELDS = ['id']
    with pytest.raises(RecipientNotFound):
        service.transfer(funded_wallet, '@bola', '10')
    with pytest.raises(RecipientError):
        service.transfer(funded_wallet, 'bola', '10', lookup='tag')
    settings.FLW_WALLET_ENABLE_INTERNAL_TRANSFERS = False
    with pytest.raises(FeatureDisabled):
        service.transfer(funded_wallet, other_wallet, '10')


def test_transfer_to_locked_wallet_refused(funded_wallet, other_wallet, service):
    service.lock_wallet_account(other_wallet, 'review')
    with pytest.raises(WalletLocked):
        service.transfer(funded_wallet, other_wallet, '10')


def test_recipient_without_wallet_in_currency_gets_one(user, other_wallet, other_user, service):
    usd = fund(service.get_wallet(user, 'USD'), 100)
    service.transfer(usd, '@bola', '40')
    assert Wallet.objects.get(user=other_user, balance_currency='USD').balance == money(40, 'USD')


def test_describe_recipient_masks_details(other_wallet, other_user, service):
    service.set_phone_number(other_user, '08031234567')
    info = service.describe_recipient(other_wallet)
    assert info['name'] == 'Bola A.' and info['phone_number'] == '+234803****567'


def test_transfer_fee_customer_bearer(funded_wallet, other_wallet, service, settings):
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'transfer': {'percentage': '1', 'cap': '20', 'bearer': 'customer'}}
    debit = service.transfer(funded_wallet, other_wallet, '5000')
    assert debit.total_amount == money(5020) and debit.fees == money(20)
    other_wallet.refresh_from_db()
    assert other_wallet.balance == money(5000)
    assert PlatformFee.objects.get(transaction=debit).amount == money(20)


def test_platform_payment(funded_wallet, service):
    txn = service.pay(funded_wallet, '1500', description='Order #1')
    assert txn.status == 'success' and txn.counterparty_wallet is None
    assert PlatformFee.objects.get(transaction=txn).amount == money(1500)


def test_marketplace_payment_and_escrow(funded_wallet, other_wallet, service):
    instant = service.pay(funded_wallet, '1000', merchant_wallet=other_wallet)
    assert instant.status == 'success' and instant.related_transaction.wallet_id == other_wallet.pk

    held = service.pay(funded_wallet, '2000', merchant_wallet='@bola', escrow=True)
    other_wallet.refresh_from_db()
    assert held.status == 'pending' and other_wallet.balance == money(1000)
    service.release_payment(held)
    other_wallet.refresh_from_db()
    assert other_wallet.balance == money(3000)
    with pytest.raises(InvalidTransactionState):
        service.release_payment(held)

    refunded = service.pay(funded_wallet, '3000', merchant_wallet=other_wallet, escrow=True)
    service.cancel_payment(refunded, reason='Out of stock')
    funded_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000 - 1000 - 2000)
    assert Transaction.objects.get(pk=refunded.pk).status == 'cancelled'


def test_escrow_needs_merchant(funded_wallet, service):
    from flutterwave_wallet.exceptions import WalletError
    with pytest.raises(WalletError):
        service.pay(funded_wallet, '10', escrow=True)


def test_reverse_transfer(funded_wallet, other_wallet, service):
    debit = service.transfer(funded_wallet, other_wallet, '700')
    reversal = service.reverse_transaction(debit, reason='Fraud', performed_by=None)
    funded_wallet.refresh_from_db()
    other_wallet.refresh_from_db()
    assert funded_wallet.balance == money(100000) and other_wallet.balance == money(0)
    assert reversal.direction == 'credit'
    assert Transaction.objects.get(pk=debit.pk).status == 'reversed'
    with pytest.raises(InvalidTransactionState):
        service.reverse_transaction(Transaction.objects.get(pk=debit.pk))


def test_reverse_fails_if_receiver_spent_it(funded_wallet, other_wallet, service, wallet):
    debit = service.transfer(funded_wallet, other_wallet, '700')
    service.transfer(other_wallet, wallet, '700')
    with pytest.raises(InsufficientFunds):
        service.reverse_transaction(debit)


def test_pin_lifecycle(user, service, settings):
    with pytest.raises(PinNotSet):
        service.verify_pin(user, '1357')
    for weak in ('1234', '12a4', '123'):
        with pytest.raises(InvalidPin):
            service.set_pin(user, weak)
    service.set_pin(user, '1357')
    assert service.verify_pin(user, '1357')
    with pytest.raises(InvalidPin):
        service.set_pin(user, '2468', current_pin='0000')
    service.set_pin(user, '2468', current_pin='1357')
    settings.FLW_WALLET_PIN_MAX_ATTEMPTS = 3
    for _ in range(2):
        with pytest.raises(InvalidPin):
            service.verify_pin(user, '9999')
    with pytest.raises(PinLocked):
        service.verify_pin(user, '9999')
    with pytest.raises(PinLocked):
        service.verify_pin(user, '2468')  # even the right PIN is refused while locked


def test_tag_and_phone_uniqueness(user, other_user, service):
    service.set_tag(user, '@Ada.Obi')
    assert user.wallet_profile.tag == 'ada.obi'
    with pytest.raises(RecipientError):
        service.set_tag(other_user, 'ada.obi')
    with pytest.raises(RecipientError):
        service.set_tag(user, 'a')
    service.set_phone_number(user, '+2348031234567')
    with pytest.raises(RecipientError):
        service.set_phone_number(other_user, '08031234567')


def test_fee_sweep_and_clawback(funded_wallet, other_wallet, staff_user, service, settings):
    fee_wallet = service.get_wallet(staff_user)
    settings.FLW_WALLET_FEE_COLLECTION_WALLET_ID = str(fee_wallet.pk)
    settings.FLW_WALLET_ENABLE_FEES = True
    settings.FLW_WALLET_FEE_RULES = {'transfer': {'flat': '10', 'bearer': 'customer'}}
    first = service.transfer(funded_wallet, other_wallet, '100')
    service.transfer(funded_wallet, other_wallet, '100')
    assert FeeService().sweep() == {'NGN': 20}
    fee_wallet.refresh_from_db()
    assert fee_wallet.balance == money(20)
    assert FeeService().sweep() == {}

    service.reverse_transaction(first)  # its fee was already swept: claw it back
    assert FeeService().sweep() == {'NGN': -10}
    fee_wallet.refresh_from_db()
    assert fee_wallet.balance == money(10)
    total = sum(Wallet.objects.get(pk=w.pk).balance.amount for w in (funded_wallet, other_wallet, fee_wallet))
    assert total == 100000  # money is never created or destroyed
    assert service.verify_ledger(fee_wallet) == []
