
from fastapi import APIRouter, HTTPException, Query, status, File, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator
from bson import ObjectId
from typing import Optional, List
import boto3
from botocore.client import Config
from app.services.spaces_service import (
    upload_image_to_spaces,
    get_image_from_spaces,
    delete_image_from_spaces,
    update_image_in_spaces,
    list_images_from_spaces
)
from app.services.image_service import optimize_image_from_url
from app.services.mongo_service import get_candidates_grouped_paginated, collection

router = APIRouter()

# ==================== MODELOS ====================

class ImageResult(BaseModel):
    brand: str = Field(..., min_length=1, description="Marca del producto")
    mpn: str = Field(..., min_length=1, description="Código MPN del producto")
    imageUrls: List[str] = Field(..., min_items=1, description="Lista de URLs de imágenes")
    
    @validator('brand')
    def brand_not_empty(cls, v):
        if not v or v.strip() == "":
            raise ValueError("brand no puede estar vacío")
        return v.strip()
    
    @validator('mpn')
    def mpn_not_empty(cls, v):
        if not v or v.strip() == "":
            raise ValueError("mpn no puede estar vacío")
        return v.strip()
    
    @validator('imageUrls', pre=True)
    def validate_image_urls(cls, v):
        if not isinstance(v, list):
            raise ValueError("imageUrls debe ser una lista")
        if len(v) == 0:
            raise ValueError("imageUrls no puede estar vacía")
        
        for url in v:
            if not isinstance(url, str):
                raise ValueError(f"Cada URL debe ser un string, recibido: {type(url)}")
            if not url.strip():
                raise ValueError("Las URLs no pueden estar vacías")
            if not (url.startswith('http://') or url.startswith('https://')):
                raise ValueError(f"URL inválida: {url}. Debe comenzar con http:// o https://")
        
        return [url.strip() for url in v]

class ImageProcessingData(BaseModel):
    results: List[ImageResult] = Field(..., min_items=1, description="Lista de productos con imágenes")

class ImageProcessingRequest(BaseModel):
    chunkId: str = Field(..., min_length=1, description="ID único del lote de procesamiento")
    data: ImageProcessingData
    
    @validator('chunkId')
    def chunk_id_not_empty(cls, v):
        if not v or v.strip() == "":
            raise ValueError("chunkId no puede estar vacío")
        return v.strip()

class ImageProcessingResponse(BaseModel):
    chunkId: str
    data: dict

# ==================== ENDPOINTS DE PROCESAMIENTO ====================

