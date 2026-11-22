# ZBD

A project for working with four databases, each running in its own Docker container: PostgreSQL, MySQL, MongoDB and Redis.

## Structure

```
ZBD/
├── main.py            # runs initDB for every database and loads the dataset
├── benchmark.py       # runs the 24 scenarios on every database (runs, timeouts, CSV)
├── scenarios.py       # the 24 scenarios and their shared parameters
├── SCENARIOS.md       # description of every scenario (generated)
├── report.py          # tables and charts from results/runs.csv
├── connections.py     # connection factories shared by all databases
├── pyproject.toml     # dependencies (uv)
├── uv.lock            # pinned dependency versions (commit it)
├── docker_utils.py    # shared helper that starts a container
├── dataset.py         # RetailDataset: downloads, splits into 3 tables, replicates x10
├── data/              # generated CSVs + Kaggle source (git-ignored)
├── postgres/
│   ├── initDB.py          # starts the container
│   ├── DataInjection.py   # CREATE: schema + data insertion, check() health query
│   └── Query.py           # READ / UPDATE / DELETE: simple(), medium(), heavy(), update(), delete_all()
├── mysql/      (same files)
├── mongodb/    (same files)
└── redis/      (same files)
```

## Usage

Requires Docker (running) and [uv](https://docs.astral.sh/uv/) (see the uv docs for installation).

```
uv sync          # first time only: creates .venv and installs the drivers
uv run main.py
```

Dependencies (managed in `pyproject.toml`): `psycopg`, `pymysql`, `pymongo`, `redis`. Add more with `uv add <package>`.

| Database   | Image (pinned)      | Container    | Port  | Login / password |
|------------|---------------------|--------------|-------|------------------|
| PostgreSQL | `postgres:18.7`     | zbd-postgres | 5432  | admin / admin    |
| MySQL      | `mysql:9.7.4`       | zbd-mysql    | 3306  | root / admin     |
| MongoDB    | `mongo:9.0.8`       | zbd-mongodb  | 27017 | admin / admin    |
| Redis      | `redis:8.10.3`      | zbd-redis    | 6379  | none             |

Exact versions are pinned on purpose, so benchmark results stay comparable over time. After changing a version, remove the old container (`docker rm -f ...` below), otherwise the old image keeps running.

Every container has the same hard limit of **5 GB RAM** (`--memory=5g --memory-swap=5g`, see `MEMORY_LIMIT` in `docker_utils.py`), so the comparison is fair and a database that runs out of memory (e.g. Redis with 10M transactions) is stopped by Docker instead of freezing the machine. Redis additionally runs with `maxmemory 4gb` and `noeviction`: when full, writes fail with an error instead of the container being killed. The limit is set when a container is created: containers created earlier have none, `uv run main.py` warns about them, recreate them with the `docker rm -f` command below.

Running it again is safe: existing containers are restarted, not recreated.
To remove the containers: `docker rm -f zbd-postgres zbd-mysql zbd-mongodb zbd-redis`.

## Health checks

Each container is created with a Docker health check (runs every 5s):

| Container    | Check                                       |
|--------------|---------------------------------------------|
| zbd-postgres | `pg_isready -U admin -d zbd`                |
| zbd-mysql    | `mysqladmin ping`                           |
| zbd-mongodb  | `mongosh` ping command                      |
| zbd-redis    | `redis-cli ping`                            |

`uv run main.py` waits for every container to become healthy (up to 120s each) and prints `[name] healthy`. If a container does not become healthy, it exits with an error that points to `docker logs`. You can also check manually:

```
docker ps --filter name=zbd- --format "table {{.Names}}\t{{.Status}}"
```

Each container should show `(healthy)`. `(health: starting)` means it is still booting (MySQL can take ~30s on the first run); `(unhealthy)` means the check keeps failing.

Single container:

```
docker inspect --format "{{.State.Health.Status}}" zbd-postgres
docker logs zbd-postgres
```

Note: containers created before health checks were added have none. Recreate them with the `docker rm -f` command above and run `uv run main.py` again.

## Dataset

[Retail Sales Dataset (Kaggle)](https://www.kaggle.com/datasets/noopurbhatt/retail-sales-dataset): 120 000 rows, 17 columns. `dataset.py` (class `RetailDataset`) downloads it, splits it into 3 tables and replicates it 10 times, so we get ~1.2M transactions:

```
uv run dataset.py     # writes data/customers.csv, data/products.csv, data/transactions.csv
```

| Table          | Rows (x10)  | Columns |
|----------------|-------------|---------|
| `customers`    | 247 840     | `customer_id` (PK), `customer_gender`, `customer_age_group`, `customer_segment` |
| `products`     | 120         | `product_id` (PK), `product_name`, `category`, `brand`, `unit_price` |
| `transactions` | 1 200 000   | `transaction_id` (PK), `transaction_date`, `customer_id` (FK), `product_id` (FK), `quantity`, `discount_pct`, `sales_amount`, `payment_method`, `sales_channel`, `region` |

- `transactions` and `customers` are copied 10 times; ids get the copy number as a prefix (`T00000001`, `T10000001`, ...), so they stay unique and every copy keeps its own customers.
- `products` is a dictionary and stays at 120 rows (not replicated).
- The source is synthetic: one `customer_id` appears with different gender/age/segment in different rows. `customers` keeps the first occurrence.
- `RetailDataset(size=1_200_000, shift_years=0)`: `size` is the number of transactions (10 000, 100 000, 1 000 000, ...). The source is replicated as many times as needed and `customers` only holds customers referenced by those transactions. `shift_years` can shift transaction dates per copy.
- `data/` and `*.csv` are in `.gitignore`.

## Benchmark

24 scenarios (8 easy, 8 medium, 8 heavy; 3 CREATE, 14 READ, 3 UPDATE, 4 DELETE), identical for every database. The full list with the query of each scenario is in [SCENARIOS.md](SCENARIOS.md) (generated from `scenarios.py`: `uv run scenarios.py --markdown`).

One **run** = S01..S24 in a fixed order on one database (S01 loads the data, S24 deletes it). Every size is repeated 3 times, so 3 sizes x 3 runs = 9 runs per database:

```
uv run benchmark.py                                      # sizes 100000 1000000 10000000, 3 runs each
uv run benchmark.py --sizes 100000 --runs 1              # quick check
uv run benchmark.py --sizes 100000 --dbs postgres mongodb
uv run benchmark.py --timeout 600 --load-timeout 3600    # seconds per scenario / for the bulk load
uv run benchmark.py --isolate                            # stop the other containers while one database is measured
uv run benchmark.py --client-memory-gb 2                 # kill a scenario whose Python process exceeds 2 GB (default 3)
uv run scenarios.py --size 1000000                       # show the parameters the scenarios use
```

- Databases are measured one after another, never in parallel. `--isolate` also stops the other databases' containers during a measurement (frees RAM for 10M rows, adds a few seconds per run and database) and starts them all again at the end.
- Every scenario runs in its own process: a scenario that takes longer than `--timeout` is recorded as `timeout`, one whose Python process exceeds `--client-memory-gb` or that is killed by the system as `oom`, an exception as `error`. A failed READ does not stop the run; a failed CREATE / UPDATE / DELETE makes the rest of the run `skipped`, because later results would not be comparable.
- Results are appended to `results/runs.csv` (`size, run, database, scenario, operation, difficulty, seconds, rows, status, digest`). Finished (database, size, run) triples are skipped, so an interrupted benchmark can be started again with the same arguments.
- Ids, filters and new rows are derived from the data (`Params` in `scenarios.py`), so every database gets exactly the same input. Results are compared between databases and a warning is printed if they differ.
- Each `Query.py` has to provide `connect()`, `disconnect(ctx)` and `SCENARIOS = {"S02": fn, ..., "S24": fn}` (S01 is `DataInjection.inject`). Note: the benchmark wipes the data in the databases, run `uv run main.py` afterwards to load the full dataset again.

## Report

```
uv run report.py                          # results/runs.csv -> results/report/report.md + PNG charts
uv run report.py --csv other.csv --out some/dir
```

`results/report/report.md` contains: one table per database (rows CREATE / READ / UPDATE / DELETE / SUM, columns: every size x runs 1..3 + the best run), one comparison per size (best run of every database, then all 24 scenarios), an appendix with every scenario in every run, and the charts (per database: sizes compared; per size: databases compared; per size: all 24 scenarios; log scale). "Best run" is the complete run with the smallest total; a scenario that failed is shown as its status (`timeout`, `oom`, `error`, `skipped`) and as a cross on the charts, never as a zero. Colors follow the entity (one fixed color per database).

## Files in each database folder

- `initDB.py` – database initialization (starts the container)
- `DataInjection.py` – CREATE (scenario S01): schema, bulk load of the 3 tables from `RetailDataset`, indexes, plus `check()` (health query)
- `Query.py` – scenarios S02..S24 (`SCENARIOS`, `connect()`, `disconnect()`), see [SCENARIOS.md](SCENARIOS.md)

Indexes: only the ones some scenario uses (transaction date, customer; Redis also the `Online` channel set). There is no `product_id` or `region` index in any database; the only exception is MySQL, where InnoDB creates an index for the `product_id` foreign key automatically.

Redis has no joins or aggregation, so scenarios with joins / grouping read the transactions page by page (by rank in the date index, or with SSCAN) and aggregate in Python. Memory of the client stays flat whatever the size, but these scenarios are slow by nature.

## Resources

- [Shared project doc (Google Docs)](https://docs.google.com/document/d/1FrQhxgavi0X86dzHLODkb-GS5DfaSlGvv1snJkOFYeU/edit?tab=t.0)
- [Dataset: Retail Sales Dataset (Kaggle)](https://www.kaggle.com/datasets/noopurbhatt/retail-sales-dataset)
