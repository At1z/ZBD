import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent
DATABASES = ["postgres", "mysql", "mongodb", "redis"]


def load_init(db):
    # load by file path so the folders do not shadow installed packages (redis, mysql...)
    path = ROOT / db / "initDB.py"
    spec = importlib.util.spec_from_file_location(f"{db}_initDB", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.init


def main():
    for db in DATABASES:
        load_init(db)()


if __name__ == "__main__":
    main()
