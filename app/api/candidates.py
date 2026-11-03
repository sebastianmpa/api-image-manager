
from fastapi import APIRouter, HTTPException, Query, status, File, UploadFile, Form, BackgroundTasks, Header
from fastapi.responses import StreamingResponse, JSONResponse
from io import BytesIO
from PIL import Image, ImageOps
import secrets
import time
import uuid
from pydantic import BaseModel, Field, validator
from bson import ObjectId
from typing import Optional, List
from enum import IntEnum

class PageSize(IntEnum):
    small = 25
    medium = 50
    large = 100
import boto3
from botocore.client import Config
from app.services.spaces_service import (
    upload_image_to_spaces,
    get_image_from_spaces,
    delete_image_from_spaces,
    update_image_in_spaces,
    list_images_from_spaces
)
from app.services.image_service import optimize_image_from_url, optimize_image_from_bytes
from app.services.mongo_service import get_candidates_grouped_paginated, collection, soft_delete_candidate, get_deletion_stats, get_deletion_stats_by_email, save_failed_image, get_failed_images, update_failed_image_status, move_candidate_to_processed, get_processed_candidates, get_global_stats, add_manual_processed_candidate, get_not_found_candidates_paginated, resolve_not_found_candidates
from app.services.mongo_service import add_warehouse_candidate, WAREHOUSE_LOTE
from app.services.mysql_stats_service import save_stats_snapshot
from app.services.review_approvals_sync import sync_review_approvals
from app.services.bigcommerce_service import search_products_by_brand_and_sku
from app.api.bigcommerce import process_bigcommerce_upload
from app.config import WAREHOUSE_API_KEY

router = APIRouter()

# ==================== MODELOS ====================

class ImageResult(BaseModel):
    candidateId: Optional[str] = Field(None, description="ID del candidato (ObjectId)")
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
    email: Optional[str] = Field(None, description="Email del usuario que aprueba el lote")
    data: ImageProcessingData
    
    @validator('chunkId')
    def chunk_id_not_empty(cls, v):
        if not v or v.strip() == "":
            raise ValueError("chunkId no puede estar vacío")
        return v.strip()

class ImageProcessingResponse(BaseModel):
    chunkId: str
    data: dict

def _single_product(results: List[ImageResult]) -> dict:
    """brand/mpn para el snapshot de estadísticas solo si el lote trae un único producto."""
    products = {(r.brand, r.mpn) for r in results}
    if len(products) == 1:
        brand, mpn = products.pop()
        return {"brand": brand, "mpn": mpn}
    return {}

# ==================== ENDPOINTS DE PROCESAMIENTO ====================

