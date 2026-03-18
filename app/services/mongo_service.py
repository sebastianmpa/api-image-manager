import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_HOST = os.getenv('MONGO_HOST', 'localhost')
MONGO_PORT = int(os.getenv('MONGO_PORT', 27017))
MONGO_USER = os.getenv('MONGO_USER', '')
MONGO_PASS = os.getenv('MONGO_PASS', '')
MONGO_DBNAME = os.getenv('MONGO_DBNAME', 'Prontoweb')

if MONGO_USER and MONGO_PASS:
    MONGO_URI = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DBNAME}"
else:
    MONGO_URI = f"mongodb://{MONGO_HOST}:{MONGO_PORT}/"

client = MongoClient(MONGO_URI)
db = client[MONGO_DBNAME]
collection = db["image_candidates"]




def get_candidates_grouped_paginated(page: int = 1, limit: int = 20, brand: str = None, mpn: str = None):
    filters = {}
    if brand:
        filters["brand"] = brand
    if mpn:
        filters["mpn"] = mpn

    skip = (page - 1) * limit
    cursor = collection.find(filters).sort("createdAt", -1).skip(skip).limit(limit)


    from bson import ObjectId
    from datetime import datetime
    def convert_obj(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: convert_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [convert_obj(i) for i in obj]
        return obj

    grouped = {}
    for doc in cursor:
        brand_val = doc.get("brand", "SinBrand")
        mpn_val = doc.get("mpn", "SinMPN")
        key = (brand_val, mpn_val)
        if key not in grouped:
            grouped[key] = []
        # Convertir ObjectId y datetime
        doc_conv = convert_obj(doc)
        grouped[key].append(doc_conv)

    # Convertir a lista de objetos agrupados para el frontend
    results = []
    for (brand_val, mpn_val), items in grouped.items():
        results.append({
            "brand": brand_val,
            "mpn": mpn_val,
            "candidates": items
        })

    total_count = collection.count_documents(filters)
    total_pages = (total_count + limit - 1) // limit

    return {
        "results": results,
        "stats": {
            "total": total_count,
            "page": page,
            "limit": limit,
            "total_pages": total_pages
        }
    }
