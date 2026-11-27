"""MySQL implementation of scenarios S02..S32 (see SCENARIOS.md; S01 is DataInjection.inject).

Contract used by benchmark.py: connect() -> ctx, disconnect(ctx), SCENARIOS = {id: fn(ctx, params)}.
A scenario returns a list of tuples (READ) or the number of affected rows (CREATE / UPDATE / DELETE).
Changes are committed inside the scenario, so the commit is part of the measured time.
Every UPDATE here changes the value (x + 1), so MySQL's "changed rows" equals the matched rows.
"""
from decimal import Decimal

from zbd import connections
from zbd.dataset import CUSTOMER_COLUMNS, TRANSACTION_COLUMNS

BATCH = 1000          # ids per statement in the batched UPDATE
INSERT_BATCH = 5000   # rows per executemany (pymysql turns it into one multi-row INSERT)

ROW_COLUMNS = ", ".join(TRANSACTION_COLUMNS)
SALES_AMOUNT = TRANSACTION_COLUMNS.index("sales_amount")
INSERT = f"INSERT INTO transactions ({ROW_COLUMNS}) VALUES ({', '.join(['%s'] * len(TRANSACTION_COLUMNS))})"
INSERT_CUSTOMER = (f"INSERT INTO customers ({', '.join(CUSTOMER_COLUMNS)}) "
                   f"VALUES ({', '.join(['%s'] * len(CUSTOMER_COLUMNS))})")
SUMMARY = "monthly_sales"  # table created by S15

MEDIUM = """
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category
"""

HEAVY = """
SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n,
       SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
WHERE t.transaction_date >= %s
GROUP BY c.customer_segment, p.category, t.region
HAVING SUM(t.sales_amount) > 1000
ORDER BY revenue DESC, c.customer_segment, p.category, t.region
LIMIT 20
"""


def connect():
    return connections.mysql()


def disconnect(conn):
    conn.close()


def fetch(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def read_by_key(conn, ids):
    """One query per id. Returns [(rows found, sum of sales_amount)]."""
    found, total = 0, Decimal(0)
    with conn.cursor() as cur:
        for tid in ids:
            cur.execute(f"SELECT {ROW_COLUMNS} FROM transactions WHERE transaction_id = %s", (tid,))
            row = cur.fetchone()
            if row:
                found += 1
                total += row[SALES_AMOUNT]
    return [(found, total)]


def execute_each(conn, sql, args_list):
    """Run one statement per element (no batching), commit once. Returns the number of affected rows."""
    affected = 0
    with conn.cursor() as cur:
        for args in args_list:
            affected += cur.execute(sql, args)
    conn.commit()
    return affected


def execute_once(conn, sql, args):
    with conn.cursor() as cur:
        affected = cur.execute(sql, args)
    conn.commit()
    return affected


def row_values(row):
    return [row[c] for c in TRANSACTION_COLUMNS]


# READ

def s02(conn, p):
    return read_by_key(conn, p.pk_ids)


def s03(conn, p):
    return fetch(conn, """
        SELECT region, COUNT(*), SUM(sales_amount)
        FROM transactions
        WHERE sales_channel = 'Online'
        GROUP BY region
        ORDER BY region
    """)


def s04(conn, p):
    return fetch(conn, """
        SELECT COUNT(*) FROM transactions
        WHERE transaction_date >= %s AND transaction_date < %s
    """, (p.count_start, p.count_end))


def s05(conn, p):
    return fetch(conn, """
        SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN products p USING (product_id)
        GROUP BY p.category
        ORDER BY revenue DESC, p.category
    """)


def s06(conn, p):
    return fetch(conn, """
        SELECT p.product_id, p.product_name, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN products p USING (product_id)
        GROUP BY p.product_id, p.product_name
        ORDER BY revenue DESC, p.product_id
        LIMIT 10
    """)


def s07(conn, p):
    rows, names = 0, set()
    with conn.cursor() as cur:
        for customer_id in p.customer_ids:
            cur.execute("""
                SELECT t.transaction_id, p.product_name
                FROM transactions t
                JOIN products p USING (product_id)
                WHERE t.customer_id = %s
            """, (customer_id,))
            for _, name in cur.fetchall():
                rows += 1
                names.add(name)
    return [(rows, len(names))]


def s08(conn, p):
    return fetch(conn, """
        SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN customers c USING (customer_id)
        JOIN products p USING (product_id)
        WHERE t.transaction_date >= %s
        GROUP BY c.customer_segment, p.category, t.region
        HAVING SUM(t.sales_amount) > 1000
        ORDER BY revenue DESC, c.customer_segment, p.category, t.region
        LIMIT 20
    """, (p.heavy_since,))


def s09(conn, p):
    return fetch(conn, """
        SELECT EXTRACT(YEAR FROM t.transaction_date) AS y, EXTRACT(MONTH FROM t.transaction_date) AS m,
               p.category, c.customer_age_group, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN customers c USING (customer_id)
        JOIN products p USING (product_id)
        GROUP BY 1, 2, 3, 4
        ORDER BY revenue DESC, 1, 2, 3, 4
        LIMIT 20
    """)


def s14(conn, p):
    return read_by_key(conn, p.new_single_ids)


def s22(conn, p):
    return fetch(conn, MEDIUM)


def s23(conn, p):
    return fetch(conn, HEAVY, (p.heavy_since,))


def s29(conn, p):
    return fetch(conn, "SELECT COUNT(*) FROM transactions")


def s30(conn, p):
    return fetch(conn, MEDIUM)


def s31(conn, p):
    return fetch(conn, HEAVY, (p.heavy_since,))


# CREATE

def s10(conn, p):
    return execute_each(conn, INSERT, [row_values(r) for r in p.new_single])


def insert_batches(cur, rows):
    """executemany in batches of INSERT_BATCH. Returns the number of inserted rows (no commit)."""
    inserted = 0
    for i in range(0, len(rows), INSERT_BATCH):
        inserted += cur.executemany(INSERT, [row_values(r) for r in rows[i:i + INSERT_BATCH]])
    return inserted


def s11(conn, p):
    with conn.cursor() as cur:
        inserted = insert_batches(cur, p.new_batch)
    conn.commit()
    return inserted


def s12(conn, p):
    with conn.cursor() as cur:
        inserted = insert_batches(cur, p.new_bulk)
    conn.commit()
    return inserted


def s13(conn, p):
    """New customers first (the foreign key), then their transactions, all in one transaction."""
    with conn.cursor() as cur:
        inserted = cur.executemany(INSERT_CUSTOMER, [[c[col] for col in CUSTOMER_COLUMNS] for c in p.new_customers])
        inserted += insert_batches(cur, p.new_customer_transactions)
    conn.commit()
    return inserted


def s15(conn, p):
    """CREATE TABLE AS of the S09 aggregation without LIMIT. Returns the number of rows of the new table."""
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SUMMARY}")
        cur.execute(f"""
            CREATE TABLE {SUMMARY} AS
            SELECT EXTRACT(YEAR FROM t.transaction_date) AS y, EXTRACT(MONTH FROM t.transaction_date) AS m,
                   p.category, c.customer_age_group, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
            FROM transactions t
            JOIN customers c USING (customer_id)
            JOIN products p USING (product_id)
            GROUP BY 1, 2, 3, 4
        """)
        conn.commit()
        cur.execute(f"SELECT COUNT(*) FROM {SUMMARY}")
        return cur.fetchone()[0]