@router.post("/process-images", summary="Procesar y subir lote de imágenes", response_model=ImageProcessingResponse)
async def process_images_batch(request: ImageProcessingRequest, background_tasks: BackgroundTasks):
    """
    Procesa un lote de imágenes:
    1. Valida la estructura del JSON de entrada
    2. Obtiene las imágenes desde URLs externas
    3. Las optimiza
    4. Las sube a Digital Ocean Spaces
    5. Traslada el candidato a processed_candidates (si se procesó exitosamente)
    6. Devuelve la misma estructura con las URLs actualizadas
    
    **Validaciones:**
    - chunkId: no vacío
    - results: lista con al menos 1 elemento
    - brand y mpn: no vacíos
    - imageUrls: lista con al menos 1 URL válida
    - URLs: deben comenzar con http:// o https://
    
    **Importante:**
    - El candidateId es opcional. Si no se envía, se busca automáticamente por brand + mpn
    - Los candidatos procesados exitosamente se trasladan a la tabla 'processed_candidates'
    - Las imágenes fallidas se guardan en 'failed_images' para revisión posterior
    """
    try:
        chunk_id = request.chunkId
        approved_by = (request.email or "").strip() or None
        results = request.data.results
        processed_results = []

        # Procesar cada resultado (brand + mpn + imágenes)
        for result_idx, result in enumerate(results):
            brand = result.brand
            mpn = result.mpn
            candidate_id = result.candidateId
            image_urls = result.imageUrls
            processed_images = []  # list of {originalImageUrl, imageUrl}
            moved_ids = []

            # Procesar cada URL
            import time
            for img_idx, url in enumerate(image_urls):
                optimized_image_bytes = None
                do_url = None
                filename = None
                try:
                    print(f"\n[{chunk_id}] Procesando: {brand} / {mpn} / Imagen {img_idx + 1}/{len(image_urls)}")
                    print(f"  URL: {url}")

                    # Optimizar la imagen desde la URL
                    optimized_image_bytes = await optimize_image_from_url(url)
                    print(f"  ✓ Imagen optimizada ({len(optimized_image_bytes)} bytes)")

                    # Generar nombre único para la imagen
                    timestamp = int(time.time() * 1000)
                    filename = f"{brand}_{mpn}_{img_idx}_{timestamp}.jpg"

                    # Subir a Digital Ocean Spaces
                    do_url = upload_image_to_spaces(optimized_image_bytes, filename, "image/jpeg")
                    print(f"  ✓ Subida a Spaces: {do_url}")

                    processed_images.append({
                        "originalImageUrl": url,
                        "imageUrl": do_url
                    })

                except Exception as e:
                    error_msg = f"Error procesando imagen en result[{result_idx}] url[{img_idx}] ({url}): {str(e)}"
                    print(f"  ✗ {error_msg}")
                    # Si la imagen llegó a subirse antes del fallo, eliminarla de Spaces
                    if do_url and filename:
                        try:
                            delete_image_from_spaces(filename)
                            print(f"  ✓ Imagen eliminada de Spaces tras fallo: {filename}")
                        except Exception as del_e:
                            print(f"  ⚠ Error eliminando imagen de Spaces: {str(del_e)}")
                    # Guardar la imagen fallida en la colección
                    save_failed_image(chunk_id, brand, mpn, url, str(e))
                    # Continuar con la siguiente imagen
                    continue
                finally:
                    # Liberar memoria siempre, independientemente del resultado
                    optimized_image_bytes = None

            # Si se procesó al menos una imagen exitosamente, trasladar el candidato
            if processed_images:
                try:
                    processed_data = {
                        "brand": brand,
                        "mpn": mpn,
                        "processedImageUrls": [img["imageUrl"] for img in processed_images],
                        "totalImagesProcessed": len(processed_images),
                        "chunkId": chunk_id,
                        "approvedBy": approved_by
                    }
                    moved_ids = move_candidate_to_processed(
                        brand, mpn, [img["originalImageUrl"] for img in processed_images], processed_data, candidate_id
                    )
                    if moved_ids:
                        print(f"  ✓ {len(moved_ids)} candidato(s) trasladado(s) a processed_candidates: {moved_ids}")
                    else:
                        print(f"  ⚠ No se encontró en image_candidates ningún candidato con esas URLs")
                except Exception as e:
                    print(f"  ⚠ Error trasladando candidato: {str(e)}")

            processed_results.append({
                "brand": brand,
                "mpn": mpn,
                "imageUrls": processed_images,
                "movedCandidateIds": moved_ids
            })
        
        print(f"\n✓ Lote {chunk_id} completado exitosamente\n")

        background_tasks.add_task(save_stats_snapshot, "process_images", email=approved_by, chunk_id=chunk_id, **_single_product(results))

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


