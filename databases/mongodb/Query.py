"""MongoDB implementation of scenarios S02..S24 (see SCENARIOS.md; S01 is DataInjection.inject).

Contract used by benchmark.py: connect() -> ctx (the database), disconnect(ctx), SCENARIOS = {id: fn(ctx, params)}.
A scenario returns a list of tuples (READ) or the number of affected documents (CREATE / UPDATE / DELETE).
Joins are `$lookup` stages (collections hold references: customer_id / product_id -> _id).
Writes use the driver defaults (acknowledged, no journal wait), like the other databases' defaults.
"""
from datetime import datetime

from zbd import connections

BATCH = 1000          # ids per update_many in S15
INSERT_BATCH = 5000   # documents per insert_many


def connect():
    return connections.mongo()


def disconnect(db):
    db.client.close()


def day(value):
    """'2024-06-01' -> datetime (dates are stored as datetime at midnight)."""
    return datetime.fromisoformat(value)


def date_range(start, end):
    return {"$gte": day(start), "$lt": day(end)}


def lookup(collection, local_field, alias):
    return [
        {"$lookup": {"from": collection, "localField": local_field, "foreignField": "_id", "as": alias}},
        {"$unwind": f"${alias}"},
    ]


def transaction_docs(rows):
    # reuse the converter of the loader so inserted documents look exactly like loaded ones
    from main import load_module
    convert = load_module("mongodb", "DataInjection").transaction_doc
    return [convert(row) for row in rows]


def read_by_key(db, ids):
    """One find_one per id. Returns [(documents found, sum of sales_amount)]."""
    found, total = 0, 0.0
    for tid in ids:
        doc = db.transactions.find_one({"_id": tid})
        if doc:
            found += 1
            total += doc["sales_amount"]
    return [(found, total)]


def aggregate(db, pipeline):
    return list(db.transactions.aggregate(pipeline, allowDiskUse=True))


def medium(db):
    # Group by product_id first (120 groups), join the small products collection on the groups only, then group by
    # category. Same result as joining every transaction, but 10M $lookup stages become 120 (the way S06 does it).
    rows = aggregate(db, [
        {"$group": {"_id": "$product_id", "n": {"$sum": 1}, "revenue": {"$sum": "$sales_amount"},
                    "quantity": {"$sum": "$quantity"}}},
        *lookup("products", "_id", "product"),
        {"$group": {"_id": "$product.category", "n": {"$sum": "$n"}, "revenue": {"$sum": "$revenue"},
                    "quantity": {"$sum": "$quantity"}}},
        {"$sort": {"revenue": -1, "_id": 1}},
    ])
    return [(d["_id"], d["n"], d["revenue"], d["quantity"]) for d in rows]


def heavy(db, since, with_quantity):
    rows = aggregate(db, [
        {"$match": {"transaction_date": {"$gte": day(since)}}},
        *lookup("customers", "customer_id", "customer"),
        *lookup("products", "product_id", "product"),
        {"$group": {
            "_id": {"segment": "$customer.customer_segment", "category": "$product.category", "region": "$region"},
            "n": {"$sum": 1},
            "revenue": {"$sum": "$sales_amount"},
            "quantity": {"$sum": "$quantity"},
        }},
        {"$match": {"revenue": {"$gt": 1000}}},
        {"$sort": {"revenue": -1, "_id.segment": 1, "_id.category": 1, "_id.region": 1}},
        {"$limit": 20},
    ])
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
        {"$group": {"_id": "$product_id", "revenue": {"$sum": "$sales_amount"}}},
        {"$sort": {"revenue": -1, "_id": 1}},
        {"$limit": 10},
        {"$lookup": {"from": "products", "localField": "_id", "foreignField": "_id", "as": "product"}},
        {"$unwind": "$product"},
        {"$sort": {"revenue": -1, "_id": 1}},
    ])
    return [(d["_id"], d["product"]["product_name"], d["revenue"]) for d in rows]