# UPDATE

def s16(conn, p):
    return execute_each(conn, "UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id = %s",
                        [(tid,) for tid in p.pk_ids])


def s17(conn, p):
    return execute_once(conn, """
        UPDATE transactions SET discount_pct = discount_pct + 1
        WHERE region = %s AND transaction_date >= %s AND transaction_date < %s
    """, (p.region, p.update_start, p.update_end))


def s18(conn, p):
    batches = [p.update_ids[i:i + BATCH] for i in range(0, len(p.update_ids), BATCH)]
    return execute_each_in(conn, "UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id IN ({})", batches)


def execute_each_in(conn, sql_template, batches):
    """Like execute_each, for statements with an `IN (...)` list sized to each batch."""
    affected = 0
    with conn.cursor() as cur:
        for batch in batches:
            affected += cur.execute(sql_template.format(", ".join(["%s"] * len(batch))), batch)
    conn.commit()
    return affected


def s19(conn, p):
    return execute_once(conn, """
        UPDATE transactions SET quantity = quantity + 1, discount_pct = discount_pct + 1
        WHERE discount_pct >= %s
    """, (p.discount_min,))


def s20(conn, p):
    return execute_once(conn, """
        UPDATE transactions t
        JOIN products p ON p.product_id = t.product_id
        SET t.discount_pct = t.discount_pct + 2
        WHERE p.category = %s
    """, (p.update_category,))


def s21(conn, p):
    return execute_once(conn, """
        UPDATE customers SET customer_segment = %s
        WHERE customer_id IN (
            SELECT customer_id FROM transactions
            GROUP BY customer_id HAVING SUM(sales_amount) > %s
        )
    """, (p.vip_segment, p.vip_threshold))


# DELETE

def s24(conn, p):
    return execute_each(conn, "DELETE FROM transactions WHERE transaction_id = %s",
                        [(tid,) for tid in p.new_single_ids])


def s25(conn, p):
    return execute_once(conn, """
        DELETE FROM transactions
        WHERE region = %s AND transaction_date >= %s AND transaction_date < %s
    """, (p.region, p.delete_start, p.delete_end))


def s26(conn, p):
    return execute_once(conn, "DELETE FROM transactions WHERE transaction_date < %s", (p.cutoff_date,))


def s27(conn, p):
    return execute_once(conn, """
        DELETE FROM transactions
        WHERE customer_id IN (SELECT customer_id FROM customers WHERE customer_segment = %s)
    """, (p.vip_segment,))


def s28(conn, p):
    return execute_once(conn, """
        DELETE FROM customers
        WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.customer_id = customers.customer_id)
    """, None)


TABLES = ["transactions", "products", "customers"]  # children first (foreign keys)


def s32(conn, p):
    """Delete every row from all 3 tables and drop the S15 table. Returns the number of rows left (must be 0)."""
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"DELETE FROM {table}")
        cur.execute(f"DROP TABLE IF EXISTS {SUMMARY}")
        conn.commit()
        left = 0
        for table in TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            left += cur.fetchone()[0]
    return left


SCENARIOS = {
    "S02": s02, "S03": s03, "S04": s04, "S05": s05, "S06": s06, "S07": s07, "S08": s08, "S09": s09,
    "S10": s10, "S11": s11, "S12": s12, "S13": s13, "S14": s14, "S15": s15, "S16": s16, "S17": s17,
    "S18": s18, "S19": s19, "S20": s20, "S21": s21, "S22": s22, "S23": s23, "S24": s24, "S25": s25,
    "S26": s26, "S27": s27, "S28": s28, "S29": s29, "S30": s30, "S31": s31, "S32": s32,
}