@router.post("/process-mark-as-processed", summary="Marcar lote como procesado (SIN optimizar)", response_model=ImageProcessingResponse)
async def process_mark_as_processed(request: ImageProcessingRequest, background_tasks: BackgroundTasks):
    """
    Marca un lote de candidatos como procesados SIN optimizar ni subir imágenes.
    
    Solo:
    1. Valida la estructura del JSON de entrada
    2. Traslada cada candidato de 'image_candidates' a 'processed_candidates'
    3. Devuelve la misma estructura con las URLs originales
    
    **IMPORTANTE:**
    - NO optimiza imágenes
    - NO sube a Digital Ocean Spaces
    - Solo realiza el cambio de tabla en MongoDB
    - Las URLs devueltas son las mismas que se recibieron
    
    **Caso de uso:** Procesar lotes masivos sin necesidad de optimización.
    """
    try:
        chunk_id = request.chunkId
        approved_by = (request.email or "").strip() or None
        results = request.data.results
        processed_results = []

        print(f"\n[{chunk_id}] Marcando lote como procesado (sin optimización)")
        
        # Procesar cada resultado (brand + mpn)
        for result_idx, result in enumerate(results):
            brand = result.brand
            mpn = result.mpn
            candidate_id = result.candidateId
            image_urls = result.imageUrls
            
            try:
                print(f"\n[{chunk_id}] Resultado {result_idx + 1}: {brand} / {mpn}")
                print(f"  URLs: {len(image_urls)} imagen(es)")
                
                # Trasladar candidato a processed_candidates (sin optimizar ni subir)
                processed_data = {
                    "brand": brand,
                    "mpn": mpn,
                    "originalImageUrls": image_urls,
                    "totalImagesProcessed": len(image_urls),
                    "chunkId": chunk_id,
                    "processedMethod": "mark_as_processed",  # Indica que solo fue marcado, no optimizado
                    "approvedBy": approved_by
                }
                
                moved_ids = move_candidate_to_processed(brand, mpn, image_urls, processed_data, candidate_id)
                if moved_ids:
                    print(f"  ✓ {len(moved_ids)} candidato(s) trasladado(s) a processed_candidates: {moved_ids}")
                else:
                    print(f"  ⚠ No se encontró en image_candidates ningún candidato con esas URLs")

                # Devolver las mismas URLs originales
                processed_results.append({
                    "brand": brand,
                    "mpn": mpn,
                    "imageUrls": [
                        {
                            "originalImageUrl": url,
                            "imageUrl": url  # URLs originales (sin cambios)
                        } for url in image_urls
                    ],
                    "movedCandidateIds": moved_ids
                })
                
            except Exception as e:
                error_msg = f"Error procesando candidato {result_idx} ({brand}/{mpn}): {str(e)}"
                print(f"  ✗ {error_msg}")
                raise HTTPException(status_code=400, detail=error_msg)
        
        print(f"\n✓ Lote {chunk_id} marcado como procesado (sin optimización)\n")

        background_tasks.add_task(save_stats_snapshot, "mark_as_processed", email=approved_by, chunk_id=chunk_id, **_single_product(results))

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


