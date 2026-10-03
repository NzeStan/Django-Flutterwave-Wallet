from django.utils.translation import gettext_lazy as _

# ==========================================
# CURRENCIES SUPPORTED BY FLUTTERWAVE
# ==========================================

SUPPORTED_CURRENCIES = (
    ('NGN', _('Nigerian Naira')),
    ('USD', _('US Dollar')),
    ('EUR', _('Euro')),
    ('GBP', _('British Pound')),
    ('GHS', _('Ghanaian Cedi')),
    ('KES', _('Kenyan Shilling')),
    ('UGX', _('Ugandan Shilling')),
    ('TZS', _('Tanzanian Shilling')),
    ('RWF', _('Rwandan Franc')),
    ('ZAR', _('South African Rand')),
    ('ZMW', _('Zambian Kwacha')),
    ('MWK', _('Malawian Kwacha')),
    ('XAF', _('Central African CFA Franc')),
    ('XOF', _('West African CFA Franc')),
    ('EGP', _('Egyptian Pound')),
    ('SLL', _('Sierra Leonean Leone')),
    ('MAD', _('Moroccan Dirham')),
    ('ETB', _('Ethiopian Birr')),
    ('CAD', _('Canadian Dollar')),
)
CURRENCY_CODES = {code for code, _label in SUPPORTED_CURRENCIES}

# Currencies Flutterwave settles without minor units
ZERO_DECIMAL_CURRENCIES = {'UGX', 'RWF', 'XAF', 'XOF', 'MWK'}

# ==========================================
# TRANSACTIONS (ledger entries)
# ==========================================

TRANSACTION_TYPE_DEPOSIT = 'deposit'
TRANSACTION_TYPE_WITHDRAWAL = 'withdrawal'
TRANSACTION_TYPE_TRANSFER = 'transfer'
TRANSACTION_TYPE_PAYMENT = 'payment'
TRANSACTION_TYPE_BILL_PAYMENT = 'bill_payment'
TRANSACTION_TYPE_REFUND = 'refund'
TRANSACTION_TYPE_REVERSAL = 'reversal'
TRANSACTION_TYPE_FEE = 'fee'
TRANSACTION_TYPE_ADJUSTMENT = 'adjustment'

TRANSACTION_TYPES = (
    (TRANSACTION_TYPE_DEPOSIT, _('Deposit')),
    (TRANSACTION_TYPE_WITHDRAWAL, _('Withdrawal')),
    (TRANSACTION_TYPE_TRANSFER, _('Transfer')),
    (TRANSACTION_TYPE_PAYMENT, _('Payment')),
    (TRANSACTION_TYPE_BILL_PAYMENT, _('Bill payment')),
    (TRANSACTION_TYPE_REFUND, _('Refund')),
    (TRANSACTION_TYPE_REVERSAL, _('Reversal')),
    (TRANSACTION_TYPE_FEE, _('Fee')),
    (TRANSACTION_TYPE_ADJUSTMENT, _('Adjustment')),
)

# Outgoing types that count towards the daily limit
OUTGOING_TYPES = (
    TRANSACTION_TYPE_WITHDRAWAL,
    TRANSACTION_TYPE_TRANSFER,
    TRANSACTION_TYPE_PAYMENT,
    TRANSACTION_TYPE_BILL_PAYMENT,
)

TRANSACTION_STATUS_PENDING = 'pending'
TRANSACTION_STATUS_PROCESSING = 'processing'
TRANSACTION_STATUS_SUCCESS = 'success'
TRANSACTION_STATUS_FAILED = 'failed'
TRANSACTION_STATUS_CANCELLED = 'cancelled'
TRANSACTION_STATUS_REVERSED = 'reversed'

TRANSACTION_STATUSES = (
    (TRANSACTION_STATUS_PENDING, _('Pending')),
    (TRANSACTION_STATUS_PROCESSING, _('Processing')),
    (TRANSACTION_STATUS_SUCCESS, _('Success')),
    (TRANSACTION_STATUS_FAILED, _('Failed')),
    (TRANSACTION_STATUS_CANCELLED, _('Cancelled')),
    (TRANSACTION_STATUS_REVERSED, _('Reversed')),
)

ACTIVE_STATUSES = (TRANSACTION_STATUS_PENDING, TRANSACTION_STATUS_PROCESSING)
FINAL_TRANSACTION_STATUSES = (
    TRANSACTION_STATUS_SUCCESS,
    TRANSACTION_STATUS_FAILED,
    TRANSACTION_STATUS_CANCELLED,
    TRANSACTION_STATUS_REVERSED,
)

DIRECTION_CREDIT = 'credit'
DIRECTION_DEBIT = 'debit'

TRANSACTION_DIRECTIONS = (
    (DIRECTION_CREDIT, _('Credit')),
    (DIRECTION_DEBIT, _('Debit')),
)

# ==========================================
# PAYMENT METHODS (Flutterwave `payment_type`)
# ==========================================

