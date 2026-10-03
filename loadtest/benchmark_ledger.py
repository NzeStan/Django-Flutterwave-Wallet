"""
Ledger load test: many concurrent workers moving money between many wallets.

Measures throughput on *your* database, then proves the books still balance:
money is neither created nor destroyed and every wallet's ledger (positions,
running balances, hash chain) verifies.

    DJANGO_SETTINGS_MODULE=yourproject.settings python loadtest/benchmark_ledger.py \
        --wallets 200 --workers 32 --seconds 30

Use a PostgreSQL (or MySQL) database you can throw away: it creates users.
SQLite has no row locks and is not meaningful here.
"""
import argparse
import os
import random
import sys
import threading
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tests.settings_postgres')

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, connections  # noqa: E402

from flutterwave_wallet.exceptions import InsufficientFunds, WalletError  # noqa: E402
from flutterwave_wallet.models import Wallet  # noqa: E402
from flutterwave_wallet.services.ledger import verify_wallet_ledger  # noqa: E402
from flutterwave_wallet.services.wallet_service import WalletService  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--wallets', type=int, default=100)
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--opening-balance', default='100000')
    parser.add_argument('--migrate', action='store_true', help='Run migrations first')
    args = parser.parse_args()

    if connection.vendor == 'sqlite':
        sys.exit("Use PostgreSQL or MySQL: SQLite has no row-level locking.")
    if args.migrate:
        call_command('migrate', verbosity=0)

    service = WalletService()
    run_id = int(time.time())
    User = get_user_model()
    wallets = []
    for i in range(args.wallets):
        user = User.objects.create_user(username=f'bench{run_id}_{i}', email=f'bench{run_id}_{i}@example.com')
        wallet = service.get_wallet(user)
        service.credit_wallet(wallet, args.opening_balance, description='benchmark opening balance')
        wallets.append(wallet.pk)
    total_before = sum(Wallet.objects.get(pk=pk).balance.amount for pk in wallets)
    print(f"{args.wallets} wallets funded with {args.opening_balance} each; {args.workers} workers for "
          f"{args.seconds}s on {connection.vendor}")

    stop = time.monotonic() + args.seconds
    counts = {'ok': 0, 'insufficient': 0, 'error': 0}
    latencies = []
    lock = threading.Lock()

    def worker(seed):
        rng = random.Random(seed)
        local = WalletService()
        try:
            while time.monotonic() < stop:
                source, target = rng.sample(wallets, 2)
                amount = Decimal(rng.randint(1, 5000))
                started = time.perf_counter()
                try:
                    local.transfer(Wallet.objects.get(pk=source), str(target), amount)
                    outcome = 'ok'
                except InsufficientFunds:
                    outcome = 'insufficient'
                except WalletError:
                    outcome = 'error'
                elapsed = time.perf_counter() - started
                with lock:
                    counts[outcome] += 1
                    latencies.append(elapsed)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.workers)]
    began = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration = time.monotonic() - began

    latencies.sort()
    pct = lambda p: latencies[min(int(len(latencies) * p), len(latencies) - 1)] * 1000 if latencies else 0
    print(f"transfers: {counts['ok']} ok, {counts['insufficient']} refused (insufficient funds), "
          f"{counts['error']} errors")
    print(f"throughput: {counts['ok'] / duration:.0f} transfers/s ({2 * counts['ok'] / duration:.0f} ledger postings/s)")
    print(f"latency: p50 {pct(0.5):.1f}ms  p95 {pct(0.95):.1f}ms  p99 {pct(0.99):.1f}ms")

    total_after = sum(Wallet.objects.get(pk=pk).balance.amount for pk in wallets)
    problems = {str(pk): verify_wallet_ledger(Wallet.objects.get(pk=pk)) for pk in wallets}
    problems = {k: v for k, v in problems.items() if v}
    print(f"money conserved: {total_before == total_after} ({total_before} -> {total_after})")
    print(f"ledgers verified: {len(wallets) - len(problems)}/{len(wallets)}")
    if total_before != total_after or problems or counts['error']:
        for wallet_id, issues in list(problems.items())[:5]:
            print(wallet_id, issues[:3])
        sys.exit(1)


if __name__ == '__main__':
    main()
