# Load testing

`benchmark_ledger.py` runs many concurrent workers moving money between many wallets on
**your** database, then proves the books still balance: the total across wallets is
unchanged and every wallet's ledger (positions, running balances, hash chain) verifies.

```bash
DJANGO_SETTINGS_MODULE=yourproject.settings python loadtest/benchmark_ledger.py \
    --wallets 200 --workers 32 --seconds 30
```

Use a disposable PostgreSQL/MySQL database (the script creates users). `--migrate` runs
migrations first.

Example run (laptop, embedded PostgreSQL 16, 100 wallets, 24 workers, 20 s):

```
transfers: 4380 ok, 1 refused (insufficient funds), 0 errors
throughput: 218 transfers/s (436 ledger postings/s)
latency: p50 102.2ms  p95 169.1ms  p99 225.5ms
money conserved: True (10000000.00 -> 10000000.00)
ledgers verified: 100/100
```

Throughput grows with database hardware and the number of distinct wallets involved.
Operations only lock the wallets they touch, so unrelated users never wait on each other.
