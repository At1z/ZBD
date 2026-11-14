import connections

# CRUD in one file (READ, UPDATE, DELETE); CREATE is in DataInjection.py.
# Same 3 read queries in every database (see README):
#   simple - 1 collection, medium - 2 ($lookup), heavy - 3 (two $lookup)


def lookup(collection, local_field, alias):
    return [
        {"$lookup": {"from": collection, "localField": local_field, "foreignField": "_id", "as": alias}},
        {"$unwind": f"${alias}"},
    ]


def simple():
    db = connections.mongo()
    pipeline = [
        {"$match": {"sales_channel": "Online"}},
        {"$group": {"_id": "$region", "n": {"$sum": 1}, "revenue": {"$sum": "$sales_amount"}}},
        {"$sort": {"_id": 1}},
    ]
    return [(d["_id"], d["n"], d["revenue"]) for d in db.transactions.aggregate(pipeline, allowDiskUse=True)]


def medium():
    db = connections.mongo()
    pipeline = [
        *lookup("products", "product_id", "product"),
        {"$group": {"_id": "$product.category", "n": {"$sum": 1}, "revenue": {"$sum": "$sales_amount"}}},
        {"$sort": {"revenue": -1, "_id": 1}},
    ]
    return [(d["_id"], d["n"], d["revenue"]) for d in db.transactions.aggregate(pipeline, allowDiskUse=True)]


def heavy():
    from datetime import datetime

    db = connections.mongo()
    pipeline = [
        {"$match": {"transaction_date": {"$gte": datetime(2025, 1, 1)}}},
        *lookup("customers", "customer_id", "customer"),
        *lookup("products", "product_id", "product"),
        {"$group": {
            "_id": {"segment": "$customer.customer_segment", "category": "$product.category", "region": "$region"},
            "n": {"$sum": 1},
            "revenue": {"$sum": "$sales_amount"},
        }},
        {"$match": {"revenue": {"$gt": 1000}}},
        {"$sort": {"revenue": -1, "_id.segment": 1, "_id.category": 1, "_id.region": 1}},
        {"$limit": 20},
    ]
    return [
        (d["_id"]["segment"], d["_id"]["category"], d["_id"]["region"], d["n"], d["revenue"])
        for d in db.transactions.aggregate(pipeline, allowDiskUse=True)
    ]


# UPDATE

UPDATE_BATCH_SIZE = 1000


def update(ids):
    """UPDATE: quantity + 1 for the given transaction ids. Returns the number of updated documents."""
    db = connections.mongo()
    updated = 0
    for i in range(0, len(ids), UPDATE_BATCH_SIZE):
        result = db.transactions.update_many({"_id": {"$in": ids[i:i + UPDATE_BATCH_SIZE]}}, {"$inc": {"quantity": 1}})
        updated += result.modified_count
    return updated


# DELETE

COLLECTIONS = ["transactions", "products", "customers"]


def delete_all():
    """DELETE every document from all 3 collections. Returns the number left (should be 0)."""
    db = connections.mongo()
    for name in COLLECTIONS:
        db[name].delete_many({})
    return sum(db[name].count_documents({}) for name in COLLECTIONS)