@router.post("/upload-product-image", summary="Subir una o varias imágenes de producto ya aprobadas, asociarlas a marca+mpn y enviarlas a BigCommerce")
async def upload_product_image_endpoint(
    background_tasks: BackgroundTasks,
    brand: str = Form(..., description="Marca del producto"),
    mpn: str = Form(..., description="Código MPN/SKU del producto"),
    email: str = Form(..., description="Email del usuario que sube la(s) imagen(es)"),
    files: List[UploadFile] = File(..., description="Una o más imágenes a subir"),
    is_diagram: List[bool] = Form(..., description="Un valor por cada imagen en 'files', emparejado por posición (el N-ésimo is_diagram corresponde al N-ésimo file): True si esa imagen es un diagrama, False si es una foto de producto normal")
):
    """
    Sube una o varias imágenes de producto ya aprobadas. Por cada imagen:
    1. La optimiza
    2. La sube a Digital Ocean Spaces
    3. Crea un candidato directamente en 'processed_candidates' (queda aprobado de
       una vez, con brand, mpn, email, lote="000" e isDiagram según corresponda a
       esa imagen específica)
    4. Encola el envío de esa imagen a BigCommerce (una tarea en background por imagen,
       ya que ese servicio solo acepta una imagen a la vez). El custom field __IMG
       queda en "DGR" o "NWM" según el is_diagram de esa imagen puntual.

    `is_diagram` debe tener exactamente la misma cantidad de valores que `files`,
    en el mismo orden (posición 0 de is_diagram ↔ posición 0 de files, etc.).

    Al terminar el lote, si hay registros en 'image_candidates_not_found' para ese
    brand+mpn (búsquedas previas sin resultado), se eliminan de ahí y se trasladan a
    'processed_candidates' también, ya que ahora sí existe imagen para ese producto.

    Si una imagen falla al optimizarse/subirse a Spaces, continúa con las demás y la
    reporta en 'failed'. El envío a BigCommerce corre en background y no bloquea la
    respuesta; su resultado solo queda en logs (mismo comportamiento que
    POST /bigcommerce/upload-image).
    """
    brand = brand.strip()
    mpn = mpn.strip()
    email = email.strip()
    if not brand or not mpn:
        raise HTTPException(status_code=400, detail="brand y mpn no pueden estar vacíos")
    if not email:
        raise HTTPException(status_code=400, detail="email no puede estar vacío")
    if not files:
        raise HTTPException(status_code=400, detail="Debe enviar al menos un archivo")
    if len(is_diagram) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f"Debe enviar un valor de is_diagram por cada archivo: {len(files)} archivo(s) pero {len(is_diagram)} valor(es) de is_diagram"
        )

    import time
    import uuid

    created = []
    failed = []

    for idx, (file, diagram_flag) in enumerate(zip(files, is_diagram)):
        try:
            contents = await file.read()
            optimized_image_bytes = optimize_image_from_bytes(contents)

            timestamp = int(time.time() * 1000)
            filename = f"{brand}_{mpn}_{idx}_{timestamp}.jpg"

            image_url = upload_image_to_spaces(optimized_image_bytes, filename, "image/jpeg")

            candidate = add_manual_processed_candidate(brand, mpn, image_url, email, diagram_flag)

            bc_task_id = f"bc-upload-{timestamp}-{uuid.uuid4().hex[:8]}"
            background_tasks.add_task(
                process_bigcommerce_upload,
                brand=brand,
                sku=mpn,
                processed_image_url=image_url,
                original_image_url=image_url,
                task_id=bc_task_id,
                is_diagram=diagram_flag
            )

            created.append({
                "candidateId": str(candidate["_id"]),
                "imageUrl": image_url,
                "originalFilename": file.filename,
                "isDiagram": diagram_flag,
                "isPrimaryManual": candidate.get("isPrimaryManual"),
                "bigcommerceTaskId": bc_task_id
            })
        except Exception as e:
            failed.append({
                "originalFilename": file.filename,
                "error": str(e)
            })

    resolved_not_found = []
    if created:
        resolved_not_found = resolve_not_found_candidates(
            brand, mpn,
            images=[{"imageUrl": c["imageUrl"], "isDiagram": c["isDiagram"]} for c in created],
            email=email
        )
        background_tasks.add_task(save_stats_snapshot, "manual_upload", email=email, brand=brand, mpn=mpn)

    return {
        "brand": brand,
        "mpn": mpn,
        "email": email,
        "totalUploaded": len(created),
        "totalFailed": len(failed),
        "candidates": created,
        "failed": failed,
        "resolvedNotFound": resolved_not_found
    }


WAREHOUSE_MAX_PHOTO_BYTES = 15 * 1024 * 1024


