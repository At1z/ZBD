"""MongoDB implementation of scenarios S02..S32 (see SCENARIOS.md; S01 is DataInjection.inject).

Contract used by benchmark.py: connect() -> ctx (the database), disconnect(ctx), SCENARIOS = {id: fn(ctx, params)}.
A scenario returns a list of tuples (READ) or the number of affected documents (CREATE / UPDATE / DELETE).
Embedded model: every transaction carries the customer (segment, age_group) and product (category, name) fields the
scenarios need (see DataInjection.embed), so there are no joins / $lookup stages. The price of this model shows in
S01 (bigger documents) and S10 / S11, where those fields have to be read before a new transaction is inserted.
Writes use the driver defaults (acknowledged, no journal wait), like the other databases' defaults.
"""
from datetime import datetime

from pymongo.errors import OperationFailure

from zbd import connections

BATCH = 1000          # ids per update_many in S18 / S21 (and per delete_many in S28)
INSERT_BATCH = 5000   # documents per insert_many
SUMMARY = "monthly_sales"  # collection created by S15


def connect():
    return connections.mongo()


def disconnect(db):
    db.client.close()


def day(value):
    """'2024-06-01' -> datetime (dates are stored as datetime at midnight)."""
    return datetime.fromisoformat(value)


def date_range(start, end):
    return {"$gte": day(start), "$lt": day(end)}


def embedded_docs(db, rows):
    """Transaction documents for new rows: the customer and product fields to embed are read from the collections."""
    from main import load_module
    injection = load_module("mongodb", "DataInjection")
    products = {d["_id"]: d for d in db.products.find({}, {"category": 1, "product_name": 1})}
    ids = list({row["customer_id"] for row in rows})
    customers = {d["_id"]: d for d in db.customers.find(
        {"_id": {"$in": ids}}, {"customer_segment": 1, "customer_age_group": 1})}
    return [injection.embed(injection.transaction_doc(row), customers[row["customer_id"]],
                            products[row["product_id"]]) for row in rows]


def read_by_key(db, ids):
    """One find_one per id. Returns [(documents found, sum of sales_amount)]."""
    found, total = 0, 0.0
    for tid in ids:
        doc = db.transactions.find_one({"_id": tid})
        if doc:
            found += 1
            total += doc["sales_amount"]
    return [(found, total)]


def aggregate(db, pipeline, scan=False):
    """scan=True forces a scan of the whole collection (hint $natural) instead of the date index: for a filter that
    matches a large part of the documents, the index plan reads them in date order, i.e. at random positions in the
    collection, which at 10M documents no longer fits in the cache (78 s instead of seconds)."""
    options = {"hint": {"$natural": 1}} if scan else {}
    return list(db.transactions.aggregate(pipeline, allowDiskUse=True, **options))


def medium(db):
    rows = aggregate(db, [
        {"$group": {"_id": "$product.category", "n": {"$sum": 1}, "revenue": {"$sum": "$sales_amount"},
                    "quantity": {"$sum": "$quantity"}}},
        {"$sort": {"revenue": -1, "_id": 1}},
    ])
    return [(d["_id"], d["n"], d["revenue"], d["quantity"]) for d in rows]


def heavy(db, since, with_quantity):
    rows = aggregate(db, [
        {"$match": {"transaction_date": {"$gte": day(since)}}},
        {"$group": {
            "_id": {"segment": "$customer.segment", "category": "$product.category", "region": "$region"},
            "n": {"$sum": 1},
            "revenue": {"$sum": "$sales_amount"},
            "quantity": {"$sum": "$quantity"},
        }},
        {"$match": {"revenue": {"$gt": 1000}}},
        {"$sort": {"revenue": -1, "_id.segment": 1, "_id.category": 1, "_id.region": 1}},
        {"$limit": 20},
    ], scan=True)
    extra = (lambda d: (d["quantity"],)) if with_quantity else (lambda d: ())
    return [(d["_id"]["segment"], d["_id"]["category"], d["_id"]["region"], d["n"], d["revenue"], *extra(d))
            for d in rows]


# READ

def s02(db, p):
    return read_by_key(db, p.pk_ids)


