import psycopg

from dataset import CUSTOMER_COLUMNS, PRODUCT_COLUMNS, TRANSACTION_COLUMNS

DSN = "host=localhost port=5432 dbname=zbd user=admin password=admin"

SCHEMA = """
DROP TABLE IF EXISTS transactions, products, customers CASCADE;

CREATE TABLE customers (
    customer_id        TEXT PRIMARY KEY,
    customer_gender    TEXT NOT NULL,
    customer_age_group TEXT NOT NULL,
    customer_segment   TEXT NOT NULL
);

CREATE TABLE products (
    product_id   TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    brand        TEXT NOT NULL,
    unit_price   NUMERIC(10, 2) NOT NULL
);

CREATE TABLE transactions (
    transaction_id   TEXT PRIMARY KEY,
    transaction_date DATE NOT NULL,
    customer_id      TEXT NOT NULL REFERENCES customers (customer_id),
    product_id       TEXT NOT NULL REFERENCES products (product_id),
    quantity         INT NOT NULL,
    discount_pct     INT NOT NULL,
    sales_amount     NUMERIC(12, 2) NOT NULL,
    payment_method   TEXT NOT NULL,
    sales_channel    TEXT NOT NULL,
    region           TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX ON transactions (customer_id);
CREATE INDEX ON transactions (product_id);
CREATE INDEX ON transactions (transaction_date);
"""


def inject(dataset):
    tables = [
        ("customers", CUSTOMER_COLUMNS, dataset.iter_customers()),
        ("products", PRODUCT_COLUMNS, dataset.iter_products()),
        ("transactions", TRANSACTION_COLUMNS, dataset.iter_transactions()),
    ]
    with psycopg.connect(DSN) as conn:
        conn.execute(SCHEMA)
        for name, columns, rows in tables:
            with conn.cursor() as cur, cur.copy(f"COPY {name} ({', '.join(columns)}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row([row[c] for c in columns])
            print(f"[postgres] {name} loaded")
        conn.execute(INDEXES)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("ANALYZE")


def check(expected, quiet=False):
    """Health query: row counts plus a 3-table JOIN that must match every transaction."""
    try:
        with psycopg.connect(DSN) as conn:
            counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in expected}
            joined = conn.execute(
                "SELECT count(*) FROM transactions t "
                "JOIN customers c USING (customer_id) JOIN products p USING (product_id)"
            ).fetchone()[0]
    except psycopg.Error:
        return False
    ok = counts == expected and joined == expected["transactions"]
    if not quiet:
        print(f"[postgres] {counts}, joined transactions: {joined} -> {'OK' if ok else 'MISSING DATA'}")
    return ok