PAYMENT_METHOD_CARD = 'card'
PAYMENT_METHOD_BANK_TRANSFER = 'bank_transfer'
PAYMENT_METHOD_ACCOUNT = 'account'
PAYMENT_METHOD_USSD = 'ussd'
PAYMENT_METHOD_MOBILE_MONEY = 'mobile_money'
PAYMENT_METHOD_MPESA = 'mpesa'
PAYMENT_METHOD_QR = 'qr'
PAYMENT_METHOD_VOUCHER = 'voucher'
PAYMENT_METHOD_OPAY = 'opay'
PAYMENT_METHOD_ENAIRA = 'enaira'
PAYMENT_METHOD_APPLE_PAY = 'applepay'
PAYMENT_METHOD_GOOGLE_PAY = 'googlepay'
PAYMENT_METHOD_ACH = 'ach'
PAYMENT_METHOD_VIRTUAL_ACCOUNT = 'virtual_account'
PAYMENT_METHOD_WALLET = 'wallet'
PAYMENT_METHOD_OTHER = 'other'

PAYMENT_METHODS = (
    (PAYMENT_METHOD_CARD, _('Card')),
    (PAYMENT_METHOD_BANK_TRANSFER, _('Bank transfer')),
    (PAYMENT_METHOD_ACCOUNT, _('Bank account debit')),
    (PAYMENT_METHOD_USSD, _('USSD')),
    (PAYMENT_METHOD_MOBILE_MONEY, _('Mobile money')),
    (PAYMENT_METHOD_MPESA, _('M-Pesa')),
    (PAYMENT_METHOD_QR, _('QR')),
    (PAYMENT_METHOD_VOUCHER, _('Voucher')),
    (PAYMENT_METHOD_OPAY, _('OPay')),
    (PAYMENT_METHOD_ENAIRA, _('eNaira')),
    (PAYMENT_METHOD_APPLE_PAY, _('Apple Pay')),
    (PAYMENT_METHOD_GOOGLE_PAY, _('Google Pay')),
    (PAYMENT_METHOD_ACH, _('ACH')),
    (PAYMENT_METHOD_VIRTUAL_ACCOUNT, _('Virtual account')),
    (PAYMENT_METHOD_WALLET, _('Wallet')),
    (PAYMENT_METHOD_OTHER, _('Other')),
)


def payment_method_for(payment_type):
    """Map Flutterwave's ``payment_type`` to one of our payment methods."""
    value = (payment_type or '').lower().replace('-', '_')
    if not value:
        return None
    if value.startswith('mobilemoney') or value.startswith('mobile_money'):
        return PAYMENT_METHOD_MOBILE_MONEY
    mapping = {
        'card': PAYMENT_METHOD_CARD,
        'bank_transfer': PAYMENT_METHOD_BANK_TRANSFER,
        'banktransfer': PAYMENT_METHOD_BANK_TRANSFER,
        'account': PAYMENT_METHOD_ACCOUNT,
        'ussd': PAYMENT_METHOD_USSD,
        'mpesa': PAYMENT_METHOD_MPESA,
        'qr': PAYMENT_METHOD_QR,
        'nqr': PAYMENT_METHOD_QR,
        'voucher': PAYMENT_METHOD_VOUCHER,
        'voucher_payment': PAYMENT_METHOD_VOUCHER,
        'opay': PAYMENT_METHOD_OPAY,
        'enaira': PAYMENT_METHOD_ENAIRA,
        'applepay': PAYMENT_METHOD_APPLE_PAY,
        'googlepay': PAYMENT_METHOD_GOOGLE_PAY,
        'ach_payment': PAYMENT_METHOD_ACH,
        'ach': PAYMENT_METHOD_ACH,
    }
    return mapping.get(value, PAYMENT_METHOD_OTHER)


# Direct charges the wallet supports (v3: POST /charges?type=<type>). Card-based methods (card,
# Apple Pay, Google Pay) are deliberately absent: cards are only ever paid on Flutterwave's hosted
# checkout, so card data never touches your servers (PCI-DSS SAQ A scope).
CHARGE_TYPES = (
    'bank_transfer', 'ach_payment', 'account', 'debit_uk_account', 'mpesa',
    'mobile_money_ghana', 'mobile_money_rwanda', 'mobile_money_uganda', 'mobile_money_franco',
    'mobile_money_tanzania', 'mobile_money_zambia', 'voucher_payment', 'enaira', 'ussd', 'qr', 'opay',
    'capitec', 'fawry_pay',
)
CARD_CHARGE_TYPES = ('card', 'applepay', 'googlepay')

# Flutterwave Standard `payment_options` values
CHECKOUT_PAYMENT_OPTIONS = (
    'card', 'account', 'banktransfer', 'ussd', 'mpesa', 'mobilemoneyghana', 'mobilemoneyfranco',
    'mobilemoneyuganda', 'mobilemoneyrwanda', 'mobilemoneyzambia', 'mobilemoneytanzania', 'barter', 'nqr',
    'credit', 'opay', 'applepay', 'googlepay', 'enaira', 'paypal', 'ach',
)

# ==========================================
# FEES
# ==========================================

