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


def get_deletion_stats(page: int = 1, limit: int = 20):
    """
    Obtiene las estadísticas de eliminaciones con paginación.
    Retorna: email, cantidad de candidatos eliminados, fecha última eliminación
    """
    from datetime import datetime
    
    skip = (page - 1) * limit
    cursor = deletion_stats_collection.find().sort("lastDeletion", -1).skip(skip).limit(limit)
    
    results = []
    for doc in cursor:
        last_deletion = doc.get("lastDeletion")
        results.append({
            "email": doc.get("email"),
            "deletedCount": doc.get("count", 0),
            "lastDeletion": last_deletion.isoformat() if isinstance(last_deletion, datetime) else None,
            "totalDeleted": doc.get("totalCandidates", 0)
        })
    
    total_count = deletion_stats_collection.count_documents({})
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


def get_failed_images(page: int = 1, limit: int = 20, status_filter: str = None):
    """
    Obtiene las imágenes fallidas con paginación.
    """
    from datetime import datetime
    
    filters = {}
    if status_filter:
        filters["status"] = status_filter
    
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


def get_processed_candidates(page: int = 1, limit: int = 20, brand: str = None, mpn: str = None):
    """
    Obtiene los candidatos procesados con paginación.
    """
    from datetime import datetime
    
    filters = {}
    if brand:
        filters["brand"] = brand
    if mpn:
        filters["mpn"] = mpn
    
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
