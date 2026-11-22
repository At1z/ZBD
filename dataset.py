import csv
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
KAGGLE_DIR = DATA_DIR / "kaggle"
KAGGLE_URL = "https://www.kaggle.com/api/v1/datasets/download/noopurbhatt/retail-sales-dataset"
SOURCE_CSV = KAGGLE_DIR / "retail_sales_dataset.csv"

CUSTOMER_COLUMNS = ["customer_id", "customer_gender", "customer_age_group", "customer_segment"]
PRODUCT_COLUMNS = ["product_id", "product_name", "category", "brand", "unit_price"]
TRANSACTION_COLUMNS = [
    "transaction_id", "transaction_date", "customer_id", "product_id", "quantity",
    "discount_pct", "sales_amount", "payment_method", "sales_channel", "region",
]


class RetailDataset:
    """Retail Sales Dataset (Kaggle) split into customers, products and transactions.

    The source has 120 000 rows. `size` is the number of transactions to produce: the source is
    replicated as many times as needed (ids get the copy number as a prefix, so they stay unique),
    the last copy may be partial. customers only contains customers referenced by the produced
    transactions, products stay a 120-row dictionary.
    """

    def __init__(self, size=1_200_000, shift_years=0):
        self.size = size
        self.shift_years = shift_years  # shift transaction dates by N years per copy
        self.customers = []
        self.products = []
        self.transactions = []

    def download(self):
        if SOURCE_CSV.exists():
            return
        KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
        archive = KAGGLE_DIR / "retail-sales-dataset.zip"
        urllib.request.urlretrieve(KAGGLE_URL, archive)
        with zipfile.ZipFile(archive) as z:
            z.extract("retail_sales_dataset.csv", KAGGLE_DIR)
        archive.unlink()
        print(f"downloaded {SOURCE_CSV}")

    def load(self):
        """Read the source CSV and split it into the 3 tables (before replication)."""
        self.download()
        customers, products, transactions = {}, {}, []
        with open(SOURCE_CSV, newline="") as f:
            for row in csv.DictReader(f):
                # the dataset is synthetic: one customer_id has different attributes in different rows,
                # so the first occurrence wins
                customers.setdefault(row["customer_id"], {c: row[c] for c in CUSTOMER_COLUMNS})
                products.setdefault(row["product_id"], {c: row[c] for c in PRODUCT_COLUMNS})
                transactions.append({c: row[c] for c in TRANSACTION_COLUMNS})
        self.customers = list(customers.values())
        self.products = list(products.values())
        self.transactions = transactions
        return self

    def expected_counts(self):
        """Row counts every database should hold after injection."""
        if not self.transactions:
            self.load()
        return {
            "customers": sum(self._customer_count(rows) for _, rows in self._plan()),
            "products": len(self.products),
            "transactions": self.size,
        }

    def _plan(self):
        """(copy number, number of source rows used) for every copy needed to reach `size`."""
        if not self.transactions:
            self.load()
        full, rest = divmod(self.size, len(self.transactions))
        plan = [(copy, len(self.transactions)) for copy in range(full)]
        if rest:
            plan.append((full, rest))
        return plan

    def _customer_count(self, rows):
        # self.customers is in order of first appearance, so the customers of the first `rows`
        # transactions are a prefix of it
        if rows == len(self.transactions):
            return len(self.customers)
        return len({t["customer_id"] for t in self.transactions[:rows]})

    def transaction_id_at(self, index):
        """Id of the transaction at global position `index` (same order as iter_transactions)."""
        if not self.transactions:
            self.load()
        copy, row = divmod(index, len(self.transactions))
        return self._transaction_id(self.transactions[row]["transaction_id"], copy)

    def transaction_ids(self, every):
        """Ids of every `every`-th transaction (computed by index, without iterating all rows)."""
        return [self.transaction_id_at(i) for i in range(0, self.size, every)]

    def base_rows(self):
        """Source rows used by copy 0: the customers they reference always exist in every size."""
        if not self.transactions:
            self.load()
        return self.transactions[:min(self.size, len(self.transactions))]

    def copy0_customer_id(self, source_customer_id):
        return self._customer_id(source_customer_id, 0)

    def sample_customer_ids(self, count):
        """`count` distinct, evenly spaced ids of customers that exist in the loaded data."""
        ids = list(dict.fromkeys(row["customer_id"] for row in self.base_rows()))
        count = min(count, len(ids))
        return [self.copy0_customer_id(ids[i * len(ids) // count]) for i in range(count)]

    def _customer_id(self, customer_id, copy):
        return f"C{copy}{customer_id[1:]}"

    def _transaction_id(self, transaction_id, copy):
        return f"T{copy}{transaction_id[1:]}"

    def _shift_date(self, value, copy):
        d = date.fromisoformat(value)
        year = d.year + self.shift_years * copy
        try:
            return d.replace(year=year).isoformat()
        except ValueError:  # 29 Feb in a non-leap year
            return d.replace(year=year, day=28).isoformat()

    def iter_customers(self):
        for copy, rows in self._plan():
            for row in self.customers[:self._customer_count(rows)]:
                yield {**row, "customer_id": self._customer_id(row["customer_id"], copy)}

    def iter_products(self):
        yield from self.products

    def iter_transactions(self):
        for copy, rows in self._plan():
            for row in self.transactions[:rows]:
                yield {
                    **row,
                    "transaction_id": self._transaction_id(row["transaction_id"], copy),
                    "customer_id": self._customer_id(row["customer_id"], copy),
                    "transaction_date": self._shift_date(row["transaction_date"], copy),
                }

    def write_csv(self):
        """Write customers.csv, products.csv and transactions.csv to data/."""
        if not self.transactions:
            self.load()
        DATA_DIR.mkdir(exist_ok=True)
        tables = {
            "customers": (CUSTOMER_COLUMNS, self.iter_customers()),
            "products": (PRODUCT_COLUMNS, self.iter_products()),
            "transactions": (TRANSACTION_COLUMNS, self.iter_transactions()),
        }
        for name, (columns, rows) in tables.items():
            path = DATA_DIR / f"{name}.csv"
            count = 0
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
                    count += 1
            print(f"{path.name}: {count} rows")


if __name__ == "__main__":
    RetailDataset().write_csv()
