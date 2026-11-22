import sys
from datetime import datetime

from pymongo.errors import PyMongoError

from zbd import connections

BATCH_SIZE = 50_000


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
        "customer_id": row["customer_id"],  # also kept as a reference to customers._id
        "product_id": row["product_id"],  # also kept as a reference to products._id
        "quantity": int(row["quantity"]),
        "discount_pct": int(row["discount_pct"]),
        "sales_amount": float(row["sales_amount"]),
        "payment_method": row["payment_method"],
        "sales_channel": row["sales_channel"],
        "region": row["region"],
    }


def embed_values(doc, segment, age_group, category, product_name):
    """Embed the customer and product fields the scenarios read, so queries need no $lookup:
    transaction = {..., customer: {segment, age_group}, product: {category, name}}."""
    doc["customer"] = {"segment": segment, "age_group": age_group}
    doc["product"] = {"category": category, "name": product_name}
    return doc


def embed(doc, customer, product):
    """Same, from a customer / product document (used by S10 / S11 for new transactions)."""
    return embed_values(doc, customer["customer_segment"], customer["customer_age_group"],
                        product["category"], product["product_name"])


def transaction_docs(dataset):
    """Transaction documents with embedded fields. Customers are kept as small tuples (millions of them at 10M
    transactions), with interned strings: a few distinct values shared by every entry."""
    products = {r["product_id"]: (r["category"], r["product_name"]) for r in dataset.iter_products()}
    customers = {r["customer_id"]: (sys.intern(r["customer_segment"]), sys.intern(r["customer_age_group"]))
                 for r in dataset.iter_customers()}
    for row in dataset.iter_transactions():
        segment, age_group = customers[row["customer_id"]]
        category, name = products[row["product_id"]]
        yield embed_values(transaction_doc(row), segment, age_group, category, name)


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
    db = connections.mongo()
    collections = [
        ("customers", (customer_doc(r) for r in dataset.iter_customers())),
        ("products", (product_doc(r) for r in dataset.iter_products())),
        ("transactions", transaction_docs(dataset)),
    ]
    for name, docs in collections:
        db[name].drop()
        insert_docs(db[name], docs)
        print(f"[mongodb] {name} loaded")
    db.transactions.create_index("customer_id")
    db.transactions.create_index("transaction_date")


def check(expected, quiet=False):
    """Health query: document counts plus a $lookup on a sample of transactions."""
    sample_size = 1000
    try:
        db = connections.mongo()
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
