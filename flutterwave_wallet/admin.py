"""
Django admin.

Balances and ledger entries are read-only on purpose: editing money by hand silently
corrupts the books (and breaks the hash chain). Use the actions - which go through the
services, are audited and keep the ledger consistent.
"""
import csv

from django.contrib import admin, messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.exceptions import WalletError
from flutterwave_wallet.models import (
    Bank,
    BankAccount,
    Card,
    PlatformFee,
    Transaction,
    VirtualAccount,
    Wallet,
    WalletProfile,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)


def _link(obj, label=None):
    if obj is None:
        return '-'
    url = reverse(f'admin:{obj._meta.app_label}_{obj._meta.model_name}_change', args=[obj.pk])
    return format_html('<a href="{}">{}</a>', url, label or str(obj))


def _run(modeladmin, request, queryset, func, success_message):
    done = 0
    for obj in queryset:
        try:
            func(obj)
            done += 1
        except WalletError as exc:
            modeladmin.message_user(request, f"{obj}: {exc.message}", messages.ERROR)
        except Exception as exc:  # admin actions report, never crash
            modeladmin.message_user(request, f"{obj}: {type(exc).__name__}: {exc}", messages.ERROR)
    if done:
        modeladmin.message_user(request, success_message.format(count=done), messages.SUCCESS)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a cell)."""
    text = '' if value is None else str(value)
    return f"'{text}" if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


class CSVExportMixin:
    export_fields = ()

    @admin.action(description=_("Export selected to CSV"))
    def export_csv(self, request, queryset):
        fields = self.export_fields or [f.name for f in self.model._meta.fields]
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{self.model._meta.model_name}.csv"'
        writer = csv.writer(response)
        writer.writerow(fields)
        for obj in queryset.iterator():
            row = []
            for field in fields:
                value = obj
                for part in field.split('.'):
                    value = getattr(value, part, None) if value is not None else None
                row.append(_csv_safe(value))
            writer.writerow(row)
        return response


class NoDeleteMixin:
    def has_delete_permission(self, request, obj=None):
        return False


class TransactionInline(admin.TabularInline):
    model = Transaction
    fk_name = 'wallet'
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    fields = ('reference', 'transaction_type', 'direction', 'status', 'total_amount', 'balance_after', 'created_at')
    readonly_fields = fields
    ordering = ('-created_at',)

    def get_queryset(self, request):
        return super().get_queryset(request).order_by('-created_at')


@admin.register(Wallet)
class WalletAdmin(CSVExportMixin, NoDeleteMixin, admin.ModelAdmin):
    list_display = ('__str__', 'balance', 'is_active', 'is_locked', 'ledger_sequence', 'created_at')
    list_filter = ('is_active', 'is_locked', 'balance_currency')
    search_fields = ('user__email', 'user__username', 'id')
    readonly_fields = ('id', 'user', 'balance', 'ledger_sequence', 'ledger_head_hash', 'last_transaction_date',
                       'created_at', 'updated_at')
    fields = ('id', 'user', 'balance', 'is_active', 'is_locked', 'locked_reason', 'daily_limit', 'ledger_sequence',
              'ledger_head_hash', 'last_transaction_date', 'metadata', 'created_at', 'updated_at')
    list_select_related = ('user',)
    inlines = [TransactionInline]
    actions = ['lock_wallets', 'unlock_wallets', 'verify_ledgers', 'export_csv']
    export_fields = ('id', 'user.email', 'balance', 'is_active', 'is_locked', 'created_at')

    def has_add_permission(self, request):
        return False

    @admin.action(description=_("Lock selected wallets"))
    def lock_wallets(self, request, queryset):
        from flutterwave_wallet.services.wallet_service import WalletService
        service = WalletService()
        _run(self, request, queryset, lambda w: service.lock_wallet_account(w, 'Locked by admin', request.user),
             "Locked {count} wallet(s)")

    @admin.action(description=_("Unlock selected wallets"))
    def unlock_wallets(self, request, queryset):
        from flutterwave_wallet.services.wallet_service import WalletService
        service = WalletService()
        _run(self, request, queryset, lambda w: service.unlock_wallet_account(w, request.user),
             "Unlocked {count} wallet(s)")

    @admin.action(description=_("Verify ledger integrity"))
    def verify_ledgers(self, request, queryset):
        from flutterwave_wallet.services.ledger import verify_wallet_ledger
        bad = 0
        for wallet in queryset:
            problems = verify_wallet_ledger(wallet)
            if problems:
                bad += 1
                self.message_user(request, f"{wallet}: {'; '.join(problems[:5])}", messages.ERROR)
        if not bad:
            self.message_user(request, _("All selected ledgers are consistent"), messages.SUCCESS)


@admin.register(WalletProfile)
class WalletProfileAdmin(NoDeleteMixin, admin.ModelAdmin):
    list_display = ('user', 'tag', 'phone_number', 'kyc_status', 'has_pin', 'pin_locked_until')
    list_filter = ('kyc_status',)
    search_fields = ('tag', 'phone_number', 'user__email')
    readonly_fields = ('user', 'kyc_status', 'kyc_verified_at', 'kyc_data', 'bvn_reference', 'pin_locked_until',
                       'failed_pin_attempts', 'created_at', 'updated_at')
    exclude = ('pin_hash',)
    actions = ['reset_pins']

    @admin.display(boolean=True, description=_('PIN set'))
    def has_pin(self, obj):
        return obj.has_pin

    @admin.action(description=_("Reset transaction PIN"))
    def reset_pins(self, request, queryset):
        from flutterwave_wallet.services.base import BaseService

        def reset(profile):
            profile.clear_pin()
            BaseService.audit('pin.reset', request.user, user=profile.user_id)
        _run(self, request, queryset, reset, "Reset {count} PIN(s)")


@admin.register(Transaction)
class TransactionAdmin(CSVExportMixin, NoDeleteMixin, admin.ModelAdmin):
    list_display = ('reference', 'wallet_link', 'transaction_type', 'direction', 'status', 'amount', 'fees',
                    'total_amount', 'needs_review', 'created_at')
    list_filter = ('transaction_type', 'status', 'direction', 'needs_review', 'payment_method', 'amount_currency')
    search_fields = ('reference', 'flw_ref', 'flw_id', 'wallet__user__email', 'description')
    date_hierarchy = 'created_at'
    list_select_related = ('wallet', 'wallet__user')
    readonly_fields = [f.name for f in Transaction._meta.fields]
    actions = ['requery', 'refund_deposits', 'reverse_transactions', 'release_escrow', 'clear_review', 'export_csv']
    export_fields = ('reference', 'wallet.user.email', 'transaction_type', 'direction', 'status', 'amount', 'fees',
                     'total_amount', 'balance_after', 'flw_ref', 'created_at', 'completed_at')

    def has_add_permission(self, request):
        return False

    @admin.display(description=_('Wallet'))
    def wallet_link(self, obj):
        return _link(obj.wallet)

    @admin.action(description=_("Re-check with Flutterwave"))
    def requery(self, request, queryset):
        from flutterwave_wallet.services.wallet_service import WalletService
        service = WalletService()
        handlers = {'deposit': service.reconcile_deposit, 'withdrawal': service.verify_withdrawal,
                    'bill_payment': service.verify_bill_payment, 'refund': service.verify_refund}
        _run(self, request, queryset.filter(status__in=('pending', 'processing')),
             lambda t: handlers.get(t.transaction_type, lambda x: x)(t), "Re-checked {count} transaction(s)")

    @admin.action(description=_("Refund deposits to the payer"))
    def refund_deposits(self, request, queryset):
        from flutterwave_wallet.services.wallet_service import WalletService
        service = WalletService()
        _run(self, request, queryset, lambda t: service.refund_deposit(t, reason='Refunded by admin',
                                                                       performed_by=request.user),
             "Started {count} refund(s)")

    @admin.action(description=_("Reverse transfers/payments"))
    def reverse_transactions(self, request, queryset):
        from flutterwave_wallet.services.wallet_service import WalletService
        service = WalletService()
        _run(self, request, queryset, lambda t: service.reverse_transaction(t, 'Reversed by admin', request.user),
             "Reversed {count} transaction(s)")

    @admin.action(description=_("Release escrow payments"))
    def release_escrow(self, request, queryset):
        from flutterwave_wallet.services.wallet_service import WalletService
        service = WalletService()
        _run(self, request, queryset, lambda t: service.release_payment(t, performed_by=request.user),
             "Released {count} payment(s)")

    @admin.action(description=_("Mark reviewed"))
    def clear_review(self, request, queryset):
        from flutterwave_wallet.services.base import BaseService
        for txn in queryset.filter(needs_review=True):
            BaseService.audit('transaction.reviewed', request.user, transaction=txn.reference)
        count = queryset.filter(needs_review=True).update(needs_review=False)
        self.message_user(request, f"Marked {count} transaction(s) as reviewed", messages.SUCCESS)


@admin.register(PlatformFee)
class PlatformFeeAdmin(CSVExportMixin, NoDeleteMixin, admin.ModelAdmin):
    list_display = ('transaction', 'operation', 'amount', 'bearer', 'swept', 'voided', 'created_at')
    list_filter = ('operation', 'swept', 'voided', 'amount_currency')
    readonly_fields = [f.name for f in PlatformFee._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(Bank)
class BankAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'country', 'is_active')
    list_filter = ('country', 'is_active')
    search_fields = ('name', 'code')


@admin.register(BankAccount)
class BankAccountAdmin(NoDeleteMixin, admin.ModelAdmin):
    list_display = ('user', 'account_name', 'bank_name', 'masked_account_number', 'currency', 'beneficiary_type',
                    'is_verified', 'is_default', 'is_active')
    list_filter = ('beneficiary_type', 'is_verified', 'is_active', 'currency')
    search_fields = ('account_name', 'account_number', 'user__email')
    readonly_fields = ('user', 'account_bank', 'account_number', 'account_name', 'is_verified', 'flw_beneficiary_id',
                       'created_at', 'updated_at')


@admin.register(Card)
class CardAdmin(NoDeleteMixin, admin.ModelAdmin):
    list_display = ('user', 'masked', 'card_type', 'issuer', 'expiry', 'is_default', 'is_active')
    list_filter = ('card_type', 'is_active')
    search_fields = ('user__email', 'last_4digits')
    exclude = ('token',)
    readonly_fields = ('user', 'fingerprint', 'first_6digits', 'last_4digits', 'issuer', 'card_type', 'country',
                       'expiry', 'email', 'currency', 'last_used_at', 'created_at', 'updated_at')


@admin.register(VirtualAccount)
class VirtualAccountAdmin(NoDeleteMixin, admin.ModelAdmin):
    list_display = ('account_number', 'bank_name', 'wallet', 'kind', 'currency', 'is_active', 'expires_at')
    list_filter = ('kind', 'is_active', 'currency')
    search_fields = ('account_number', 'tx_ref', 'order_ref', 'wallet__user__email')
    readonly_fields = [f.name for f in VirtualAccount._meta.fields]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'reference', 'processed', 'processing_attempts', 'source_ip', 'created_at')
    list_filter = ('event_type', 'processed')
    search_fields = ('reference', 'flw_id', 'idempotency_key')
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]
    actions = ['reprocess']

    def has_add_permission(self, request):
        return False

    @admin.action(description=_("Reprocess selected events"))
    def reprocess(self, request, queryset):
        from flutterwave_wallet.services.webhook_service import WebhookService
        service = WebhookService()
        _run(self, request, queryset, service.process_event, "Reprocessed {count} event(s)")


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ('name', 'url', 'is_active', 'max_attempts')


@admin.register(WebhookDeliveryAttempt)
class WebhookDeliveryAttemptAdmin(admin.ModelAdmin):
    list_display = ('webhook_endpoint', 'webhook_event', 'attempt_number', 'response_code', 'is_success', 'created_at')
    list_filter = ('is_success',)
    readonly_fields = [f.name for f in WebhookDeliveryAttempt._meta.fields]

    def has_add_permission(self, request):
        return False
