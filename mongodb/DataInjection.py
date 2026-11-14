from datetime import datetime

from pymongo import MongoClient
from pymongo.errors import PyMongoError

BATCH_SIZE = 50_000
URI = "mongodb://admin:admin@localhost:27017"


def connect():
    return MongoClient(URI, serverSelectionTimeoutMS=5000)["zbd"]


def customer_doc(row):
    return {
        "_id": row["customer_id"],
        "customer_gender": row["customer_gender"],
        "customer_age_group": row["customer_age_group"],
        "customer_segment": row["customer_segment"],
    }


def product_doc(row):
    return {
        "_id": row["product_id"],
        "product_name": row["product_name"],
        "category": row["category"],
        "brand": row["brand"],
        "unit_price": float(row["unit_price"]),
    }


def transaction_doc(row):
    return {
        "_id": row["transaction_id"],
        "transaction_date": datetime.fromisoformat(row["transaction_date"]),
        "customer_id": row["customer_id"],  # reference to customers._id (use $lookup)
        "product_id": row["product_id"],  # reference to products._id (use $lookup)
        "quantity": int(row["quantity"]),
        "discount_pct": int(row["discount_pct"]),
        "sales_amount": float(row["sales_amount"]),
        "payment_method": row["payment_method"],
        "sales_channel": row["sales_channel"],
        "region": row["region"],
    }


def insert_docs(collection, docs):
    batch = []
    for doc in docs:
        batch.append(doc)
        if len(batch) == BATCH_SIZE:
            collection.insert_many(batch, ordered=False)
            batch = []
    if batch:
        collection.insert_many(batch, ordered=False)


def inject(dataset):
    db = connect()
    collections = [
        ("customers", (customer_doc(r) for r in dataset.iter_customers())),
        ("products", (product_doc(r) for r in dataset.iter_products())),
        ("transactions", (transaction_doc(r) for r in dataset.iter_transactions())),
    ]
    for name, docs in collections:
        db[name].drop()
        insert_docs(db[name], docs)
        print(f"[mongodb] {name} loaded")
    db.transactions.create_index("customer_id")
    db.transactions.create_index("product_id")
    db.transactions.create_index("transaction_date")


def check(expected, quiet=False):
    """Health query: document counts plus a $lookup on a sample of transactions."""
    sample_size = 1000
    try:
        db = connect()
        counts = {name: db[name].count_documents({}) for name in expected}
        joined = next(db.transactions.aggregate([
            {"$limit": sample_size},
            {"$lookup": {"from": "customers", "localField": "customer_id", "foreignField": "_id", "as": "customer"}},
            {"$lookup": {"from": "products", "localField": "product_id", "foreignField": "_id", "as": "product"}},
            {"$match": {"customer": {"$size": 1}, "product": {"$size": 1}}},
            {"$count": "n"},
        ]), {"n": 0})["n"]
    except PyMongoError:
        return False
    ok = counts == expected and joined == min(sample_size, expected["transactions"])
    if not quiet:
        print(f"[mongodb] {counts}, $lookup on a {sample_size}-transaction sample matched: {joined} -> {'OK' if ok else 'MISSING DATA'}")
    return ok
