# ZBD

A project for working with four databases, each running in its own Docker container: PostgreSQL, MySQL, MongoDB and Redis.

## Structure

```
ZBD/
├── main.py            # runs initDB for every database
├── pyproject.toml     # dependencies (uv)
├── uv.lock            # pinned dependency versions (commit it)
├── docker_utils.py    # shared helper that starts a container
├── dataset.py         # RetailDataset: downloads, splits into 3 tables, replicates x10
├── data/              # generated CSVs + Kaggle source (git-ignored)
├── postgres/
│   ├── initDB.py          # starts the container
│   ├── DataInjection.py   # data insertion (empty for now)
│   └── Query.py           # queries (empty for now)
├── mysql/      (same files)
├── mongodb/    (same files)
└── redis/      (same files)
```

## Usage

Requires Docker (running) and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```
uv sync          # first time only: creates .venv and installs the drivers
uv run main.py
```

Dependencies (managed in `pyproject.toml`): `psycopg`, `pymysql`, `pymongo`, `redis`. Add more with `uv add <package>`.

| Database   | Container    | Port  | Login / password |
|------------|--------------|-------|------------------|
| PostgreSQL | zbd-postgres | 5432  | admin / admin    |
| MySQL      | zbd-mysql    | 3306  | root / admin     |
| MongoDB    | zbd-mongodb  | 27017 | admin / admin    |
| Redis      | zbd-redis    | 6379  | none             |

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

## CRUD benchmark

Every database goes through the same steps (`uv run benchmark.py --sizes 10000 100000 1000000`):

| Step   | What happens | File |
|--------|--------------|------|
| CREATE | load N transactions (+ their customers and the 120 products), timed | `DataInjection.py` → `inject()` |
| READ   | 3 queries: **simple** (1 table), **medium** (2 tables), **heavy** (3 tables) | `Query.py` → `simple()`, `medium()`, `heavy()` |
| UPDATE | `quantity + 1` on every 100th transaction | `Query.py` → `update(ids)` |
| DELETE | delete everything | `Query.py` → `delete_all()` |

The queries are the same everywhere (SQL / `$lookup` / client-side join in Redis):

| Query  | Tables | What it computes |
|--------|--------|------------------|
| simple | `transactions` | count and revenue per region for the `Online` channel |
| medium | `transactions` + `products` | count and revenue per product category |
| heavy  | `transactions` + `customers` + `products` | revenue per segment, category and region since 2025-01-01, groups above 1000, top 20 |

Running operations on a chosen number of rows (`--sizes` = number of transactions; the steps share the same data, so use the same size in every call):

```
uv run benchmark.py --sizes 10000                          # full CRUD cycle on 10 000
uv run benchmark.py --sizes 100000 --steps create          # only load 100 000
uv run benchmark.py --sizes 100000 --steps read --repeat 3 # only the 3 queries, best of 3
uv run benchmark.py --sizes 100000 --steps update          # only update every 100th
uv run benchmark.py --sizes 100000 --steps delete          # only delete everything
uv run benchmark.py --sizes 10000 100000 1000000           # whole cycle for three sizes in a row
uv run benchmark.py --sizes 100000 --dbs postgres mongodb  # only some databases
```

Options: `--dbs postgres mongodb` (subset), `--update-every 100`, `--repeat 3` (best of N for queries). Results are printed and appended to `results/benchmark.csv`. The benchmark also compares query results between databases and prints a warning if they differ. Note: it wipes the data in the databases (DELETE step), run `uv run main.py` afterwards to load the full dataset again.

## Files in each database folder

- `initDB.py` – database initialization (currently: starts the container)
- `DataInjection.py` – CREATE: loads the 3 tables from `RetailDataset`, plus `check()` (health query)
- `Query.py` – READ, UPDATE and DELETE: `simple()`, `medium()`, `heavy()`, `update(ids)` (`quantity + 1`), `delete_all()` (Redis: `FLUSHDB`)

## Resources

- [Shared project doc (Google Docs)](https://docs.google.com/document/d/1FrQhxgavi0X86dzHLODkb-GS5DfaSlGvv1snJkOFYeU/edit?tab=t.0)
- [Dataset: Retail Sales Dataset (Kaggle)](https://www.kaggle.com/datasets/noopurbhatt/retail-sales-dataset)
