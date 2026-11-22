"""Redis implementation of scenarios S02..S24 (see SCENARIOS.md; S01 is DataInjection.inject).

Contract used by benchmark.py: connect() -> ctx (the client), disconnect(ctx), SCENARIOS = {id: fn(ctx, params)}.
A scenario returns a list of tuples (READ) or the number of affected hashes (CREATE / UPDATE / DELETE).

Redis has no joins or aggregation, so they are done in the client. To keep the client's memory flat whatever
the dataset size, transactions are never collected into one big list: ids are read page by page (by rank in
the `idx:tx:date` sorted set, or with SSCAN) and aggregated on the fly. Customers and products are looked up
in small caches.
"""
from datetime import date

import connections

DATE_INDEX = "idx:tx:date"   # sorted set: member = transaction id, score = date ordinal
PAGE = 50_000                # ids read from an index at once
CHUNK = 10_000               # commands per pipeline

# HINCRBY would create a missing hash, so change the field only when the transaction exists
INCREMENT_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('HINCRBY', KEYS[1], ARGV[1], ARGV[2])
    return 1
end
return 0
"""


def connect():
    return connections.redis_client()


def disconnect(r):
    r.close()


def ordinal(value):
    return date.fromisoformat(value).toordinal()


# reading helpers

def date_pages(r, lo=None, hi=None):
    """Pages of transaction ids ordered by date; lo / hi are inclusive date ordinals (None = open).
    Pages are read by rank, so reading a page costs the same however far into the index it is."""
    first = 0 if lo is None else r.zcount(DATE_INDEX, "-inf", f"({lo}")
    end = r.zcard(DATE_INDEX) if hi is None else r.zcount(DATE_INDEX, "-inf", hi)  # exclusive rank bound
    for start in range(first, end, PAGE):
        yield r.zrange(DATE_INDEX, start, min(start + PAGE, end) - 1)


def set_pages(r, key):
    """Pages of ids of a set, read with SSCAN."""
    page = []
    for member in r.sscan_iter(key, count=CHUNK):
        page.append(member)
        if len(page) == PAGE:
            yield page
            page = []
    if page:
        yield page


def read_fields(r, ids, fields):
    """Yield dict(field -> value) for the given transaction ids, read through pipelines."""
    for i in range(0, len(ids), CHUNK):
        pipe = r.pipeline(transaction=False)
        for tid in ids[i:i + CHUNK]:
            pipe.hmget(f"transaction:{tid}", fields)
        for values in pipe.execute():
            yield dict(zip(fields, values))


def scan(r, pages, fields):
    for ids in pages:
        yield from read_fields(r, ids, fields)


def product_field(r, field):
    """product_id -> field, for the whole (small) products dictionary."""
    ids = sorted(r.smembers("ids:products"))
    pipe = r.pipeline(transaction=False)
    for pid in ids:
        pipe.hget(f"product:{pid}", field)
    return dict(zip(ids, pipe.execute()))


class CustomerField:
    """customer_id -> field, filled lazily (customers are many, only the referenced ones are read)."""

    def __init__(self, r, field):
        self.r, self.field, self.cache = r, field, {}

    def load(self, ids):
        missing = list(set(ids) - self.cache.keys())
        for i in range(0, len(missing), CHUNK):
            part = missing[i:i + CHUNK]
            pipe = self.r.pipeline(transaction=False)
            for cid in part:
                pipe.hget(f"customer:{cid}", self.field)
            self.cache.update(zip(part, pipe.execute()))

    def __getitem__(self, cid):
        return self.cache[cid]


def read_by_key(r, ids):
    """One HGETALL per id. Returns [(hashes found, sum of sales_amount)]."""
    found, total = 0, 0.0
    for tid in ids:
        row = r.hgetall(f"transaction:{tid}")
        if row:
            found += 1
            total += float(row["sales_amount"])
    return [(found, total)]


def batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def by_page(r, pages, fields):
    """Like scan, but yields one list of dicts per page (for aggregations that look up customers per page)."""
    for ids in pages:
        yield list(read_fields(r, ids, fields))


# aggregations (done in the client)

def group_medium(r):
    category_of = product_field(r, "category")
    totals = {}  # category -> [n, revenue, quantity]
    for tx in scan(r, date_pages(r), ["product_id", "sales_amount", "quantity"]):
        t = totals.setdefault(category_of[tx["product_id"]], [0, 0.0, 0])
        t[0] += 1
        t[1] += float(tx["sales_amount"])
        t[2] += int(tx["quantity"])
    rows = [(category, *t) for category, t in totals.items()]
    return sorted(rows, key=lambda row: (-row[2], row[0]))  # category, n, revenue, quantity


def group_heavy(r, since):
    category_of = product_field(r, "category")
    segment_of = CustomerField(r, "customer_segment")
    totals = {}  # (segment, category, region) -> [n, revenue, quantity]
    fields = ["customer_id", "product_id", "region", "sales_amount", "quantity"]
    for batch in by_page(r, date_pages(r, lo=ordinal(since)), fields):
        segment_of.load(tx["customer_id"] for tx in batch)
        for tx in batch:
            key = (segment_of[tx["customer_id"]], category_of[tx["product_id"]], tx["region"])
            t = totals.setdefault(key, [0, 0.0, 0])
            t[0] += 1
            t[1] += float(tx["sales_amount"])
            t[2] += int(tx["quantity"])
    rows = [(*key, *t) for key, t in totals.items() if t[1] > 1000]  # segment, category, region, n, revenue, quantity
    return sorted(rows, key=lambda row: (-row[4], row[0], row[1], row[2]))[:20]


# READ

def s02(r, p):
    return read_by_key(r, p.pk_ids)


def s03(r, p):
    totals = {}
    for tx in scan(r, set_pages(r, "idx:tx:channel:Online"), ["region", "sales_amount"]):
        t = totals.setdefault(tx["region"], [0, 0.0])
        t[0] += 1
        t[1] += float(tx["sales_amount"])
    return [(region, n, revenue) for region, (n, revenue) in sorted(totals.items())]


def s04(r, p):
    return [(r.zcount(DATE_INDEX, ordinal(p.count_start), ordinal(p.count_end) - 1),)]


def s05(r, p):
    return [row[:3] for row in group_medium(r)]  # category, n, revenue


def s06(r, p):
    name_of = product_field(r, "product_name")
    revenue_of = {}
    for tx in scan(r, date_pages(r), ["product_id", "sales_amount"]):
        revenue_of[tx["product_id"]] = revenue_of.get(tx["product_id"], 0.0) + float(tx["sales_amount"])
    top = sorted(revenue_of.items(), key=lambda item: (-item[1], item[0]))[:10]
    return [(pid, name_of[pid], revenue) for pid, revenue in top]


def s07(r, p):
    name_of = product_field(r, "product_name")
    rows, names = 0, set()
    for customer_id in p.customer_ids:
        ids = list(r.smembers(f"idx:tx:customer:{customer_id}"))
        for tx in read_fields(r, ids, ["product_id"]):
            rows += 1
            names.add(name_of[tx["product_id"]])
    return [(rows, len(names))]


def s08(r, p):
    return [row[:5] for row in group_heavy(r, p.heavy_since)]  # without quantity


def s09(r, p):
    category_of = product_field(r, "category")
    age_of = CustomerField(r, "customer_age_group")
    totals = {}  # (year, month, category, age group) -> revenue
    fields = ["customer_id", "product_id", "transaction_date", "sales_amount"]
    for batch in by_page(r, date_pages(r), fields):
        age_of.load(tx["customer_id"] for tx in batch)
        for tx in batch:
            d = tx["transaction_date"]  # YYYY-MM-DD
            key = (int(d[:4]), int(d[5:7]), category_of[tx["product_id"]], age_of[tx["customer_id"]])
            totals[key] = totals.get(key, 0.0) + float(tx["sales_amount"])
    rows = [(*key, revenue) for key, revenue in totals.items()]
    return sorted(rows, key=lambda row: (-row[4], row[0], row[1], row[2], row[3]))[:20]


def s12(r, p):
    return read_by_key(r, p.new_single_ids)


def s16(r, p):
    return group_medium(r)


def s17(r, p):
    return group_heavy(r, p.heavy_since)


def s21(r, p):
    return [(r.zcard(DATE_INDEX),)]


def s22(r, p):
    return group_medium(r)


def s23(r, p):
    return group_heavy(r, p.heavy_since)


# CREATE

def add_transaction(pipe, row):
    """Hash plus the indexes of inject(): customer, channel and date."""
    tid = row["transaction_id"]
    pipe.hset(f"transaction:{tid}", mapping=row)
    pipe.sadd(f"idx:tx:customer:{row['customer_id']}", tid)
    pipe.sadd(f"idx:tx:channel:{row['sales_channel']}", tid)
    pipe.zadd(DATE_INDEX, {tid: ordinal(row["transaction_date"])})


def s10(r, p):
    for row in p.new_single:
        pipe = r.pipeline(transaction=True)  # one round trip per row (MULTI / EXEC)
        add_transaction(pipe, row)
        pipe.execute()
    return len(p.new_single)


def s11(r, p):
    for rows in batches(p.new_batch, CHUNK):
        pipe = r.pipeline(transaction=False)
        for row in rows:
            add_transaction(pipe, row)
        pipe.execute()
    return len(p.new_batch)


# UPDATE

def s13(r, p):
    increment = r.register_script(INCREMENT_IF_EXISTS)
    return sum(increment(keys=[f"transaction:{tid}"], args=["quantity", 1]) for tid in p.pk_ids)


def increment_many(r, ids, field):
    """field + 1 for every existing transaction in `ids`, through pipelines. Returns how many were changed."""
    increment = r.register_script(INCREMENT_IF_EXISTS)
    updated = 0
    for part in batches(ids, CHUNK):
        pipe = r.pipeline(transaction=False)
        for tid in part:
            increment(keys=[f"transaction:{tid}"], args=[field, 1], client=pipe)
        updated += sum(pipe.execute())
    return updated


def ids_in_region(r, lo, hi, region):
    """Ids of transactions of one region in a date range (the date index narrows it down, the region is
    checked on the hash: there is no region index)."""
    ids = []
    for page in date_pages(r, lo=lo, hi=hi):
        for tid, tx in zip(page, read_fields(r, page, ["region"])):
            if tx["region"] == region:
                ids.append(tid)
    return ids


def s14(r, p):
    ids = ids_in_region(r, ordinal(p.update_start), ordinal(p.update_end) - 1, p.region)
    return increment_many(r, ids, "discount_pct")


def s15(r, p):
    return increment_many(r, p.update_ids, "quantity")


# DELETE

def delete_transactions(r, ids):
    """Delete the hashes and remove them from every index. Returns the number of deleted hashes."""
    deleted = 0
    for part in batches(ids, CHUNK):
        pipe = r.pipeline(transaction=False)
        for tid in part:
            pipe.hmget(f"transaction:{tid}", ["customer_id", "sales_channel"])
        found = pipe.execute()
        pipe = r.pipeline(transaction=False)
        for tid, (customer_id, channel) in zip(part, found):
            if customer_id is None:  # already gone
                continue
            pipe.delete(f"transaction:{tid}")
            pipe.srem(f"idx:tx:customer:{customer_id}", tid)
            pipe.srem(f"idx:tx:channel:{channel}", tid)
            pipe.zrem(DATE_INDEX, tid)
        deleted += sum(pipe.execute()[0::4])  # every transaction queued DEL, SREM, SREM, ZREM
    return deleted


def s18(r, p):
    return sum(delete_transactions(r, [tid]) for tid in p.new_single_ids)


def s19(r, p):
    ids = ids_in_region(r, ordinal(p.delete_start), ordinal(p.delete_end) - 1, p.region)
    return delete_transactions(r, ids)


def s20(r, p):
    """Delete everything older than the cutoff. Always takes the oldest page, so there is no growing offset."""
    deleted = 0
    while True:
        ids = r.zrangebyscore(DATE_INDEX, "-inf", f"({ordinal(p.cutoff_date)}", start=0, num=PAGE)
        if not ids:
            return deleted
        deleted += delete_transactions(r, ids)


def s24(r, p):
    """FLUSHDB: Redis has no tables, everything lives in one keyspace. Returns the number of keys left."""
    r.flushdb()
    return r.dbsize()


SCENARIOS = {
    "S02": s02, "S03": s03, "S04": s04, "S05": s05, "S06": s06, "S07": s07, "S08": s08, "S09": s09,
    "S10": s10, "S11": s11, "S12": s12, "S13": s13, "S14": s14, "S15": s15, "S16": s16, "S17": s17,
    "S18": s18, "S19": s19, "S20": s20, "S21": s21, "S22": s22, "S23": s23, "S24": s24,
}
