import pymysql

import connections
from dataset import CUSTOMER_COLUMNS, PRODUCT_COLUMNS, TRANSACTION_COLUMNS

BATCH_SIZE = 10_000

SCHEMA = [
    "SET FOREIGN_KEY_CHECKS = 0",
    "DROP TABLE IF EXISTS transactions, products, customers",
    "SET FOREIGN_KEY_CHECKS = 1",
    """
    CREATE TABLE customers (
        customer_id        VARCHAR(16) PRIMARY KEY,
        customer_gender    VARCHAR(16) NOT NULL,
        customer_age_group VARCHAR(16) NOT NULL,
        customer_segment   VARCHAR(16) NOT NULL
    )
    """,
    """
    CREATE TABLE products (
        product_id   VARCHAR(16) PRIMARY KEY,
        product_name VARCHAR(64) NOT NULL,
        category     VARCHAR(32) NOT NULL,
        brand        VARCHAR(32) NOT NULL,
        unit_price   DECIMAL(10, 2) NOT NULL
    )
    """,
    """
    CREATE TABLE transactions (
        transaction_id   VARCHAR(16) PRIMARY KEY,
        transaction_date DATE NOT NULL,
        customer_id      VARCHAR(16) NOT NULL,
        product_id       VARCHAR(16) NOT NULL,
        quantity         INT NOT NULL,
        discount_pct     INT NOT NULL,
        sales_amount     DECIMAL(12, 2) NOT NULL,
        payment_method   VARCHAR(32) NOT NULL,
        sales_channel    VARCHAR(32) NOT NULL,
        region           VARCHAR(32) NOT NULL,
        INDEX (transaction_date),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (product_id) REFERENCES products (product_id)
    )
    """,
]


def insert_rows(conn, table, columns, rows):
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    batch = []
    with conn.cursor() as cur:
        for row in rows:
            batch.append([row[c] for c in columns])
            if len(batch) == BATCH_SIZE:
                cur.executemany(sql, batch)  # pymysql turns this into one multi-row INSERT
                batch.clear()
        if batch:
            cur.executemany(sql, batch)
    conn.commit()


def inject(dataset):
    tables = [
        ("customers", CUSTOMER_COLUMNS, dataset.iter_customers()),
        ("products", PRODUCT_COLUMNS, dataset.iter_products()),
        ("transactions", TRANSACTION_COLUMNS, dataset.iter_transactions()),
    ]
    conn = connections.mysql()
    try:
        with conn.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            cur.execute("SET unique_checks = 0")
            cur.execute("SET foreign_key_checks = 0")
        for name, columns, rows in tables:
            insert_rows(conn, name, columns, rows)
            print(f"[mysql] {name} loaded")
    finally:
        conn.close()


def check(expected, quiet=False):
    """Health query: row counts plus a 3-table JOIN that must match every transaction."""
    try:
        conn = connections.mysql()
    except pymysql.MySQLError:
        return False
    try:
        with conn.cursor() as cur:
            counts = {}
            for table in expected:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM transactions t "
                "JOIN customers c USING (customer_id) JOIN products p USING (product_id)"
            )
            joined = cur.fetchone()[0]
    except pymysql.MySQLError:
        return False
    finally:
        conn.close()
    ok = counts == expected and joined == expected["transactions"]
    if not quiet:
        print(f"[mysql] {counts}, joined transactions: {joined} -> {'OK' if ok else 'MISSING DATA'}")
    return ok