def _prepare_warehouse_photo(contents: bytes) -> bytes:
    """
    Valida la foto y la normaliza a JPEG. Aplica la rotación EXIF del teléfono para que
    la imagen no quede de lado cuando después se optimice (el pipeline ignora EXIF).
    """
    if not contents:
        raise ValueError("Archivo vacío")
    if len(contents) > WAREHOUSE_MAX_PHOTO_BYTES:
        raise ValueError(f"La foto supera el máximo de {WAREHOUSE_MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    try:
        img = Image.open(BytesIO(contents))
        img.load()
    except Exception:
        raise ValueError("El archivo no es una imagen válida")

    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@router.post("/warehouse-upload", summary="Subir fotos del equipo de Warehouse como candidatos pendientes")
def warehouse_upload(
    background_tasks: BackgroundTasks,
    brand: str = Form(..., description="Marca del producto"),
    mpn: str = Form(..., description="MPN/SKU del producto"),
    user_id: Optional[str] = Form(None, description="ID de quien tomó las fotos (opcional)"),
    user_name: Optional[str] = Form(None, description="Nombre de quien tomó las fotos (opcional)"),
    photo1: UploadFile = File(..., description="Foto 1 (obligatoria)"),
    photo2: Optional[UploadFile] = File(None, description="Foto 2 (opcional)"),
    photo3: Optional[UploadFile] = File(None, description="Foto 3 (opcional)"),
    x_api_key: Optional[str] = Header(None, description="API Key de Warehouse"),
):
    """
    Recibe de 1 a 3 fotos de un producto tomadas por el equipo de Warehouse y las guarda
    como candidatos PENDIENTES en `image_candidates` (lote fijo APP001, source="warehouse").
    Pasan por la misma aprobación que los candidatos del scraper.

    Por cada foto:
    1. Valida que sea una imagen (máx. 15 MB) y la normaliza a JPEG con la rotación del teléfono
    2. La sube a Digital Ocean Spaces (sin optimizar: eso ocurre al aprobarla)
    3. Crea el candidato pendiente

    Si el producto (brand + mpn) no existe en Products, se rechaza con 404.
    Si una foto falla, las demás continúan; `ok` es true solo si todas se subieron.

    **Respuesta:**
    ```json
    { "ok": true, "upload_id": "app-...", "photos": [
        { "slot": 1, "ok": true, "url": "https://...", "candidate_id": "..." },
        { "slot": 2, "ok": false, "error": "..." } ] }
    ```
    """
    if not WAREHOUSE_API_KEY:
        raise HTTPException(status_code=503, detail="WAREHOUSE_API_KEY no está configurada en el servidor")
    if not x_api_key:
        raise HTTPException(status_code=401, detail="x-api-key header requerido")
    if not secrets.compare_digest(x_api_key, WAREHOUSE_API_KEY):
        raise HTTPException(status_code=403, detail="x-api-key inválida o no autorizada")

    brand = brand.strip()
    mpn = mpn.strip()
    user_id = (user_id or "").strip() or None
    user_name = (user_name or "").strip() or None
    if not brand or not mpn:
        return JSONResponse(status_code=400, content={"ok": False, "error": "brand y mpn no pueden estar vacíos"})

    products = search_products_by_brand_and_sku(brand, mpn)
    if not products:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Producto no encontrado: brand={brand}, mpn={mpn}"}
        )

    # Guardar con la marca/MPN del catálogo para que se agrupe con los candidatos del scraper
    product = products[0]
    brand = product.get("BRAND") or brand
    mpn = product.get("MPN") or mpn

    upload_id = f"app-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    photos = []

    for slot, photo in enumerate([photo1, photo2, photo3], start=1):
        if photo is None or not photo.filename:
            continue

        filename = None
        image_url = None
        try:
            jpeg_bytes = _prepare_warehouse_photo(photo.file.read())
            filename = f"APP_{brand}_{mpn}_{upload_id}_slot{slot}.jpg"
            image_url = upload_image_to_spaces(jpeg_bytes, filename, "image/jpeg")
            candidate = add_warehouse_candidate(
                brand, mpn, image_url, upload_id, slot, user_id, user_name, product
            )
            photos.append({"slot": slot, "ok": True, "url": image_url, "candidate_id": str(candidate["_id"])})
        except Exception as e:
            # Si la foto alcanzó a subirse a Spaces pero falló después, no dejarla huérfana
            if image_url and filename:
                try:
                    delete_image_from_spaces(filename)
                except Exception as del_e:
                    print(f"  ⚠ Error eliminando foto de Spaces tras fallo: {del_e}")
            photos.append({"slot": slot, "ok": False, "error": str(e)})

    if any(p["ok"] for p in photos):
        background_tasks.add_task(save_stats_snapshot, "warehouse_upload", brand=brand, mpn=mpn)

    return {
        "ok": bool(photos) and all(p["ok"] for p in photos),
        "upload_id": upload_id,
        "lote": WAREHOUSE_LOTE,
        "brand": brand,
        "mpn": mpn,
        "photos": photos,
    }


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

