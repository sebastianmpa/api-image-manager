"""
Router de marcas de la tienda (colección Mongo `Brands`).
Pensado para llenar y filtrar el selector de marcas del front.
"""

import re
import threading
import time
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services.mongo_service import db
from app.services.bigcommerce_service import get_store_base_url, PRODUCT_URL_STORE_ID

router = APIRouter()
logger = logging.getLogger(__name__)

brands_collection = db["Brands"]
products_collection = db["Products"]

# Las marcas cambian poco (se sincronizan de BigCommerce una vez al día): se cachean por tienda
BRANDS_CACHE_TTL_SECONDS = 600
_cache = {}  # store_id -> (loaded_at, [marca])
_cache_lock = threading.Lock()


def _load_store_brands(store_id: int) -> list:
    """Lee las marcas de la tienda con su link y cuántos productos tiene cada una."""
    docs = list(brands_collection.find(
        {"STOREID": store_id},
        {"_id": 0, "ID": 1, "NAME": 1, "CUSTOM_URL": 1, "IMAGE_URL": 1},
    ))
    counts = {
        row["_id"]: row["n"]
        for row in products_collection.aggregate([
            {"$match": {"STOREID": store_id}},
            {"$group": {"_id": "$BRAND_ID", "n": {"$sum": 1}}},
        ])
    }
    base_url = get_store_base_url(store_id)

    brands = []
    for d in docs:
        custom_url = d.get("CUSTOM_URL")
        if isinstance(custom_url, dict):
            custom_url = custom_url.get("url")
        brands.append({
            "id": d.get("ID"),
            "name": (d.get("NAME") or "").strip(),
            "productCount": counts.get(d.get("ID"), 0),
            "url": f"{base_url}{custom_url}" if base_url and custom_url else None,
            "imageUrl": d.get("IMAGE_URL") or None,
        })
    brands.sort(key=lambda b: b["name"].lower())
    return brands


def get_store_brands(store_id: int, refresh: bool = False) -> list:
    with _cache_lock:
        cached = _cache.get(store_id)
        if cached and not refresh and time.time() - cached[0] < BRANDS_CACHE_TTL_SECONDS:
            return cached[1]
    brands = _load_store_brands(store_id)
    with _cache_lock:
        _cache[store_id] = (time.time(), brands)
    return brands


@router.get("", summary="Listar marcas de la tienda con filtros (para el selector de marcas)")
def list_brands(
    search: Optional[str] = Query(None, description="Texto a buscar en el nombre (sin distinguir mayúsculas); ej. 'briggs'"),
    has_products: Optional[bool] = Query(None, description="true = solo marcas con productos; false = solo sin productos; vacío = todas"),
    store_id: int = Query(PRODUCT_URL_STORE_ID, description="Tienda (default 5 = smallenginesprodealer.com)"),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    refresh: bool = Query(False, description="true = ignora la caché de 10 min y relee Mongo"),
):
    """
    Lista las marcas (`Brands` en Mongo) de una tienda, ordenadas por nombre.

    Cada marca trae `id`, `name`, `productCount`, `url` (link público en la tienda) e `imageUrl`.
    """
    try:
        brands = get_store_brands(store_id, refresh=refresh)
    except Exception as e:
        logger.error(f"Error leyendo marcas de la tienda {store_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    items = brands
    if has_products is not None:
        items = [b for b in items if (b["productCount"] > 0) == has_products]
    if search and search.strip():
        # Todas las palabras deben aparecer, en cualquier orden: "stratton briggs" encuentra "BRIGGS & STRATTON"
        words = [re.escape(w) for w in search.strip().lower().split()]
        items = [b for b in items if all(re.search(w, b["name"].lower()) for w in words)]

    total = len(items)
    start = (page - 1) * limit
    return {
        "storeId": store_id,
        "total": total,
        "page": page,
        "limit": limit,
        "totalPages": (total + limit - 1) // limit,
        "results": items[start:start + limit],
    }