def s07(db, p):
    rows, names = 0, set()
    for customer_id in p.customer_ids:
        for d in db.transactions.aggregate([
            {"$match": {"customer_id": customer_id}},
            *lookup("products", "product_id", "product"),
            {"$project": {"name": "$product.product_name"}},
        ]):
            rows += 1
            names.add(d["name"])
    return [(rows, len(names))]


def s08(db, p):
    return heavy(db, p.heavy_since, with_quantity=False)


def s09(db, p):
    rows = aggregate(db, [
        *lookup("customers", "customer_id", "customer"),
        *lookup("products", "product_id", "product"),
        {"$group": {
            "_id": {"y": {"$year": "$transaction_date"}, "m": {"$month": "$transaction_date"},
                    "category": "$product.category", "age": "$customer.customer_age_group"},
            "revenue": {"$sum": "$sales_amount"},
        }},
        {"$sort": {"revenue": -1, "_id.y": 1, "_id.m": 1, "_id.category": 1, "_id.age": 1}},
        {"$limit": 20},
    ])
    return [(d["_id"]["y"], d["_id"]["m"], d["_id"]["category"], d["_id"]["age"], d["revenue"]) for d in rows]


def s12(db, p):
    return read_by_key(db, p.new_single_ids)


def s16(db, p):
    return medium(db)


def s17(db, p):
    return heavy(db, p.heavy_since, with_quantity=True)


def s21(db, p):
    return [(db.transactions.count_documents({}),)]


def s22(db, p):
    return medium(db)


def s23(db, p):
    return heavy(db, p.heavy_since, with_quantity=True)


# CREATE

def s10(db, p):
    inserted = 0
    for doc in transaction_docs(p.new_single):
        db.transactions.insert_one(doc)
        inserted += 1
    return inserted


def s11(db, p):
    docs = transaction_docs(p.new_batch)
    inserted = 0
    for i in range(0, len(docs), INSERT_BATCH):
        inserted += len(db.transactions.insert_many(docs[i:i + INSERT_BATCH], ordered=False).inserted_ids)
    return inserted


# UPDATE

def s13(db, p):
    modified = 0
    for tid in p.pk_ids:
        modified += db.transactions.update_one({"_id": tid}, {"$inc": {"quantity": 1}}).modified_count
    return modified


def s14(db, p):
    return db.transactions.update_many(
        {"region": p.region, "transaction_date": date_range(p.update_start, p.update_end)},
        {"$inc": {"discount_pct": 1}},
    ).modified_count


def s15(db, p):
    modified = 0
    for i in range(0, len(p.update_ids), BATCH):
        modified += db.transactions.update_many(
            {"_id": {"$in": p.update_ids[i:i + BATCH]}}, {"$inc": {"quantity": 1}}).modified_count
    return modified


# DELETE

def s18(db, p):
    deleted = 0
    for tid in p.new_single_ids:
        deleted += db.transactions.delete_one({"_id": tid}).deleted_count
    return deleted


def s19(db, p):
    return db.transactions.delete_many(
        {"region": p.region, "transaction_date": date_range(p.delete_start, p.delete_end)}).deleted_count


def s20(db, p):
    return db.transactions.delete_many({"transaction_date": {"$lt": day(p.cutoff_date)}}).deleted_count


COLLECTIONS = ["transactions", "products", "customers"]


def s24(db, p):
    """Delete every document from all 3 collections. Returns the number left (must be 0)."""
    for name in COLLECTIONS:
        db[name].delete_many({})
    return sum(db[name].count_documents({}) for name in COLLECTIONS)


SCENARIOS = {
    "S02": s02, "S03": s03, "S04": s04, "S05": s05, "S06": s06, "S07": s07, "S08": s08, "S09": s09,
    "S10": s10, "S11": s11, "S12": s12, "S13": s13, "S14": s14, "S15": s15, "S16": s16, "S17": s17,
    "S18": s18, "S19": s19, "S20": s20, "S21": s21, "S22": s22, "S23": s23, "S24": s24,
}