@router.get("/stats", summary="Totales globales de candidatos")
def global_stats():
    """
    Devuelve los totales globales de candidatos derivados de la tabla original:

    - **pending**: candidatos aún sin procesar (tabla `image_candidates`)
    - **processed**: candidatos ya procesados/aprobados (`processed_candidates`), bot + manual
    - **deleted**: candidatos eliminados con borrado lógico (`deleted_candidates`)
    - **notFound**: búsquedas sin resultado (`image_candidates_not_found`)
    - **failedImages**: total de registros de imágenes fallidas (`failed_images`)
    - **failedCandidates**: candidatos únicos con al menos una imagen fallida
    - **total**: pending + processed + deleted
    - **manualUploads**: candidatos aprobados subidos manualmente (no vía scraping)
    - **images** / **diagrams**: candidatos aprobados (bot + manual) que son imagen vs diagrama
    - **uniqueSkus**: combinaciones brand+mpn distintas entre los aprobados (bot + manual)
    - **secondaryImages**: imágenes aprobadas adicionales sobre la primera por SKU (bot + manual)

    **Módulo de búsqueda/carga (candidatos de origen bot):**
    - **botFoundCandidates**: total de candidatos que alguna vez localizó el bot
      (pending + aprobados-por-bot + rechazados-por-usuario)
    - **userApprovedCandidates**: candidatos del bot que el usuario aprobó (movidos a
      `processed_candidates` por el flujo automático, no por subida manual)
    - **userRejectedCandidates**: candidatos del bot que el usuario rechazó (= `deleted`)
    - **autoProcessedImages**: debe coincidir siempre con `userApprovedCandidates`

    **Desglose de subidas manuales** (principal = la primera imagen/diagrama que se sube
    para ese brand+mpn; secundaria = cualquier otra después, del mismo lote o de uno posterior):
    - **totalManualImages** / **manualImagesPrimary** / **manualImagesSecondary**
    - **totalManualDiagrams** / **manualDiagramsPrimary** / **manualDiagramsSecondary**

    **Nota:** las estadísticas de aprobación/rechazo de la revisión humana posterior sobre
    imágenes ya procesadas automáticamente (tipo "images-update") viven en el backend de
    task-hub, no acá, y no están incluidas en esta respuesta.
    """
    try:
        return get_global_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stats/sync", summary="Guardar snapshot de estadísticas en MySQL")
def sync_global_stats():
    """
    Calcula las estadísticas globales y las inserta como una fila nueva en la tabla
    MySQL `image_manager_stats` (espejo de `GET /stats`).

    Los snapshots se guardan solos cada vez que se procesa, aprueba, sube o elimina un
    candidato; este endpoint sirve para forzar uno (ej. desde un cron), ya que los
    candidatos que el bot inserta directo en MongoDB no pasan por esta API.
    """
    stats = save_stats_snapshot("manual_sync")
    if stats is None:
        raise HTTPException(status_code=500, detail="No se pudo guardar el snapshot en MySQL (ver logs)")
    return stats


@router.post("/stats/sync-reviews", summary="Sincronizar revisiones de task-hub (images-update) a MySQL")
def sync_task_hub_reviews():
    """
    Copia las aprobaciones/rechazos de la revisión humana "images-update" de task-hub
    a la tabla MySQL `image_review_approvals` (upsert por id, no duplica). Para Power BI
    usar la vista `vw_image_review_aprobaciones`.

    Corre sola cada `TASKHUB_SYNC_MINUTES` (15 por defecto); este endpoint la fuerza.
    """
    summary = sync_review_approvals()
    if summary is None:
        raise HTTPException(status_code=500, detail="No se pudo sincronizar (ver logs; puede que ya haya un sync en curso)")
    return summary


