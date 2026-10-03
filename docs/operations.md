# Operations

## Celery

```python
FLW_WALLET_USE_CELERY = True

CELERY_BEAT_SCHEDULE = {
    'flw-reconcile':        {'task': 'flutterwave_wallet.tasks.reconcile_task', 'schedule': 300},
    'flw-retry-webhooks':   {'task': 'flutterwave_wallet.tasks.retry_failed_webhooks_task', 'schedule': 300},
    'flw-sweep-fees':       {'task': 'flutterwave_wallet.tasks.sweep_platform_fees_task', 'schedule': 600},
    'flw-retry-deliveries': {'task': 'flutterwave_wallet.tasks.retry_webhook_deliveries_task', 'schedule': 900},
    'flw-expire-cards':     {'task': 'flutterwave_wallet.tasks.remove_expired_cards_task', 'schedule': 86400},
    'flw-verify-ledgers':   {'task': 'flutterwave_wallet.tasks.verify_ledgers_task', 'schedule': 86400},
    'flw-prune':            {'task': 'flutterwave_wallet.tasks.prune_task', 'schedule': 86400},
}
```

With Celery on:

- webhooks are stored and acknowledged at once, then processed by
  `process_webhook_event_task` (`acks_late`, exponential backoff up to 8 retries);
- new users' wallets are created on a worker;
- forwarded webhooks are delivered on workers.

Without Celery everything runs inline, and the management commands cover the periodic work
(see [installation.md](installation.md#background-jobs)).

## Scaling

- **No global locks.** An operation locks only the wallets it touches, in primary-key order, so
  transfers in opposite directions cannot deadlock. Throughput scales with the number of wallets.
- **Hot wallets.** Fees are accrued (`PlatformFee`) and swept in batches, so your revenue wallet
  is touched once per sweep, not once per transaction. A very busy merchant wallet still
  serialises its own credits. That is correct for a ledger, and each lock is held only
  for one short DB transaction (never during HTTP calls).
- **External calls never hold wallet locks.** Debits commit before Flutterwave is called.
- **Many workers.** Reconciliation claims each transaction in the cache; fee sweeps use
  `SKIP LOCKED`; periodic tasks take a single-instance cache lock. Run as many workers as you like.
- **Connection pooling.** The HTTP clients keep a pooled `requests.Session` per process.
- **v4 tokens.** One OAuth token is shared by every worker through Django's cache and refreshed a
  minute before it expires, so the identity server sees about one request per 9 minutes, not one per
  payment. Use a shared cache (Redis) in production.
- **Database.** Hot paths are indexed (`wallet+created_at`, `wallet+status`,
  `type+status+created_at`, `status+created_at`, the partial unique ledger index). Use PgBouncer
  in transaction mode if you run many processes.
- **Retention.** `prune_wallet_data` removes processed webhooks and expired idempotency
  records. The ledger is never pruned; partition `flutterwave_wallet_transaction` by
  `created_at` if it grows into the hundreds of millions.

## Monitoring

| Signal / check | Meaning | Action |
| --- | --- | --- |
| `transaction_needs_review` signal, `Transaction.needs_review=True`, `transactions/review-queue/` | Unknown outcome, amount mismatch, success-after-refund | Check the Flutterwave dashboard, then clear the flag (admin action) or `force_reverse_withdrawal` |
| `manage.py verify_ledger` non-zero exit | A balance, position or hash doesn't add up | Incident: something wrote to the ledger outside the services |
| `WebhookEvent.processed=False` older than an hour | Processing keeps failing | Read `processing_error`; reprocess from admin/API |
| Pending deposits/withdrawals older than `RECONCILE_AFTER_MINUTES` | Reconciliation not running | Check beat/cron |
| Sum of wallet balances vs `service.flutterwave_balances()` | Books vs bank | Daily reconciliation report |

## Runbooks

**A withdrawal is stuck "processing" and flagged.** Open it in the admin and use *Re-check with
Flutterwave*. If Flutterwave shows no transfer for the reference and you've confirmed nothing left
the account, use `transactions/{id}/force-reverse/` (staff, audited).

**A webhook failed to process.** Fix the cause, then use admin → Webhook events → *Reprocess*.
It is always safe to reprocess: settlement is idempotent.

**Rotating the card-token key.** Prepend the new key in `FLW_WALLET_FIELD_ENCRYPTION_KEYS`.
New tokens use it and old tokens stay readable. Re-save cards (or wait for them to churn)
before removing the old key.

**Switching from v3 to v4.** Add the v4 client credentials and set `FLW_WALLET_API_VERSION=v4`.
New charges, accounts, withdrawals and refunds use v4. Pending v3 transactions are still verified and
reconciled on v3 (each records its `api_version`), and v3 static virtual accounts keep receiving
deposits. Create v4 static accounts for users when convenient.

**Rotating the webhook secret hash.** Set `FLW_WALLET_WEBHOOK_SIGNATURE_MODE='auto'`, change the
dashboard and the setting together. Flutterwave retries failed deliveries, and reconciliation
covers any gap.
