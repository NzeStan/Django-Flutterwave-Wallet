"""Saved (tokenized) cards."""
import logging

from django.db import IntegrityError, transaction as db_transaction
from django.utils.translation import gettext_lazy as _

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.exceptions import CardError
from flutterwave_wallet.models import Card
from flutterwave_wallet.services.base import BaseService
from flutterwave_wallet.signals import card_saved, send_on_commit
from flutterwave_wallet.utils.crypto import fingerprint

logger = logging.getLogger('flutterwave_wallet')


class CardService(BaseService):

    def save_card_from_charge(self, user, data):
        """
        Save the reusable token from a *verified* card charge. Returns the Card or None.

        The token is tied to the email it was issued for, so that email is stored with it.
        """
        if not wallet_settings.SAVE_CARDS or not wallet_settings.ENABLE_CARDS:
            return None
        card = data.get('card') if isinstance(data.get('card'), dict) else {}
        token = card.get('token')
        customer = data.get('customer') if isinstance(data.get('customer'), dict) else {}
        email = customer.get('email') or getattr(user, 'email', '')
        if not token or not email:
            return None

        first6 = str(card.get('first_6digits') or '')[:6]
        last4 = str(card.get('last_4digits') or '')[-4:]
        expiry = str(card.get('expiry') or '')[:7]
        print_ = fingerprint(first6, last4, expiry, card.get('issuer'))
        defaults = {
            'token': token, 'first_6digits': first6, 'last_4digits': last4, 'expiry': expiry,
            'issuer': str(card.get('issuer') or '')[:255], 'card_type': str(card.get('type') or '')[:50],
            'country': str(card.get('country') or '')[:100], 'email': email, 'is_active': True,
            'currency': str(data.get('currency') or '')[:3],
        }
        try:
            with db_transaction.atomic():
                saved, created = Card.objects.update_or_create(user=user, fingerprint=print_, defaults=defaults)
                if created and not Card.objects.filter(user=user, is_default=True, is_active=True).exclude(
                        pk=saved.pk).exists():
                    saved.is_default = True
                    saved.save(update_fields=['is_default', 'updated_at'])
        except IntegrityError:  # saved concurrently by a duplicate webhook
            saved, created = Card.objects.get(user=user, fingerprint=print_), False
        send_on_commit(card_saved, sender=Card, card=saved, user=user, created=created)
        return saved

    @staticmethod
    def check_card(card):
        if not card.is_active:
            raise CardError(_("This card has been removed"))
        if card.is_expired:
            raise CardError(_("This card has expired"))
        return card

    @staticmethod
    def set_default_card(card):
        with db_transaction.atomic():
            Card.objects.filter(user_id=card.user_id, is_default=True).exclude(pk=card.pk).update(is_default=False)
            card.is_default = True
            card.save(update_fields=['is_default', 'updated_at'])
        return card

    @staticmethod
    def remove_card(card):
        """Deactivate a card and wipe its token (it can never be charged again)."""
        card.is_active = False
        card.is_default = False
        card.token = ''
        card.save(update_fields=['is_active', 'is_default', 'token', 'updated_at'])
        return card

    @staticmethod
    def remove_expired_cards():
        count = 0
        for card in Card.objects.active().iterator():
            if card.is_expired:
                CardService.remove_card(card)
                count += 1
        return count
