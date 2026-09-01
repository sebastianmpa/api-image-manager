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
deleted_candidates_collection = db["deleted_candidates"]
deletion_stats_collection = db["deletion_stats"]
failed_images_collection = db["failed_images"]
processed_candidates_collection = db["processed_candidates"]
not_found_candidates_collection = db["image_candidates_not_found"]


def soft_delete_candidate(candidate_id: str, email: str):
    """
    Realiza un borrado lógico de un candidato:
    1. Obtiene el candidato de la colección original
    2. Lo guarda en la colección de eliminados
    3. Lo elimina de la colección original
    4. Actualiza las estadísticas de eliminación
    """
    from bson import ObjectId
    from datetime import datetime
    
    try:
        obj_id = ObjectId(candidate_id)
        
        # Obtener el candidato
        candidate = collection.find_one({"_id": obj_id})
        if not candidate:
            return None, "Candidato no encontrado"
        
        # Guardar en colección de eliminados con metadatos
        candidate_to_delete = candidate.copy()
        candidate_to_delete["deletedAt"] = datetime.utcnow()
        candidate_to_delete["deletedBy"] = email
        candidate_to_delete["originalId"] = obj_id
        
        deleted_candidates_collection.insert_one(candidate_to_delete)
        
        # Eliminar de la colección original
        collection.delete_one({"_id": obj_id})
        
        # Actualizar estadísticas
        deletion_stats_collection.update_one(
            {"email": email},
            {
                "$inc": {"count": 1, "totalCandidates": 1},
                "$set": {"lastDeletion": datetime.utcnow()},
                "$push": {
                    "deletions": {
                        "candidateId": obj_id,
                        "brand": candidate.get("brand"),
                        "mpn": candidate.get("mpn"),
                        "deletedAt": datetime.utcnow()
                    }
                }
            },
            upsert=True
        )
        
        return candidate, None
        
    except Exception as e:
        return None, str(e)


def get_deletion_stats(page: int = 1, limit: int = 20, date_from: str = None, date_to: str = None):
    """
    Obtiene las estadísticas de eliminaciones con paginación.
    Retorna: email, cantidad de candidatos eliminados, fecha última eliminación
    """
    from datetime import datetime

    filters = {}
    if date_from or date_to:
        filters["lastDeletion"] = {}
        if date_from:
            filters["lastDeletion"]["$gte"] = datetime.fromisoformat(date_from)
        if date_to:
            filters["lastDeletion"]["$lte"] = datetime.fromisoformat(date_to)

    skip = (page - 1) * limit
    cursor = deletion_stats_collection.find(filters).sort("lastDeletion", -1).skip(skip).limit(limit)
    
    results = []
    for doc in cursor:
        last_deletion = doc.get("lastDeletion")
        results.append({
            "email": doc.get("email"),
            "deletedCount": doc.get("count", 0),
            "lastDeletion": last_deletion.isoformat() if isinstance(last_deletion, datetime) else None,
            "totalDeleted": doc.get("totalCandidates", 0)
        })
    
    total_count = deletion_stats_collection.count_documents(filters)
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


def get_deletion_stats_by_email(email: str):
    """
    Obtiene las estadísticas de eliminación para un email específico.
    """
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
    
    doc = deletion_stats_collection.find_one({"email": email})
    
    if not doc:
        return {
            "email": email,
            "deletedCount": 0,
            "lastDeletion": None,
            "deletions": []
        }
    
    # Convertir ObjectId y datetime
    deletions = [convert_obj(d) for d in doc.get("deletions", [])]
    
    return {
        "email": email,
        "deletedCount": doc.get("count", 0),
        "lastDeletion": doc.get("lastDeletion").isoformat() if doc.get("lastDeletion") else None,
        "deletions": deletions
    }


def save_failed_image(chunk_id: str, brand: str, mpn: str, url: str, error: str):
    """
    Guarda una imagen fallida en la colección failed_images.
    """
    from datetime import datetime
    
    failed_image = {
        "chunkId": chunk_id,
        "brand": brand,
        "mpn": mpn,
        "url": url,
        "error": error,
        "failedAt": datetime.utcnow(),
        "status": "pending"  # pending, retry, ignored
    }
    
    result = failed_images_collection.insert_one(failed_image)
    return str(result.inserted_id)


def get_failed_images(page: int = 1, limit: int = 20, status_filter: str = None, date_from: str = None, date_to: str = None):
    """
    Obtiene las imágenes fallidas con paginación.
    """
    from datetime import datetime

    filters = {}
    if status_filter:
        filters["status"] = status_filter
    if date_from or date_to:
        filters["failedAt"] = {}
        if date_from:
            filters["failedAt"]["$gte"] = datetime.fromisoformat(date_from)
        if date_to:
            filters["failedAt"]["$lte"] = datetime.fromisoformat(date_to)

    skip = (page - 1) * limit
    cursor = failed_images_collection.find(filters).sort("failedAt", -1).skip(skip).limit(limit)
    
    def convert_obj(obj):
        from bson import ObjectId
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: convert_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [convert_obj(i) for i in obj]
        return obj
    
    results = []
    for doc in cursor:
        results.append(convert_obj(doc))
    
    total_count = failed_images_collection.count_documents(filters)
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


