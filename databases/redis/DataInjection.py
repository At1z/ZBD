from datetime import date

import redis

from zbd import connections

BATCH_SIZE = 10_000

# Key layout (Redis has no joins, so references are resolved by the client):
#   customer:{id}, product:{id}, transaction:{id}      hashes (transaction holds customer_id / product_id)
#   ids:customers, ids:products                        sets of all ids
#   idx:tx:customer:{id}, idx:tx:channel:{c}           sets of transaction ids
#   idx:tx:date                                        sorted set, score = date ordinal
# Only indexes that some scenario uses (SCENARIOS.md); no product / region index (MySQL only keeps the one InnoDB creates for its foreign key).


def inject(dataset):
    r = connections.redis_client()
    r.flushdb()

    pipe = r.pipeline(transaction=False)
    for row in dataset.iter_products():
        pipe.hset(f"product:{row['product_id']}", mapping=row)
        pipe.sadd("ids:products", row["product_id"])
    pipe.execute()
    print("[redis] products loaded")

    pipe = r.pipeline(transaction=False)
    for i, row in enumerate(dataset.iter_customers(), 1):
        pipe.hset(f"customer:{row['customer_id']}", mapping=row)
        pipe.sadd("ids:customers", row["customer_id"])
        if i % BATCH_SIZE == 0:
            pipe.execute()
    pipe.execute()
    print("[redis] customers loaded")

    pipe = r.pipeline(transaction=False)
    for i, row in enumerate(dataset.iter_transactions(), 1):
        tid = row["transaction_id"]
        pipe.hset(f"transaction:{tid}", mapping=row)
        pipe.sadd(f"idx:tx:customer:{row['customer_id']}", tid)
        pipe.sadd(f"idx:tx:channel:{row['sales_channel']}", tid)
        pipe.zadd("idx:tx:date", {tid: date.fromisoformat(row["transaction_date"]).toordinal()})
        if i % BATCH_SIZE == 0:
            pipe.execute()
    pipe.execute()
    print("[redis] transactions loaded")


def check(expected, quiet=False):
    """Health query: key counts plus a manual join of one transaction with its customer and product."""
    try:
        r = connections.redis_client()
        counts = {
            "customers": r.scard("ids:customers"),
            "products": r.scard("ids:products"),
            "transactions": r.zcard("idx:tx:date"),
        }
        joined = 0
        first = r.zrange("idx:tx:date", 0, 0)
        if first:
            tx = r.hgetall(f"transaction:{first[0]}")
            joined = int(bool(r.hgetall(f"customer:{tx['customer_id']}")) and bool(r.hgetall(f"product:{tx['product_id']}")))
    except redis.RedisError:
        return False
    ok = counts == expected and joined == 1
    if not quiet:
        print(f"[redis] {counts}, manual join of 1 transaction: {joined} -> {'OK' if ok else 'MISSING DATA'}")
    return ok
