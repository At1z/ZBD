import importlib.util
from pathlib import Path

from zbd.dataset import RetailDataset

ROOT = Path(__file__).parent
DATABASES = ["postgres", "mysql", "mongodb", "redis"]


def load_module(db, name):
    # load by file path so the folders do not shadow installed packages (redis, mysql...)
    path = ROOT / "databases" / db / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{db}_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    for db in DATABASES:
        load_module(db, "initDB").init()

    dataset = RetailDataset().load()
    expected = dataset.expected_counts()
    for db in DATABASES:
        injection = load_module(db, "DataInjection")
        if injection.check(expected, quiet=True):
            print(f"[{db}] data already loaded, skipping")
            continue
        print(f"[{db}] no data yet (or incomplete), loading...")
        injection.inject(dataset)
        if not injection.check(expected):
            raise RuntimeError(f"[{db}] data injection finished but the health query failed")


if __name__ == "__main__":
    main()
