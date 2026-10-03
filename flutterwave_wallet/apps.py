import logging

from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate, post_save
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger('flutterwave_wallet')


class FlutterwaveWalletConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'flutterwave_wallet'
    label = 'flutterwave_wallet'
    verbose_name = _("Flutterwave Wallet")

    def ready(self):
        from flutterwave_wallet import checks  # noqa: F401  (registers system checks)
        from flutterwave_wallet import notifications
        from flutterwave_wallet.signals.handlers import create_wallet_for_user

        post_save.connect(create_wallet_for_user, sender=settings.AUTH_USER_MODEL,
                          dispatch_uid='flutterwave_wallet.create_wallet_for_user')
        notifications.connect()
        post_migrate.connect(sync_banks_on_first_migrate, sender=self, dispatch_uid='flutterwave_wallet.sync_banks')


def sync_banks_on_first_migrate(sender, **kwargs):
    """Populate banks after the first migrate when ``FLW_WALLET_AUTO_SYNC_BANKS`` is on."""
    from flutterwave_wallet.conf import wallet_settings
    from flutterwave_wallet.models import Bank

    if not wallet_settings.AUTO_SYNC_BANKS or not wallet_settings.FLUTTERWAVE_SECRET_KEY:
        return
    try:
        if Bank.objects.exists():
            return
        if wallet_settings.USE_CELERY:
            from flutterwave_wallet.tasks import sync_banks_task
            sync_banks_task.delay()
        else:
            from flutterwave_wallet.services.bank_account_service import BankAccountService
            BankAccountService().sync_banks()
    except Exception as exc:
        logger.warning("Could not auto-sync banks: %s", exc)
