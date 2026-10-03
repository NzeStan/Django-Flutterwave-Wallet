"""Built-in receivers, connected in ``FlutterwaveWalletConfig.ready()``."""
import logging

from django.db import transaction

from flutterwave_wallet.conf import wallet_settings

logger = logging.getLogger('flutterwave_wallet')


def create_wallet_for_user(sender, instance, created, raw=False, **kwargs):
    """Create the default wallet when a user is created (``FLW_WALLET_AUTO_CREATE_WALLET``)."""
    if not created or raw or not wallet_settings.AUTO_CREATE_WALLET:
        return
    user_pk = instance.pk

    def _create():
        try:
            if wallet_settings.USE_CELERY:
                from flutterwave_wallet.tasks import create_wallet_for_user_task
                create_wallet_for_user_task.delay(user_pk)
            else:
                from flutterwave_wallet.services.wallet_service import WalletService
                WalletService().get_wallet(instance)
        except Exception:
            logger.exception("Could not create wallet for user %s", user_pk)

    transaction.on_commit(_create)
