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
    conn = connections.mysql()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())
    finally:
        conn.close()


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
    conn = connections.mysql()
    try:
        with conn.cursor() as cur:
            for i in range(0, len(ids), UPDATE_BATCH_SIZE):
                batch = ids[i:i + UPDATE_BATCH_SIZE]
                placeholders = ", ".join(["%s"] * len(batch))
                updated += cur.execute(
                    f"UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id IN ({placeholders})",
                    batch,
                )
        conn.commit()
    finally:
        conn.close()
    return updated


# DELETE

TABLES = ["transactions", "products", "customers"]  # children first (foreign keys)


def delete_all():
    """DELETE every row from all 3 tables. Returns the number of rows left (should be 0)."""
    conn = connections.mysql()
    try:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"DELETE FROM {table}")
            conn.commit()
            left = 0
            for table in TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                left += cur.fetchone()[0]
        return left
    finally:
        conn.close()
