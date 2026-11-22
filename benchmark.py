"""Benchmark: the 24 scenarios of SCENARIOS.md on every database, for several sizes, several runs each.

    uv run benchmark.py                                   # sizes 100000 1000000 10000000, 3 runs each
    uv run benchmark.py --sizes 100000 --runs 1           # quick check
    uv run benchmark.py --sizes 100000 --dbs postgres     # only some databases
    uv run benchmark.py --no-isolate                      # keep all containers running (default: only the measured one)

One run = S01..S24 in order on one database (S01 loads the data, S24 deletes it). Every scenario runs in its
own child process, so a scenario can be stopped after --timeout seconds ("timeout") or killed by the system
("oom") without stopping the benchmark. If a scenario that changes data fails, the rest of the run is
"skipped" (later results would not be comparable); a failed READ does not stop the run.

Between two runs on different databases the benchmark rests for --pause seconds (default 15).
Only the container of the measured database runs (the others are stopped, and all are started again at the
end); --no-isolate keeps all of them running.

Results are appended to results/runs.csv (or --csv FILE), one row per scenario. Finished (database, size, run) triples are
skipped, so an interrupted benchmark can simply be started again with the same arguments.
"""
import argparse
import csv
import hashlib
import multiprocessing
import os
import time
from collections import defaultdict
from pathlib import Path
from decimal import Decimal

from zbd.dataset import ROOT, RetailDataset
from zbd.docker import stop_container, wrong_limits
from main import DATABASES, load_module
from zbd.scenarios import SCENARIO_IDS, SCENARIOS, Params

RESULTS_CSV = ROOT / "results" / "runs.csv"
COLUMNS = ["size", "run", "database", "scenario", "operation", "difficulty", "seconds", "rows", "status", "digest"]
DEFAULT_SIZES = [100_000, 1_000_000, 10_000_000]


def normalize(result):
    """Make results comparable between databases (Decimal / float -> rounded int).
    Rounded to cents first: a float sum can land just below x.5 where the exact Decimal sum is x.5."""
    if isinstance(result, (list, tuple)):
        return [tuple(round(round(float(v), 2)) if isinstance(v, (Decimal, float)) else v for v in row)
                for row in result]
    return result


def digest(result):
    if result is None:
        return ""
    return hashlib.sha1(repr(normalize(result)).encode()).hexdigest()[:10]


def row_count(result, size):
    if isinstance(result, (list, tuple)):
        return len(result)
    return size if result is None else int(result)  # S01 returns nothing: it loaded `size` transactions


def _child(pipe, db, scenario_id, dataset, params):
    """Runs one scenario and sends ("ok", seconds, result) or ("error", 0, message) through the pipe."""
    try:
        if scenario_id == "S01":
            module = load_module(db, "DataInjection")
            start = time.perf_counter()
            module.inject(dataset)
            seconds, result = time.perf_counter() - start, None
        else:
            query = load_module(db, "Query")
            ctx = query.connect()  # opened outside the timed part
            try:
                start = time.perf_counter()
                result = query.SCENARIOS[scenario_id](ctx, params)
                seconds = time.perf_counter() - start
            finally:
                query.disconnect(ctx)
        pipe.send(("ok", seconds, result))
    except BaseException as error:
        pipe.send(("error", 0.0, f"{type(error).__name__}: {error}"))
    finally:
        pipe.close()


def rss_gb(pid):
    """Resident memory of a process in GB (Linux); 0 where /proc is not available."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024**3
    except (OSError, ValueError):
        return 0.0


def run_scenario(db, scenario_id, dataset, params, timeout, memory_limit_gb=0):
    """Returns (status, seconds, result, message); status: ok | timeout | oom | error.
    The child is killed when it runs longer than `timeout` or its memory exceeds `memory_limit_gb` (0 = no limit),
    so a scenario that collects millions of rows in Python cannot take the whole machine down."""
    context = multiprocessing.get_context("fork")  # the child reuses the loaded dataset without pickling it
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(sender, db, scenario_id, dataset, params), daemon=True)
    process.start()
    sender.close()
    try:
        deadline = time.monotonic() + timeout
        while not receiver.poll(0.5):
            if time.monotonic() >= deadline:
                process.kill()
                process.join()
                return "timeout", timeout, None, f"stopped after {timeout}s"
            if memory_limit_gb and rss_gb(process.pid) > memory_limit_gb:
                process.kill()
                process.join()
                return "oom", 0.0, None, f"client process used more than {memory_limit_gb} GB"
        try:
            kind, seconds, payload = receiver.recv()
        except EOFError:  # the child died without answering
            process.join()
            if process.exitcode == -9:
                return "oom", 0.0, None, "child killed (SIGKILL, probably out of memory)"
            return "error", 0.0, None, f"child exited with code {process.exitcode}"
        process.join()
        if kind == "ok":
            return "ok", seconds, payload, ""
        return "error", 0.0, None, payload
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join()


def run_database(db, dataset, expected, params, timeout, load_timeout, memory_limit_gb):
    """One run on one database. Returns (records, answers); records are the CSV rows without size / run."""
    query = load_module(db, "Query")
    missing = [i for i in SCENARIO_IDS[1:] if i not in getattr(query, "SCENARIOS", {})]
    if missing:
        raise RuntimeError(f"[{db}] Query.py does not implement {', '.join(missing)} yet")
    injection = load_module(db, "DataInjection")

    records, answers, aborted = [], {}, False
    for s in SCENARIOS:
        if aborted:
            status, seconds, result, message = "skipped", 0.0, None, "an earlier scenario failed"
        else:
            status, seconds, result, message = run_scenario(
                db, s.id, dataset, params, load_timeout if s.id == "S01" else timeout, memory_limit_gb)
            if status == "ok" and s.id == "S01" and not injection.check(expected, quiet=True):
                status, message = "error", "data incomplete after loading"
            if status != "ok" and s.operation != "READ":
                aborted = True  # a failed change leaves the data in an unknown state
        if status == "ok":
            answers[s.id] = normalize(result)
        records.append({
            "scenario": s.id, "operation": s.operation, "difficulty": s.difficulty,
            "seconds": f"{seconds:.4f}", "rows": row_count(result, dataset.size) if status == "ok" else "",
            "status": status, "digest": digest(result) if status == "ok" else "",
        })
        print(f"  [{db}] {s.id} {s.name:<52} {status:<8} {seconds:>9.3f}s  {message}".rstrip(), flush=True)
    return records, answers


def completed_runs(path):
    """(size, run, database) triples that already have all scenarios in the CSV."""
    if not path.exists():
        return set()
    counts = defaultdict(int)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise SystemExit(f"{path} has a different format (old benchmark?). Move or delete it first.")
        for row in reader:
            counts[(int(row["size"]), int(row["run"]), row["database"])] += 1
    return {key for key, n in counts.items() if n >= len(SCENARIOS)}


def print_table(size, run, results):
    print(f"\n=== {size:,} transactions, run {run} (seconds) ===")
    print(f"{'scenario':<10}" + "".join(f"{db:>12}" for db in results))
    for i, s in enumerate(SCENARIOS):
        cells = []
        for db in results:
            record = results[db][i]
            cells.append(f"{float(record['seconds']):>12.3f}" if record["status"] == "ok" else f"{record['status']:>12}")
        print(f"{s.id + ' ' + s.operation[:1]:<10}" + "".join(cells))


def compare(answers):
    """Warn when a scenario returned different results in different databases."""
    for s in SCENARIOS:
        reference = None
        for db, per_scenario in answers.items():
            if s.id not in per_scenario:
                continue
            if reference is None:
                reference = (db, per_scenario[s.id])
            elif per_scenario[s.id] != reference[1]:
                print(f"WARNING: {s.id} result of {db} differs from {reference[0]}")


def switch_to(db):
    """--isolate: stop the other databases' containers and make sure only this one is running."""
    for other in DATABASES:
        if other != db:
            stop_container(f"zbd-{other}")
    load_module(db, "initDB").init()


