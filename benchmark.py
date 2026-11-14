"""CRUD benchmark: the same steps in every database, for one or more dataset sizes.

    uv run benchmark.py --sizes 10000 100000 1000000

Steps per database and size:
    CREATE  - DataInjection.inject (timed), verified with check()
    READ    - Query.simple / medium / heavy (timed), results compared between databases
    UPDATE  - Query.update: quantity + 1 on every 100th transaction (timed)
    DELETE  - Query.delete_all: delete everything (timed), verified: nothing is left

Run only some steps on already loaded data, e.g. 100 000 transactions:
    uv run benchmark.py --sizes 100000 --steps create
    uv run benchmark.py --sizes 100000 --steps read --repeat 3
    uv run benchmark.py --sizes 100000 --steps update delete
"""
import argparse
import csv
import time
from decimal import Decimal

from dataset import ROOT, RetailDataset
from main import DATABASES, load_module

RESULTS_CSV = ROOT / "results" / "benchmark.csv"
STEPS = ["create", "read", "update", "delete"]
QUERIES = ["simple", "medium", "heavy"]


def timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


def normalize(rows):
    """Make rows comparable between databases (Decimal / float -> rounded int)."""
    return [tuple(round(float(v)) if isinstance(v, (Decimal, float)) else v for v in row) for row in rows]


def run_database(db, dataset, expected, steps, update_every, repeat):
    injection = load_module(db, "DataInjection")
    query = load_module(db, "Query")
    results = []  # (operation, seconds, rows)
    answers = {}

    if "create" in steps:
        _, seconds = timed(injection.inject, dataset)
        if not injection.check(expected, quiet=True):
            raise RuntimeError(f"[{db}] CREATE finished but the data is incomplete")
        results.append(("create", seconds, expected["transactions"]))

    if "read" in steps:
        for name in QUERIES:
            times = []
            for _ in range(repeat):
                rows, seconds = timed(getattr(query, name))
                times.append(seconds)
            answers[name] = normalize(rows)
            results.append((f"read_{name}", min(times), len(rows)))

    if "update" in steps:
        ids = dataset.transaction_ids(update_every)
        updated, seconds = timed(query.update, ids)
        if updated != len(ids):
            raise RuntimeError(f"[{db}] UPDATE changed {updated} rows, expected {len(ids)}")
        results.append(("update", seconds, updated))

    if "delete" in steps:
        left, seconds = timed(query.delete_all)
        if left:
            raise RuntimeError(f"[{db}] DELETE left {left} rows")
        results.append(("delete", seconds, expected["transactions"]))
    return results, answers


def print_table(size, table):
    operations = [op for op, _, _ in next(iter(table.values()))]
    print(f"\n=== {size:,} transactions (seconds) ===")
    print(f"{'operation':<14}" + "".join(f"{db:>12}" for db in table))
    for i, op in enumerate(operations):
        print(f"{op:<14}" + "".join(f"{table[db][i][1]:>12.2f}" for db in table))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10_000, 100_000, 1_000_000],
                        help="number of transactions to create (default: 10000 100000 1000000)")
    parser.add_argument("--dbs", nargs="+", default=DATABASES, choices=DATABASES)
    parser.add_argument("--steps", nargs="+", default=STEPS, choices=STEPS,
                        help="CRUD steps to run (default: all). Without create the data must already be loaded "
                             "with the same --sizes value")
    parser.add_argument("--update-every", type=int, default=100, help="update every N-th transaction")
    parser.add_argument("--repeat", type=int, default=1, help="run each query N times, report the best")
    args = parser.parse_args()

    for db in args.dbs:
        load_module(db, "initDB").init()

    RESULTS_CSV.parent.mkdir(exist_ok=True)
    new_file = not RESULTS_CSV.exists()
    with open(RESULTS_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["size", "database", "operation", "seconds", "rows"])

        for size in args.sizes:
            dataset = RetailDataset(size=size).load()
            expected = dataset.expected_counts()
            table, answers = {}, {}
            for db in args.dbs:
                print(f"\n[{db}] {size:,} transactions ...", flush=True)
                table[db], answers[db] = run_database(db, dataset, expected, args.steps, args.update_every, args.repeat)
                for op, seconds, rows in table[db]:
                    writer.writerow([size, db, op, f"{seconds:.3f}", rows])
                f.flush()
            print_table(size, table)

            reference_db = args.dbs[0]
            for db in args.dbs[1:]:
                for name in answers[db]:
                    if answers[db][name] != answers[reference_db][name]:
                        print(f"WARNING: {name} result of {db} differs from {reference_db}")
    print(f"\nresults appended to {RESULTS_CSV}")


if __name__ == "__main__":
    main()