FEE_BEARER_CUSTOMER = 'customer'   # the payer pays the fee on top of the amount
FEE_BEARER_MERCHANT = 'merchant'   # the receiver absorbs the fee
FEE_BEARER_PLATFORM = 'platform'   # the platform absorbs the fee; nobody is charged
FEE_BEARER_SPLIT = 'split'         # shared between payer and receiver

FEE_BEARERS = (
    (FEE_BEARER_CUSTOMER, _('Customer (payer)')),
    (FEE_BEARER_MERCHANT, _('Merchant (receiver)')),
    (FEE_BEARER_PLATFORM, _('Platform')),
    (FEE_BEARER_SPLIT, _('Split')),
)
VALID_BEARERS = {choice for choice, _label in FEE_BEARERS}

# ==========================================
# PAYOUT BENEFICIARIES
# ==========================================

BENEFICIARY_TYPE_BANK = 'bank'
BENEFICIARY_TYPE_MOBILE_MONEY = 'mobile_money'

BENEFICIARY_TYPES = (
    (BENEFICIARY_TYPE_BANK, _('Bank account')),
    (BENEFICIARY_TYPE_MOBILE_MONEY, _('Mobile money')),
)

# ==========================================
# VIRTUAL ACCOUNTS
# ==========================================

VIRTUAL_ACCOUNT_STATIC = 'static'
VIRTUAL_ACCOUNT_DYNAMIC = 'dynamic'

VIRTUAL_ACCOUNT_KINDS = (
    (VIRTUAL_ACCOUNT_STATIC, _('Static (permanent)')),
    (VIRTUAL_ACCOUNT_DYNAMIC, _('Dynamic (one deposit)')),
)

# ==========================================
# KYC
# ==========================================

KYC_STATUS_NONE = 'none'
KYC_STATUS_PENDING = 'pending'
KYC_STATUS_VERIFIED = 'verified'
KYC_STATUS_FAILED = 'failed'

KYC_STATUSES = (
    (KYC_STATUS_NONE, _('Not started')),
    (KYC_STATUS_PENDING, _('Pending')),
    (KYC_STATUS_VERIFIED, _('Verified')),
    (KYC_STATUS_FAILED, _('Failed')),
)

# ==========================================
# FLUTTERWAVE WEBHOOK EVENTS
# ==========================================

WEBHOOK_EVENT_CHARGE_COMPLETED = 'charge.completed'
WEBHOOK_EVENT_TRANSFER_COMPLETED = 'transfer.completed'      # v3
WEBHOOK_EVENT_TRANSFER_DISBURSE = 'transfer.disburse'        # v4
WEBHOOK_EVENT_TRANSFER_REVERSAL = 'transfer.reversal'        # v4
WEBHOOK_EVENT_ORDER_AUTHORIZATION = 'order.authorization'    # v4
WEBHOOK_EVENT_SUBSCRIPTION_CANCELLED = 'subscription.cancelled'
WEBHOOK_EVENT_BVN_COMPLETED = 'bvn.completed'
WEBHOOK_EVENT_BILL_PAYMENT = 'singlebillpayment.status'
WEBHOOK_EVENT_REFUND_COMPLETED = 'refund.completed'
WEBHOOK_EVENT_PAYMENT_LINK = 'paymentlink.completed'
WEBHOOK_EVENT_CARD_TRANSACTION = 'CARD_TRANSACTION'

WEBHOOK_EVENTS = (
    (WEBHOOK_EVENT_CHARGE_COMPLETED, _('Charge completed')),
    (WEBHOOK_EVENT_TRANSFER_COMPLETED, _('Transfer completed')),
    (WEBHOOK_EVENT_TRANSFER_DISBURSE, _('Transfer disbursed (v4)')),
    (WEBHOOK_EVENT_TRANSFER_REVERSAL, _('Transfer reversed (v4)')),
    (WEBHOOK_EVENT_ORDER_AUTHORIZATION, _('Order authorized (v4)')),
    (WEBHOOK_EVENT_SUBSCRIPTION_CANCELLED, _('Subscription cancelled')),
    (WEBHOOK_EVENT_BVN_COMPLETED, _('BVN verification completed')),
    (WEBHOOK_EVENT_BILL_PAYMENT, _('Bill payment status')),
    (WEBHOOK_EVENT_REFUND_COMPLETED, _('Refund completed')),
    (WEBHOOK_EVENT_PAYMENT_LINK, _('Payment link completed')),
    (WEBHOOK_EVENT_CARD_TRANSACTION, _('Virtual card transaction')),
)

# ==========================================
# INTERNAL TRANSFER RECIPIENT LOOKUP
# ==========================================

LOOKUP_ID = 'id'
LOOKUP_TAG = 'tag'
LOOKUP_PHONE = 'phone_number'
LOOKUP_EMAIL = 'email'

RECIPIENT_LOOKUP_TYPES = (
    (LOOKUP_ID, _('Wallet ID')),
    (LOOKUP_TAG, _('Tag')),
    (LOOKUP_PHONE, _('Phone number')),
    (LOOKUP_EMAIL, _('Email')),
)