@router.post("/process-images", summary="Procesar y subir lote de imágenes", response_model=ImageProcessingResponse)
async def process_images_batch(request: ImageProcessingRequest):
    """
    Procesa un lote de imágenes:
    1. Valida la estructura del JSON de entrada
    2. Obtiene las imágenes desde URLs externas
    3. Las optimiza
    4. Las sube a Digital Ocean Spaces
    5. Devuelve la misma estructura con las URLs actualizadas
    
    **Validaciones:**
    - chunkId: no vacío
    - results: lista con al menos 1 elemento
    - brand y mpn: no vacíos
    - imageUrls: lista con al menos 1 URL válida
    - URLs: deben comenzar con http:// o https://
    """
    try:
        chunk_id = request.chunkId
        results = request.data.results
        processed_results = []
        
        # Procesar cada resultado (brand + mpn + imágenes)
        for result_idx, result in enumerate(results):
            brand = result.brand
            mpn = result.mpn
            image_urls = result.imageUrls
            processed_urls = []
            
            # Procesar cada URL
            for img_idx, url in enumerate(image_urls):
                try:
                    print(f"\n[{chunk_id}] Procesando: {brand} / {mpn} / Imagen {img_idx + 1}/{len(image_urls)}")
                    print(f"  URL: {url}")
                    
                    # Optimizar la imagen desde la URL
                    optimized_image_bytes = await optimize_image_from_url(url)
                    print(f"  ✓ Imagen optimizada ({len(optimized_image_bytes)} bytes)")
                    
                    # Generar nombre único para la imagen
                    # Formato: brand_mpn_index_timestamp.jpg
                    import time
                    timestamp = int(time.time() * 1000)
                    # Usar nombre simple sin caracteres especiales
                    filename = f"{brand}_{mpn}_{img_idx}_{timestamp}.jpg"
                    
                    # Subir a Digital Ocean Spaces
                    do_url = upload_image_to_spaces(optimized_image_bytes, filename, "image/jpeg")
                    processed_urls.append(do_url)
                    
                except Exception as e:
                    error_msg = f"Error procesando imagen en result[{result_idx}] url[{img_idx}] ({url}): {str(e)}"
                    print(f"  ✗ {error_msg}")
                    raise HTTPException(status_code=500, detail=error_msg)
            
            processed_results.append({
                "brand": brand,
                "mpn": mpn,
                "imageUrls": processed_urls
            })
        
        print(f"\n✓ Lote {chunk_id} completado exitosamente\n")
        
        return ImageProcessingResponse(
            chunkId=chunk_id,
            data={"results": processed_results}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Error procesando lote: {str(e)}"
        print(f"✗ {error_msg}")
        raise HTTPException(
            status_code=400,
            detail=error_msg
        )


@router.post("/upload-image", summary="Subir imagen a DigitalOcean Spaces")
async def upload_image_to_spaces_endpoint(file: UploadFile = File(...)):
    """Sube una imagen a DigitalOcean Spaces y retorna la URL pública."""
    try:
        contents = await file.read()
        filename = file.filename
        url = upload_image_to_spaces(contents, filename, file.content_type)
        return {"filename": filename, "url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {str(e)}")


@router.get("/image/{filename}", summary="Obtener imagen de DigitalOcean Spaces")
async def get_image_endpoint(filename: str):
    """Obtiene una imagen de DigitalOcean Spaces."""
    try:
        image_bytes, content_type = get_image_from_spaces(filename)
        return StreamingResponse(iter([image_bytes]), media_type=content_type)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error obteniendo imagen: {str(e)}")


@router.delete("/image/{filename}", summary="Eliminar imagen de DigitalOcean Spaces")
async def delete_image_endpoint(filename: str):
    """Elimina una imagen de DigitalOcean Spaces."""
    try:
        result = delete_image_from_spaces(filename)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error eliminando imagen: {str(e)}")


@router.put("/image/{filename}", summary="Actualizar imagen en DigitalOcean Spaces")
async def update_image_endpoint(filename: str, file: UploadFile = File(...)):
    """Actualiza una imagen en DigitalOcean Spaces."""
    try:
        contents = await file.read()
        url = update_image_in_spaces(filename, contents, file.content_type)
        return {"filename": filename, "url": url, "message": "Imagen actualizada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error actualizando imagen: {str(e)}")


@router.get("/images/list", summary="Listar imágenes con paginación")
async def list_images_endpoint(
    prefix: str = Query("", description="Prefijo para filtrar imágenes"),
    page: int = Query(1, ge=1, description="Número de página (comenzando en 1)"),
    limit: int = Query(25, ge=1, le=100, description="Cantidad de imágenes por página (máximo 100)")
):
    """
    Lista las imágenes en DigitalOcean Spaces con paginación.
    
    **Parámetros:**
    - `prefix`: Filtrar por prefijo (ej. 'ECHO_')
    - `page`: Número de página (por defecto 1)
    - `limit`: Imágenes por página (por defecto 10, máximo 100)
    """
    try:
        result = list_images_from_spaces(prefix, page, limit)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listando imágenes: {str(e)}")


# ==================== ENDPOINTS DE CANDIDATOS ====================

@router.delete("/{candidate_id}", summary="Eliminar candidato por ID", status_code=status.HTTP_200_OK)
def delete_candidate(candidate_id: str):
    """Eliminar un candidato por su ObjectId"""
    try:
        result = collection.delete_one({"_id": ObjectId(candidate_id)})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Candidato no encontrado")
        return {"message": "Candidato eliminado correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/all", summary="Listar candidatos agrupados por brand y mpn")
def list_candidates(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    brand: Optional[str] = None,
    mpn: Optional[str] = None
):
    """
    Devuelve candidatos agrupados por brand y mpn, con paginación.
    """
    try:
        return get_candidates_grouped_paginated(page=page, limit=limit, brand=brand, mpn=mpn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
