"""
Services: everything the wallet can do, independent of HTTP.

    from flutterwave_wallet.services import WalletService

Imports are lazy so that low-level modules (e.g. the ledger) can be imported
without pulling in every service.
"""
import importlib

_EXPORTS = {
    'WalletService': 'flutterwave_wallet.services.wallet_service',
    'DepositService': 'flutterwave_wallet.services.deposit_service',
    'WithdrawalService': 'flutterwave_wallet.services.withdrawal_service',
    'TransferService': 'flutterwave_wallet.services.transfer_service',
    'BillPaymentService': 'flutterwave_wallet.services.bill_service',
    'RefundService': 'flutterwave_wallet.services.refund_service',
    'CardService': 'flutterwave_wallet.services.card_service',
    'BankAccountService': 'flutterwave_wallet.services.bank_account_service',
    'VirtualAccountService': 'flutterwave_wallet.services.virtual_account_service',
    'KYCService': 'flutterwave_wallet.services.kyc_service',
    'WebhookService': 'flutterwave_wallet.services.webhook_service',
    'FeeService': 'flutterwave_wallet.services.fee_sweep_service',
    'ReconciliationService': 'flutterwave_wallet.services.reconciliation_service',
    'FeeCalculator': 'flutterwave_wallet.services.fee_service',
    'FeeCalculationResult': 'flutterwave_wallet.services.fee_service',
    'get_fee_calculator': 'flutterwave_wallet.services.fee_service',
    'calculate_fee': 'flutterwave_wallet.services.fee_service',
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        return getattr(importlib.import_module(_EXPORTS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
