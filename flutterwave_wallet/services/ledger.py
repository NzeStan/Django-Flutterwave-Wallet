"""
The ledger: the only place wallet balances change.

Rules enforced here:

* the wallet row is locked (``SELECT ... FOR UPDATE``) for the whole posting;
* the balance change and its ledger entry are written in the same DB transaction;
* each posting takes the next ``ledger_sequence`` (unique per wallet, enforced by
  the database) and records ``balance_after``;
* each entry is chained to the previous one with a SHA-256 hash, so any later
  edit or deletion of a posted entry is detectable (:func:`verify_wallet_ledger`);
* the balance can never go negative (checked here *and* by a DB constraint).

Several wallets are always locked in primary-key order, so concurrent transfers
between the same wallets cannot deadlock.
"""
import hashlib
from decimal import Decimal

from django.db import transaction as db_transaction

from flutterwave_wallet.conf import wallet_settings
from flutterwave_wallet.constants import DIRECTION_CREDIT, DIRECTION_DEBIT
from flutterwave_wallet.models import Transaction, Wallet


def _require_atomic():
    if not db_transaction.get_connection().in_atomic_block:
        raise RuntimeError("Ledger operations must run inside transaction.atomic()")


def lock_wallet(wallet):
    _require_atomic()
    pk = wallet.pk if isinstance(wallet, Wallet) else wallet
    return Wallet.objects.select_for_update().get(pk=pk)


def lock_wallets(*wallets):
    """Lock several wallets in primary-key order (prevents deadlocks); returns them in the given order."""
    _require_atomic()
    pks = [w.pk if isinstance(w, Wallet) else w for w in wallets]
    locked = {w.pk: w for w in Wallet.objects.select_for_update().filter(pk__in=set(pks)).order_by('pk')}
    return [locked[pk] for pk in pks]


def lock_transaction(txn):
    _require_atomic()
    pk = txn.pk if isinstance(txn, Transaction) else txn
    return Transaction.objects.select_for_update().select_related('wallet').get(pk=pk)


def compute_entry_hash(txn, previous_hash):
    parts = [
        previous_hash or '',
        str(txn.wallet_id),
        str(txn.ledger_sequence),
        txn.reference,
        txn.transaction_type,
        txn.direction,
        f"{Decimal(txn.total_amount.amount):.2f}",
        str(txn.total_amount.currency),
        f"{Decimal(txn.balance_after.amount):.2f}",
    ]
    return hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()


def post(locked_wallet, txn, force=False, enforce_minimum_balance=True):
    """
    Apply ``txn`` (direction + ``total_amount``) to ``locked_wallet`` and save it as a posted entry.

    ``txn`` may be new or an existing pending row (e.g. a deposit being completed).
    ``force`` moves money on a locked/inactive wallet - only for money that has already
    arrived or is being returned (deposits, refunds, reversals). It never allows a
    negative balance. Entries with a zero total are saved without a ledger position.
    """
    _require_atomic()
    if txn.wallet_id != locked_wallet.pk:
        raise ValueError("Transaction does not belong to the locked wallet")
    if txn.ledger_sequence is not None:
        raise ValueError(f"Transaction {txn.reference} is already posted")

    amount = txn.total_amount
    if amount.amount <= 0:
        txn.save()
        return txn

    if txn.direction == DIRECTION_CREDIT:
        locked_wallet.apply_credit(amount, force=force)
    elif txn.direction == DIRECTION_DEBIT:
        locked_wallet.apply_debit(amount, enforce_minimum_balance=enforce_minimum_balance, force=force)
    else:  # pragma: no cover - guarded by model choices
        raise ValueError(f"Unknown direction {txn.direction!r}")

    txn.balance_after = locked_wallet.balance
    txn.ledger_sequence = locked_wallet.ledger_sequence
    txn.previous_hash = locked_wallet.ledger_head_hash or ''
    txn.entry_hash = compute_entry_hash(txn, txn.previous_hash) if wallet_settings.LEDGER_HASH_CHAIN else ''
    locked_wallet.ledger_head_hash = txn.entry_hash or locked_wallet.ledger_head_hash
    locked_wallet.save_balance()
    txn.save()
    return txn


def verify_wallet_ledger(wallet):
    """
    Recompute a wallet's ledger from its entries. Returns a list of problems (empty = healthy):

    * positions must be 1..N without gaps;
    * every ``balance_after`` must equal the running total;
    * every hash must chain to the previous entry (when the chain was recorded);
    * the final running total and hash must match the wallet row.
    """
    problems = []
    running = Decimal('0')
    previous_hash = ''
    expected_sequence = 0
    entries = (Transaction.objects.filter(wallet_id=wallet.pk, ledger_sequence__isnull=False)
               .order_by('ledger_sequence').iterator(chunk_size=2000))
    for entry in entries:
        expected_sequence += 1
        if entry.ledger_sequence != expected_sequence:
            problems.append(f"gap: expected position {expected_sequence}, found {entry.ledger_sequence} "
                            f"({entry.reference})")
            expected_sequence = entry.ledger_sequence
        delta = entry.total_amount.amount
        running += delta if entry.direction == DIRECTION_CREDIT else -delta
        if entry.balance_after is None or entry.balance_after.amount != running:
            problems.append(f"{entry.reference}: balance_after {entry.balance_after} != running total {running}")
            running = entry.balance_after.amount if entry.balance_after is not None else running
        if entry.entry_hash:
            if entry.previous_hash != previous_hash:
                problems.append(f"{entry.reference}: broken hash chain (previous hash mismatch)")
            if compute_entry_hash(entry, entry.previous_hash) != entry.entry_hash:
                problems.append(f"{entry.reference}: entry was modified after posting (hash mismatch)")
            previous_hash = entry.entry_hash

    wallet = Wallet.objects.get(pk=wallet.pk)
    if wallet.balance.amount != running:
        problems.append(f"wallet balance {wallet.balance.amount} != ledger total {running}")
    if wallet.ledger_sequence != expected_sequence:
        problems.append(f"wallet sequence {wallet.ledger_sequence} != last entry position {expected_sequence}")
    if previous_hash and wallet.ledger_head_hash != previous_hash:
        problems.append("wallet head hash does not match the last entry")
    return problems