def s03(db, p):
    rows = aggregate(db, [
        {"$match": {"sales_channel": "Online"}},
        {"$group": {"_id": "$region", "n": {"$sum": 1}, "revenue": {"$sum": "$sales_amount"}}},
        {"$sort": {"_id": 1}},
    ])
    return [(d["_id"], d["n"], d["revenue"]) for d in rows]


def s04(db, p):
    return [(db.transactions.count_documents({"transaction_date": date_range(p.count_start, p.count_end)}),)]


def s05(db, p):
    return [row[:3] for row in medium(db)]  # category, n, revenue


def s06(db, p):
    rows = aggregate(db, [
        {"$group": {"_id": "$product_id", "name": {"$first": "$product.name"},
                    "revenue": {"$sum": "$sales_amount"}}},
        {"$sort": {"revenue": -1, "_id": 1}},
        {"$limit": 10},
    ])
    return [(d["_id"], d["name"], d["revenue"]) for d in rows]


def s07(db, p):
    rows, names = 0, set()
    for customer_id in p.customer_ids:
        for d in db.transactions.find({"customer_id": customer_id}, {"product.name": 1}):
            rows += 1
            names.add(d["product"]["name"])
    return [(rows, len(names))]


def s08(db, p):
    return heavy(db, p.heavy_since, with_quantity=False)


def s09(db, p):
    rows = aggregate(db, [
        {"$group": {
            "_id": {"y": {"$year": "$transaction_date"}, "m": {"$month": "$transaction_date"},
                    "category": "$product.category", "age": "$customer.age_group"},
            "revenue": {"$sum": "$sales_amount"},
        }},
        {"$sort": {"revenue": -1, "_id.y": 1, "_id.m": 1, "_id.category": 1, "_id.age": 1}},
        {"$limit": 20},
    ])
    return [(d["_id"]["y"], d["_id"]["m"], d["_id"]["category"], d["_id"]["age"], d["revenue"]) for d in rows]


def s14(db, p):
    return read_by_key(db, p.new_single_ids)


def s22(db, p):
    return medium(db)


def s23(db, p):
    return heavy(db, p.heavy_since, with_quantity=True)


def s29(db, p):
    return [(db.transactions.count_documents({}),)]


def s30(db, p):
    return medium(db)


def s31(db, p):
    return heavy(db, p.heavy_since, with_quantity=True)


# CREATE

def s10(db, p):
    inserted = 0
    for doc in embedded_docs(db, p.new_single):
        db.transactions.insert_one(doc)
        inserted += 1
    return inserted


def insert_in_batches(db, rows):
    """insert_many in batches of INSERT_BATCH; the fields to embed are read per batch. Returns how many were inserted."""
    inserted = 0
    for i in range(0, len(rows), INSERT_BATCH):
        docs = embedded_docs(db, rows[i:i + INSERT_BATCH])
        inserted += len(db.transactions.insert_many(docs, ordered=False).inserted_ids)
    return inserted


def s11(db, p):
    return insert_in_batches(db, p.new_batch)


def s12(db, p):
    return insert_in_batches(db, p.new_bulk)


def s13(db, p):
    """New customers first, then their transactions (a standalone server has no multi-document transaction)."""
    from main import load_module
    injection = load_module("mongodb", "DataInjection")
    customers = [injection.customer_doc(c) for c in p.new_customers]
    inserted = len(db.customers.insert_many(customers, ordered=False).inserted_ids)
    return inserted + insert_in_batches(db, p.new_customer_transactions)


def s15(db, p):
    """The S09 aggregation without $limit, written to a new collection with $out. Returns its number of documents."""
    db[SUMMARY].drop()
    aggregate(db, [
        {"$group": {
            "_id": {"y": {"$year": "$transaction_date"}, "m": {"$month": "$transaction_date"},
                    "category": "$product.category", "age": "$customer.age_group"},
            "n": {"$sum": 1},
            "revenue": {"$sum": "$sales_amount"},
        }},
        {"$out": SUMMARY},
    ])
    return db[SUMMARY].count_documents({})


# UPDATE

def s16(db, p):
    modified = 0
    for tid in p.pk_ids:
        modified += db.transactions.update_one({"_id": tid}, {"$inc": {"quantity": 1}}).modified_count
    return modified


def s17(db, p):
    return db.transactions.update_many(
        {"region": p.region, "transaction_date": date_range(p.update_start, p.update_end)},
        {"$inc": {"discount_pct": 1}},
    ).modified_count