@router.get("/deletion-stats", summary="Listar estadísticas de eliminaciones")
def list_deletion_stats(
    page: int = Query(1, ge=1, description="Número de página"),
    limit: int = Query(20, ge=1, le=100, description="Límite de resultados por página"),
    date_from: Optional[str] = Query(None, description="Fecha inicio (ISO 8601, ej. 2026-04-01 o 2026-04-01T00:00:00)"),
    date_to: Optional[str] = Query(None, description="Fecha fin (ISO 8601, ej. 2026-04-30 o 2026-04-30T23:59:59)")
):
    """
    Lista las estadísticas de eliminaciones de todos los usuarios.

    **Filtros opcionales:**
    - `date_from` / `date_to`: Rango de fechas sobre `lastDeletion`
    """
    try:
        result = get_deletion_stats(page=page, limit=limit, date_from=date_from, date_to=date_to)
        result["globalStats"] = get_global_stats()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/deletion-stats/{email}", summary="Obtener estadísticas de un usuario")
def get_user_deletion_stats(email: str):
    """
    Obtiene las estadísticas de eliminación de un usuario específico.
    
    **Parámetros:**
    - `email`: Email del usuario
    
    **Retorna:**
    - email: Email del usuario
    - deletedCount: Cantidad de candidatos eliminados
    - lastDeletion: Fecha de última eliminación
    - deletions: Detalle de cada eliminación (candidato, marca, MPN, fecha)
    """
    try:
        return get_deletion_stats_by_email(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/failed-images", summary="Listar imágenes que fallaron al procesarse")
def list_failed_images(
    page: int = Query(1, ge=1, description="Número de página"),
    limit: int = Query(20, ge=1, le=100, description="Límite de resultados por página"),
    status: Optional[str] = Query(None, description="Filtrar por estado (pending, retry, ignored)"),
    date_from: Optional[str] = Query(None, description="Fecha inicio (ISO 8601, ej. 2026-04-01 o 2026-04-01T00:00:00)"),
    date_to: Optional[str] = Query(None, description="Fecha fin (ISO 8601, ej. 2026-04-30 o 2026-04-30T23:59:59)")
):
    """
    Lista las imágenes que fallaron durante el procesamiento.

    **Filtros opcionales:**
    - `status`: pending, retry, ignored
    - `date_from` / `date_to`: Rango de fechas sobre `failedAt`
    """
    try:
        result = get_failed_images(page=page, limit=limit, status_filter=status, date_from=date_from, date_to=date_to)
        result["globalStats"] = get_global_stats()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/failed-images/{failed_image_id}/status", summary="Actualizar estado de imagen fallida")
def update_failed_image_status_endpoint(
    failed_image_id: str,
    background_tasks: BackgroundTasks,
    new_status: str = Query(..., description="Nuevo estado: pending, retry, ignored")
):
    """
    Actualiza el estado de una imagen fallida.
    
    **Parámetros:**
    - `failed_image_id`: ID de la imagen fallida
    - `new_status`: Nuevo estado (pending, retry, ignored)
    """
    if new_status not in ["pending", "retry", "ignored"]:
        raise HTTPException(
            status_code=400,
            detail="El estado debe ser uno de: pending, retry, ignored"
        )
    
    try:
        updated = update_failed_image_status(failed_image_id, new_status)
        if not updated:
            raise HTTPException(status_code=404, detail="Imagen fallida no encontrada")

        background_tasks.add_task(save_stats_snapshot, "failed_image_status")
        return {"message": f"Estado actualizado a: {new_status}", "failedImageId": failed_image_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/processed", summary="Listar candidatos procesados (aprobados)")
def list_processed_candidates(
    page: int = Query(1, ge=1, description="Número de página"),
    limit: int = Query(20, ge=1, le=100, description="Límite de resultados por página"),
    brand: Optional[str] = Query(None, description="Filtrar por marca"),
    mpn: Optional[str] = Query(None, description="Filtrar por MPN"),
    lote: Optional[str] = Query(None, description="Filtrar por número de lote"),
    email: Optional[str] = Query(None, description="Filtrar por usuario que aprobó/subió la imagen"),
    date_from: Optional[str] = Query(None, description="Fecha inicio (ISO 8601, ej. 2026-04-01 o 2026-04-01T00:00:00)"),
    date_to: Optional[str] = Query(None, description="Fecha fin (ISO 8601, ej. 2026-04-30 o 2026-04-30T23:59:59)")
):
    """
    Lista los candidatos que ya fueron procesados (aprobados).

    **Filtros opcionales:**
    - `brand`, `mpn`: Filtrar por marca o MPN
    - `lote`: Filtrar por número de lote (las subidas manuales quedan en lote "000")
    - `email`: Filtrar por usuario que subió/procesó la imagen
    - `date_from` / `date_to`: Rango de fechas sobre `processedAt`
    """
    try:
        result = get_processed_candidates(page=page, limit=limit, brand=brand, mpn=mpn, lote=lote, email=email, date_from=date_from, date_to=date_to)
        result["globalStats"] = get_global_stats()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== ENDPOINTS DE CANDIDATOS ====================

@router.delete("/{candidate_id}", summary="Eliminar candidato por ID (borrado lógico)", status_code=status.HTTP_200_OK)
def delete_candidate(candidate_id: str, background_tasks: BackgroundTasks, email: str = Query(..., description="Email del usuario que elimina")):
    """
    Realiza un borrado lógico de un candidato:
    1. Obtiene el candidato de la colección original
    2. Lo guarda en la colección de eliminados (backup)
    3. Lo elimina de la colección original
    4. Actualiza las estadísticas de eliminación
    
    **Parámetros:**
    - `candidate_id`: ObjectId del candidato a eliminar
    - `email`: Email del usuario que realiza la eliminación
    """
    try:
        candidate, error = soft_delete_candidate(candidate_id, email)
        
        if error:
            if error == "Candidato no encontrado":
                raise HTTPException(status_code=404, detail=error)
            raise HTTPException(status_code=500, detail=error)

        background_tasks.add_task(
            save_stats_snapshot, "delete_candidate",
            email=email, brand=candidate.get("brand"), mpn=candidate.get("mpn")
        )
        return {
            "message": "Candidato eliminado correctamente",
            "candidateId": candidate_id,
            "deletedBy": email,
            "brand": candidate.get("brand"),
            "mpn": candidate.get("mpn")
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/all", summary="Listar candidatos agrupados por brand y mpn")
def list_candidates(
    page: int = Query(1, ge=1, description="Número de página"),
    limit: PageSize = Query(PageSize.small, description="Resultados por página: 25, 50 o 100"),
    brand: Optional[str] = None,
    mpn: Optional[str] = None,
    lote: Optional[str] = Query(None, description="Filtrar por número de lote")
):
    """
    Devuelve candidatos agrupados por brand y mpn, con paginación.

    Además incluye `notFound`: búsquedas que no arrojaron ningún resultado
    (colección `image_candidates_not_found`), sin imagen asociada.

    **Valores permitidos para `limit`:** 25, 50, 100
    """
    try:
        result = get_candidates_grouped_paginated(page=page, limit=int(limit), brand=brand, mpn=mpn, lote=lote)

        not_found = get_not_found_candidates_paginated(page=page, limit=int(limit), brand=brand, mpn=mpn, lote=lote)
        result["notFound"] = not_found["results"]
        result["notFoundStats"] = not_found["stats"]

        result["globalStats"] = get_global_stats()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
