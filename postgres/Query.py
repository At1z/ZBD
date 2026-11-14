import connections

# CRUD in one file (READ, UPDATE, DELETE); CREATE is in DataInjection.py.
# Same 3 read queries in every database (see README):
#   simple - 1 table, medium - 2 tables, heavy - 3 tables

SIMPLE = """
SELECT region, COUNT(*), SUM(sales_amount)
FROM transactions
WHERE sales_channel = 'Online'
GROUP BY region
ORDER BY region
"""

MEDIUM = """
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category
"""

HEAVY = """
SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
WHERE t.transaction_date >= '2025-01-01'
GROUP BY c.customer_segment, p.category, t.region
HAVING SUM(t.sales_amount) > 1000
ORDER BY revenue DESC, c.customer_segment, p.category, t.region
LIMIT 20
"""


def run(sql):
    with connections.postgres() as conn:
        return conn.execute(sql).fetchall()


def simple():
    return run(SIMPLE)


def medium():
    return run(MEDIUM)


def heavy():
    return run(HEAVY)


# UPDATE

UPDATE_BATCH_SIZE = 1000


def update(ids):
    """UPDATE: quantity + 1 for the given transaction ids. Returns the number of updated rows."""
    updated = 0
    with connections.postgres() as conn:
        for i in range(0, len(ids), UPDATE_BATCH_SIZE):
            cur = conn.execute(
                "UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id = ANY(%s)",
                (ids[i:i + UPDATE_BATCH_SIZE],),
            )
            updated += cur.rowcount
    return updated


# DELETE

TABLES = ["transactions", "products", "customers"]  # children first (foreign keys)


def delete_all():
    """DELETE every row from all 3 tables. Returns the number of rows left (should be 0)."""
    with connections.postgres() as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        return sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES)
