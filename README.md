# ZBD

A project for working with four databases, each running in its own Docker container: PostgreSQL, MySQL, MongoDB and Redis.

## Structure

```
ZBD/
├── main.py            # runs initDB for every database
├── pyproject.toml     # dependencies (uv)
├── uv.lock            # pinned dependency versions (commit it)
├── docker_utils.py    # shared helper that starts a container
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

## Files in each database folder

- `initDB.py` – database initialization (currently: starts the container)
- `DataInjection.py` – data injection (to do)
- `Query.py` – queries (to do)

## Resources

- [Shared project doc (Google Docs)](https://docs.google.com/document/d/1FrQhxgavi0X86dzHLODkb-GS5DfaSlGvv1snJkOFYeU/edit?tab=t.0)
- [Dataset: Retail Sales Dataset (Kaggle)](https://www.kaggle.com/datasets/noopurbhatt/retail-sales-dataset)
