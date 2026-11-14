from datetime import date

import connections

# Same 3 queries in every database (see README). Redis has no joins or aggregation, so the
# "join" is done in the client: transactions are read in batches (pipeline) and combined
# with product / customer hashes held in memory.
#   simple - transactions only, medium - + products, heavy - + customers

CHUNK = 10_000


def hmget_all(r, ids, fields):
    """Yield dict(field -> value) for the given transaction ids, read through a pipeline."""
    for i in range(0, len(ids), CHUNK):
        pipe = r.pipeline(transaction=False)
        for tid in ids[i:i + CHUNK]:
            pipe.hmget(f"transaction:{tid}", fields)
        for values in pipe.execute():
            yield dict(zip(fields, values))


def products(r):
    ids = sorted(r.smembers("ids:products"))
    pipe = r.pipeline(transaction=False)
    for pid in ids:
        pipe.hget(f"product:{pid}", "category")
    return dict(zip(ids, pipe.execute()))


def simple():
    r = connections.redis_client()
    ids = list(r.smembers("idx:tx:channel:Online"))
    totals = {}
    for tx in hmget_all(r, ids, ["region", "sales_amount"]):
        n, revenue = totals.get(tx["region"], (0, 0.0))
        totals[tx["region"]] = (n + 1, revenue + float(tx["sales_amount"]))
    return [(region, n, revenue) for region, (n, revenue) in sorted(totals.items())]


def medium():
    r = connections.redis_client()
    category_of = products(r)
    ids = r.zrange("idx:tx:date", 0, -1)
    totals = {}
    for tx in hmget_all(r, ids, ["product_id", "sales_amount"]):
        category = category_of[tx["product_id"]]
        n, revenue = totals.get(category, (0, 0.0))
        totals[category] = (n + 1, revenue + float(tx["sales_amount"]))
    rows = [(category, n, revenue) for category, (n, revenue) in totals.items()]
    return sorted(rows, key=lambda row: (-row[2], row[0]))


def heavy():
    r = connections.redis_client()
    category_of = products(r)
    segment_of = {}  # customer_id -> segment, filled lazily
    ids = r.zrangebyscore("idx:tx:date", date(2025, 1, 1).toordinal(), "+inf")
    totals = {}
    for i in range(0, len(ids), CHUNK):
        batch = list(hmget_all(r, ids[i:i + CHUNK], ["customer_id", "product_id", "region", "sales_amount"]))
        missing = list({tx["customer_id"] for tx in batch} - segment_of.keys())
        pipe = r.pipeline(transaction=False)
        for cid in missing:
            pipe.hget(f"customer:{cid}", "customer_segment")
        segment_of.update(zip(missing, pipe.execute()))
        for tx in batch:
            key = (segment_of[tx["customer_id"]], category_of[tx["product_id"]], tx["region"])
            n, revenue = totals.get(key, (0, 0.0))
            totals[key] = (n + 1, revenue + float(tx["sales_amount"]))
    rows = [(*key, n, revenue) for key, (n, revenue) in totals.items() if revenue > 1000]
    return sorted(rows, key=lambda row: (-row[4], row[0], row[1], row[2]))[:20]


# UPDATE

UPDATE_BATCH_SIZE = 10_000


def update(ids):
    """UPDATE: quantity + 1 for the given transaction ids. Returns the number of updated hashes."""
    r = connections.redis_client()
    updated = 0
    for i in range(0, len(ids), UPDATE_BATCH_SIZE):
        pipe = r.pipeline(transaction=False)
        for tid in ids[i:i + UPDATE_BATCH_SIZE]:
            pipe.hincrby(f"transaction:{tid}", "quantity", 1)
        updated += len(pipe.execute())
    return updated


# DELETE

def delete_all():
    """Delete every key (FLUSHDB: Redis has no tables, everything lives in one keyspace).
    Returns the number of keys left (should be 0)."""
    r = connections.redis_client()
    r.flushdb()
    return r.dbsize()