def s18(db, p):
    modified = 0
    for i in range(0, len(p.update_ids), BATCH):
        modified += db.transactions.update_many(
            {"_id": {"$in": p.update_ids[i:i + BATCH]}}, {"$inc": {"quantity": 1}}).modified_count
    return modified


def s19(db, p):
    return db.transactions.update_many(
        {"discount_pct": {"$gte": p.discount_min}}, {"$inc": {"quantity": 1, "discount_pct": 1}}).modified_count


def s20(db, p):
    """The category is embedded in every transaction, so no join is needed."""
    return db.transactions.update_many(
        {"product.category": p.update_category}, {"$inc": {"discount_pct": 2}}).modified_count


def s21(db, p):
    """Customers above the spending threshold become VIP: the new segment goes to the customer and to the copy
    embedded in each of the customer's transactions (found through the customer_id index)."""
    ids = [d["_id"] for d in aggregate(db, [
        {"$group": {"_id": "$customer_id", "total": {"$sum": "$sales_amount"}}},
        {"$match": {"total": {"$gt": float(p.vip_threshold)}}},
    ])]
    modified = 0
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        modified += db.customers.update_many(
            {"_id": {"$in": batch}}, {"$set": {"customer_segment": p.vip_segment}}).modified_count
        db.transactions.update_many({"customer_id": {"$in": batch}}, {"$set": {"customer.segment": p.vip_segment}})
    return modified


# DELETE

def s24(db, p):
    deleted = 0
    for tid in p.new_single_ids:
        deleted += db.transactions.delete_one({"_id": tid}).deleted_count
    return deleted


def s25(db, p):
    return db.transactions.delete_many(
        {"region": p.region, "transaction_date": date_range(p.delete_start, p.delete_end)}).deleted_count


def s26(db, p):
    """Delete everything older than the cutoff (about half of the documents). Like in S08 the date index would read
    the documents at random positions of the collection, so a scan of the collection is tried first; if the server
    rejects the hint for a delete, the plain delete runs (the rejection happens before anything is deleted)."""
    older = {"transaction_date": {"$lt": day(p.cutoff_date)}}
    try:
        return db.transactions.delete_many(older, hint=[("$natural", 1)]).deleted_count
    except OperationFailure as error:
        print(f"  [mongodb] S26: hint not accepted ({error}), deleting without it", flush=True)
        return db.transactions.delete_many(older).deleted_count


def s27(db, p):
    """Every transaction of a VIP customer. The segment is embedded, so this is a scan of the collection."""
    return db.transactions.delete_many({"customer.segment": p.vip_segment}).deleted_count


def s28(db, p):
    """Customers without transactions: $lookup finds at most one transaction per customer (customer_id index)."""
    orphans = [d["_id"] for d in db.customers.aggregate([
        {"$lookup": {"from": "transactions", "localField": "_id", "foreignField": "customer_id",
                     "pipeline": [{"$limit": 1}, {"$project": {"_id": 1}}], "as": "any"}},
        {"$match": {"any": {"$size": 0}}},
        {"$project": {"_id": 1}},
    ], allowDiskUse=True)]
    deleted = 0
    for i in range(0, len(orphans), BATCH):
        deleted += db.customers.delete_many({"_id": {"$in": orphans[i:i + BATCH]}}).deleted_count
    return deleted


COLLECTIONS = ["transactions", "products", "customers"]


def s32(db, p):
    """Delete every document from all 3 collections and drop the S15 one. Returns the number left (must be 0)."""
    for name in COLLECTIONS:
        db[name].delete_many({})
    db[SUMMARY].drop()
    return sum(db[name].count_documents({}) for name in COLLECTIONS)


SCENARIOS = {
    "S02": s02, "S03": s03, "S04": s04, "S05": s05, "S06": s06, "S07": s07, "S08": s08, "S09": s09,
    "S10": s10, "S11": s11, "S12": s12, "S13": s13, "S14": s14, "S15": s15, "S16": s16, "S17": s17,
    "S18": s18, "S19": s19, "S20": s20, "S21": s21, "S22": s22, "S23": s23, "S24": s24, "S25": s25,
    "S26": s26, "S27": s27, "S28": s28, "S29": s29, "S30": s30, "S31": s31, "S32": s32,
}