def update_failed_image_status(failed_image_id: str, new_status: str):
    """
    Actualiza el estado de una imagen fallida (pending, retry, ignored).
    """
    from bson import ObjectId
    
    result = failed_images_collection.update_one(
        {"_id": ObjectId(failed_image_id)},
        {"$set": {"status": new_status}}
    )
    
    return result.modified_count > 0


def move_candidate_to_processed(brand: str, mpn: str, processed_data: dict):
    """
    Traslada un candidato de image_candidates a processed_candidates
    buscando por brand y mpn.
    Guarda los datos de procesamiento adicionales.
    """
    from datetime import datetime
    
    try:
        # Buscar el candidato por brand y mpn
        candidate = collection.find_one({"brand": brand, "mpn": mpn})
        if not candidate:
            return None, "Candidato no encontrado"
        
        candidate_id = candidate["_id"]
        
        # Crear documento para tabla de procesados
        processed_candidate = candidate.copy()
        processed_candidate["processedAt"] = datetime.utcnow()
        processed_candidate["processedData"] = processed_data
        processed_candidate["originalId"] = candidate_id
        
        # Insertar en tabla de procesados
        processed_candidates_collection.insert_one(processed_candidate)
        
        # Eliminar de tabla original
        collection.delete_one({"_id": candidate_id})
        
        return candidate, None
        
    except Exception as e:
        return None, str(e)


def get_processed_candidates(page: int = 1, limit: int = 20, brand: str = None, mpn: str = None, lote: str = None, email: str = None, date_from: str = None, date_to: str = None):
    """
    Obtiene los candidatos procesados (aprobados) con paginación.
    """
    from datetime import datetime

    filters = {}
    if brand:
        filters["brand"] = brand
    if mpn:
        filters["mpn"] = mpn
    if lote:
        filters["lote"] = lote
    if email:
        filters["email"] = email
    if date_from or date_to:
        filters["processedAt"] = {}
        if date_from:
            filters["processedAt"]["$gte"] = datetime.fromisoformat(date_from)
        if date_to:
            filters["processedAt"]["$lte"] = datetime.fromisoformat(date_to)

    skip = (page - 1) * limit
    cursor = processed_candidates_collection.find(filters).sort("processedAt", -1).skip(skip).limit(limit)
    
    def convert_obj(obj):
        from bson import ObjectId
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: convert_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [convert_obj(i) for i in obj]
        return obj
    
    results = []
    for doc in cursor:
        results.append(convert_obj(doc))
    
    total_count = processed_candidates_collection.count_documents(filters)
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


def get_candidates_grouped_paginated(page: int = 1, limit: int = 20, brand: str = None, mpn: str = None, lote: str = None):
    filters = {}
    if brand:
        filters["brand"] = brand
    if mpn:
        filters["mpn"] = mpn
    if lote:
        filters["lote"] = lote

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


def get_not_found_candidates_paginated(page: int = 1, limit: int = 20, brand: str = None, mpn: str = None, lote: str = None):
    """
    Obtiene los registros de búsquedas sin resultados (image_candidates_not_found)
    con paginación. No tienen imagen asociada, solo el registro de la búsqueda.
    """
    from bson import ObjectId
    from datetime import datetime

    filters = {}
    if brand:
        filters["brand"] = brand
    if mpn:
        filters["mpn"] = mpn
    if lote:
        filters["lote"] = lote

    skip = (page - 1) * limit
    cursor = not_found_candidates_collection.find(filters).sort("createdAt", -1).skip(skip).limit(limit)

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

    results = [convert_obj(doc) for doc in cursor]

    total_count = not_found_candidates_collection.count_documents(filters)
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


MANUAL_UPLOAD_LOTE = "000"