def restore_containers():
    """Start every database again (the state main.py expects)."""
    for db in DATABASES:
        load_module(db, "initDB").init()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES,
                        help="number of transactions (default: 100000 1000000 10000000)")
    parser.add_argument("--runs", type=int, default=3, help="runs per size and database (default: 3)")
    parser.add_argument("--dbs", nargs="+", default=DATABASES, choices=DATABASES)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per scenario (default: 1800)")
    parser.add_argument("--load-timeout", type=int, default=7200, help="seconds for S01, the bulk load (default: 7200)")
    parser.add_argument("--csv", type=Path, default=RESULTS_CSV,
                        help="results file (default: results/runs.csv); use another file to keep results measured "
                             "under different container limits apart")
    parser.add_argument("--allow-wrong-limits", action="store_true",
                        help="measure even if an existing container has other memory / cpu limits than zbd/docker.py")
    parser.add_argument("--pause", type=int, default=15,
                        help="seconds of rest before each run on a database that follows another one "
                             "(default: 15, 0 = none)")
    parser.add_argument("--client-memory-gb", type=float, default=3.0,
                        help="kill a scenario whose Python process uses more memory than this (default: 3, 0 = off)")
    parser.add_argument("--isolate", action=argparse.BooleanOptionalAction, default=True,
                        help="stop the other databases' containers while one is measured and start them again at "
                             "the end (default: on; --no-isolate keeps all containers running)")
    args = parser.parse_args()
    try:
        run(args)
    finally:
        if args.isolate:
            try:
                restore_containers()
            except Exception as error:  # do not hide the error that ended the benchmark
                print(f"WARNING: could not start all containers again: {error}")


def run(args):
    problems = [message for db in args.dbs if (message := wrong_limits(f"zbd-{db}"))]
    if problems and not args.allow_wrong_limits:
        raise SystemExit("Refusing to measure: results under different container limits are not comparable.\n  "
                         + "\n  ".join(problems) + "\n(--allow-wrong-limits measures anyway)")
    if not args.isolate:
        for db in args.dbs:
            load_module(db, "initDB").init()

    args.csv.parent.mkdir(exist_ok=True)
    done = completed_runs(args.csv)
    new_file = not args.csv.exists()
    with open(args.csv, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()

        measured_before = False
        for size in args.sizes:
            dataset = RetailDataset(size=size).load()
            expected = dataset.expected_counts()
            params = Params(dataset)
            for run in range(1, args.runs + 1):
                results, answers = {}, {}
                for db in args.dbs:
                    if (size, run, db) in done:
                        print(f"[{db}] {size:,} transactions, run {run}: already in {args.csv.name}, skipping")
                        continue
                    print(f"\n[{db}] {size:,} transactions, run {run}", flush=True)
                    if args.isolate:
                        switch_to(db)
                    if measured_before and args.pause:  # let the machine settle between two databases
                        print(f"  pause {args.pause}s", flush=True)
                        time.sleep(args.pause)
                    measured_before = True
                    records, answers[db] = run_database(db, dataset, expected, params, args.timeout, args.load_timeout,
                                                       args.client_memory_gb)
                    results[db] = records
                    for record in records:
                        writer.writerow({"size": size, "run": run, "database": db, **record})
                    f.flush()
                if results:
                    print_table(size, run, results)
                    compare(answers)
    print(f"\nresults appended to {args.csv}")


if __name__ == "__main__":
    main()
