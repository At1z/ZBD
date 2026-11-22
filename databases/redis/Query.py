"""Redis implementation of scenarios S02..S24 (see SCENARIOS.md; S01 is DataInjection.inject).

Contract used by benchmark.py: connect() -> ctx (the client), disconnect(ctx), SCENARIOS = {id: fn(ctx, params)}.
A scenario returns a list of tuples (READ) or the number of affected hashes (CREATE / UPDATE / DELETE).

Redis has no joins or aggregation commands, so scenarios with grouping / joins use a Lua script that runs inside
the server (AGGREGATE): the client reads the transaction ids page by page (by rank in the `idx:tx:date` sorted set,
or with SSCAN), the script reads the hashes of that page, looks up customers / products and returns the partial
groups, and the client merges the pages. Only ids go to the server and a few group rows come back, whatever
the dataset size; a page keeps every script call short (well below the server's busy-script limit).
"""
import json
from datetime import date

from zbd import connections

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

# GROUP BY inside the server for one page of transaction ids.
# ARGV[1] = mode, ARGV[2..] = transaction ids. Returns JSON: [[group key parts..., n, revenue, quantity], ...]
# (revenue as a string with all digits of the double, so merging pages loses nothing; quantity 0 for modes that do not sum it). Customers and products are looked
# up with HGET and cached for the duration of the call only, so memory stays small.
AGGREGATE = r"""
local mode = ARGV[1]
local FIELDS = {
    region  = {'region', 'sales_amount'},
    medium  = {'product_id', 'sales_amount', 'quantity'},
    product = {'product_id', 'sales_amount'},
    heavy   = {'customer_id', 'product_id', 'region', 'sales_amount', 'quantity'},
    age     = {'customer_id', 'product_id', 'transaction_date', 'sales_amount'},
}
local AMOUNT = {region = 2, medium = 2, product = 2, heavy = 4, age = 4}
local QUANTITY = {medium = 3, heavy = 5}
local fields, amount_at, quantity_at = FIELDS[mode], AMOUNT[mode], QUANTITY[mode]

local products, customers = {}, {}
local function lookup(cache, prefix, id, field)
    local value = cache[id]
    if value == nil then
        value = redis.call('HGET', prefix .. id, field)
        cache[id] = value
    end
    return value
end

local groups = {}
for i = 2, #ARGV do
    local f = redis.call('HMGET', 'transaction:' .. ARGV[i], unpack(fields))
    if f[amount_at] then
        local parts
        if mode == 'region' or mode == 'product' then
            parts = {f[1]}
        elseif mode == 'medium' then
            parts = {lookup(products, 'product:', f[1], 'category')}
        elseif mode == 'heavy' then
            parts = {lookup(customers, 'customer:', f[1], 'customer_segment'),
                     lookup(products, 'product:', f[2], 'category'), f[3]}
        else  -- age: year, month (YYYY-MM-DD), category, customer age group
            parts = {tonumber(string.sub(f[3], 1, 4)), tonumber(string.sub(f[3], 6, 7)),
                     lookup(products, 'product:', f[2], 'category'),
                     lookup(customers, 'customer:', f[1], 'customer_age_group')}
        end
        local key = table.concat(parts, '\1')
        local g = groups[key]
        if g == nil then
            g = {parts = parts, n = 0, revenue = 0, quantity = 0}
            groups[key] = g
        end
        g.n = g.n + 1
        g.revenue = g.revenue + tonumber(f[amount_at])
        if quantity_at then
            g.quantity = g.quantity + tonumber(f[quantity_at])
        end
    end
end

local rows = {}
for _, g in pairs(groups) do
    local row = {}
    for _, part in ipairs(g.parts) do
        row[#row + 1] = part
    end
    row[#row + 1] = g.n
    row[#row + 1] = string.format('%.17g', g.revenue)
    row[#row + 1] = g.quantity
    rows[#rows + 1] = row
end
return cjson.encode(rows)
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


def aggregate(r, mode, pages):
    """Run the AGGREGATE script on every page of ids; yields (key parts, n, revenue, quantity) of each page's groups."""
    script = r.register_script(AGGREGATE)
    for ids in pages:
        if not ids:
            continue
        for *key, n, revenue, quantity in json.loads(script(keys=[], args=[mode, *ids])) or []:
            yield tuple(key), int(n), float(revenue), int(quantity)


def merge(groups):
    """Add up the partial groups of all pages: key -> [n, revenue, quantity]."""
    totals = {}
    for key, n, revenue, quantity in groups:
        t = totals.setdefault(key, [0, 0.0, 0])
        t[0] += n
        t[1] += revenue
        t[2] += quantity
    return totals


def product_field(r, field):
    """product_id -> field, for the whole (small) products dictionary."""
    ids = sorted(r.smembers("ids:products"))
    pipe = r.pipeline(transaction=False)
    for pid in ids:
        pipe.hget(f"product:{pid}", field)
    return dict(zip(ids, pipe.execute()))


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


# aggregations (done in the server by the AGGREGATE script, merged here)

def group_medium(r):
    totals = merge(aggregate(r, "medium", date_pages(r)))  # category -> [n, revenue, quantity]
    rows = [(category, *t) for (category,), t in totals.items()]
    return sorted(rows, key=lambda row: (-row[2], row[0]))  # category, n, revenue, quantity


def group_heavy(r, since):
    totals = merge(aggregate(r, "heavy", date_pages(r, lo=ordinal(since))))  # (segment, category, region) -> [...]
    rows = [(*key, *t) for key, t in totals.items() if t[1] > 1000]  # segment, category, region, n, revenue, quantity
    return sorted(rows, key=lambda row: (-row[4], row[0], row[1], row[2]))[:20]


# READ

def s02(r, p):
    return read_by_key(r, p.pk_ids)


def s03(r, p):
    totals = merge(aggregate(r, "region", set_pages(r, "idx:tx:channel:Online")))  # region -> [n, revenue, _]
    return [(region, n, revenue) for (region,), (n, revenue, _) in sorted(totals.items())]


def s04(r, p):
    return [(r.zcount(DATE_INDEX, ordinal(p.count_start), ordinal(p.count_end) - 1),)]


def s05(r, p):
    return [row[:3] for row in group_medium(r)]  # category, n, revenue


def s06(r, p):
    name_of = product_field(r, "product_name")
    totals = merge(aggregate(r, "product", date_pages(r)))  # product_id -> [n, revenue, _]
    top = sorted(((pid, t[1]) for (pid,), t in totals.items()), key=lambda item: (-item[1], item[0]))[:10]
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
    totals = merge(aggregate(r, "age", date_pages(r)))  # (year, month, category, age group) -> [n, revenue, _]
    rows = [(int(y), int(m), category, age, t[1]) for (y, m, category, age), t in totals.items()]
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