def add_manual_processed_candidate(brand: str, mpn: str, image_url: str, email: str, is_diagram: bool = False):
    """
    Crea directamente un candidato ya aprobado en processed_candidates a partir de
    una imagen subida manualmente (ya optimizada y alojada en Spaces). No pasa por
    image_candidates: la imagen manual no necesita un paso de procesamiento aparte.

    Los campos de metadata de scraping (storeId, pageUrl, etc.) quedan en None ya
    que no aplican. Las subidas manuales se agrupan bajo lote "000".
    """
    from datetime import datetime

    now = datetime.utcnow()

    processed_candidate = {
        "storeId": None,
        "productId": None,
        "brand": brand,
        "mpn": mpn,
        "domain": None,
        "pageUrl": None,
        "pageTitle": None,
        "pageSnippet": None,
        "categoryGuess": None,
        "imageUrl": image_url,
        "thumbnailUrl": None,
        "searchRank": None,
        "scoreProductPage": None,
        "createdAt": now,
        "email": email,
        "lote": MANUAL_UPLOAD_LOTE,
        "processedAt": now,
        "processedMethod": "manual_upload",
        "processedData": {
            "brand": brand,
            "mpn": mpn,
            "imageUrl": image_url
        },
        "source": "manual_upload",
        "isDiagram": is_diagram
    }

    result = processed_candidates_collection.insert_one(processed_candidate)
    processed_candidate["_id"] = result.inserted_id
    return processed_candidate


def resolve_not_found_candidates(brand: str, mpn: str, images: list, email: str = None):
    """
    Cuando se sube manualmente una o más imágenes para brand+mpn, resuelve los
    registros 'not found' pendientes de esa marca+mpn: los traslada a
    processed_candidates (con el detalle de las imágenes que los resolvieron,
    cada una con su propio isDiagram) y los elimina de image_candidates_not_found.

    :param images: lista de dicts {"imageUrl": str, "isDiagram": bool}
    Retorna la lista de _id (como string) de los registros movidos.
    """
    from datetime import datetime

    matches = list(not_found_candidates_collection.find({"brand": brand, "mpn": mpn}))
    moved_ids = []
    # Solo se marca isDiagram=True a nivel del registro si TODAS las imágenes
    # que lo resolvieron son diagramas; si hay al menos una foto real, cuenta como imagen.
    all_diagrams = bool(images) and all(img.get("isDiagram") for img in images)

    for doc in matches:
        original_id = doc["_id"]

        processed_doc = {k: v for k, v in doc.items() if k != "_id"}
        processed_doc["originalId"] = original_id
        processed_doc["processedAt"] = datetime.utcnow()
        processed_doc["processedMethod"] = "manual_upload_resolved_not_found"
        processed_doc["processedData"] = {
            "brand": brand,
            "mpn": mpn,
            "images": images
        }
        processed_doc["source"] = "manual_upload"
        processed_doc["isDiagram"] = all_diagrams
        if email:
            processed_doc["email"] = email

        processed_candidates_collection.insert_one(processed_doc)
        not_found_candidates_collection.delete_one({"_id": original_id})
        moved_ids.append(str(original_id))

    return moved_ids


def get_global_stats():
    """
    Devuelve totales globales basados en los candidatos originales:
    - pending:            candidatos aún en la tabla original (por procesar)
    - processed:          candidatos trasladados a processed_candidates (aprobados)
    - deleted:            candidatos eliminados (borrado lógico)
    - notFound:           búsquedas sin resultado (image_candidates_not_found)
    - failedImages:       total de registros en failed_images
    - failedCandidates:   candidatos únicos con al menos una imagen fallida
    - total:              suma de pending + processed + deleted
    - manualUploads:      candidatos aprobados que se subieron manualmente
    - images / diagrams:  candidatos aprobados que son imagen vs diagrama
    - uniqueSkus:         combinaciones brand+mpn distintas entre los aprobados
    - secondaryImages:    imágenes aprobadas adicionales sobre esa primera por SKU
    """
    pending            = collection.count_documents({})
    processed          = processed_candidates_collection.count_documents({})
    deleted            = deleted_candidates_collection.count_documents({})
    not_found          = not_found_candidates_collection.count_documents({})
    failed_images      = failed_images_collection.count_documents({})
    # Candidatos únicos con al menos una imagen fallida (por brand+mpn)
    failed_candidates  = len(failed_images_collection.distinct("mpn"))

    manual_uploads = processed_candidates_collection.count_documents({"source": "manual_upload"})
    diagrams       = processed_candidates_collection.count_documents({"isDiagram": True})
    images         = processed - diagrams

    sku_groups = list(processed_candidates_collection.aggregate([
        {"$group": {"_id": {"brand": "$brand", "mpn": "$mpn"}, "count": {"$sum": 1}}}
    ]))
    unique_skus = len(sku_groups)
    secondary_images = sum(max(g["count"] - 1, 0) for g in sku_groups)

    return {
        "pending":          pending,
        "processed":        processed,
        "deleted":          deleted,
        "notFound":         not_found,
        "failedImages":     failed_images,
        "failedCandidates": failed_candidates,
        "total":            pending + processed + deleted,
        "manualUploads":    manual_uploads,
        "images":           images,
        "diagrams":         diagrams,
        "uniqueSkus":       unique_skus,
        "secondaryImages":  secondary_images,
    }
