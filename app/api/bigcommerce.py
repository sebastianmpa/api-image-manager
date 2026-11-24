"""
Router para integraciones con BigCommerce.
Incluye endpoints para upload de imágenes.
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field, validator
from typing import Optional
from app.services.bigcommerce_service import process_bigcommerce_image_update, get_product_url
from app.services.review_approvals_sync import save_candidate_approvals, BIGCOMMERCE_UPLOAD_SOURCE
import logging

router = APIRouter(prefix="/bigcommerce", tags=["BigCommerce"])

logger = logging.getLogger(__name__)

# ==================== MODELOS ====================

class BigCommerceImageUploadRequest(BaseModel):
    """Modelo para upload de imagen a BigCommerce"""
    brand: str = Field(..., min_length=1, description="Marca del producto")
    sku: str = Field(..., min_length=1, description="SKU del producto")
    processed_image_url: str = Field(..., description="URL de la imagen procesada")
    original_image_url: str = Field(..., description="URL de la imagen original")
    is_diagram: bool = Field(False, description="True si la imagen es un diagrama (__IMG = DGR); False = foto (__IMG = NWM)")
    is_thumbnail: bool = Field(True, description="True = imagen PRINCIPAL del producto en BigCommerce (is_thumbnail); False = imagen secundaria de la galería")
    email: Optional[str] = Field(None, description="Opcional: quién sube la imagen; queda en el reporte de imágenes aprobadas")

    @validator('brand')
    def brand_not_empty(cls, v):
        if not v or v.strip() == "":
            raise ValueError("brand no puede estar vacío")
        return v.strip()
    
    @validator('sku')
    def sku_not_empty(cls, v):
        if not v or v.strip() == "":
            raise ValueError("sku no puede estar vacío")
        return v.strip()
    
    @validator('processed_image_url')
    def validate_processed_url(cls, v):
        if not v.startswith('http://') and not v.startswith('https://'):
            raise ValueError("processed_image_url debe ser una URL válida")
        return v.strip()
    
    @validator('original_image_url')
    def validate_original_url(cls, v):
        if not v.startswith('http://') and not v.startswith('https://'):
            raise ValueError("original_image_url debe ser una URL válida")
        return v.strip()


class BigCommerceUploadResponse(BaseModel):
    """Respuesta para upload a BigCommerce"""
    message: str
    taskId: str
    brand: str
    sku: str
    status: str
    productUrl: Optional[str] = None


# ==================== FUNCIONES DE BACKGROUND ====================

def bigcommerce_status(stores_ok: int, stores_failed: int) -> str:
    """ok = subió a todas sus tiendas; partial = a algunas; failed = a ninguna (o producto no encontrado)."""
    if stores_ok and not stores_failed:
        return "ok"
    if stores_ok:
        return "partial"
    return "failed"


def run_bigcommerce_upload(brand: str, sku: str, processed_image_url: str, task_id: str, is_diagram: bool = False, is_thumbnail: bool = True, approval: Optional[dict] = None) -> dict:
    """
    Sube una imagen a BigCommerce (bloqueante) y devuelve el resultado por tienda.

    Flujo:
      1. Buscar producto en MongoDB (brand + SKU/MPN)
      2. Obtener credenciales de tienda desde MySQL
      3. Eliminar imágenes actuales del producto (logos/default)
      4. Subir la imagen procesada nueva
      5. Marcar custom field __IMG = "DGR" si es_diagram, o "NWM" si no
      6. Si viene `approval` (dict con source, email, candidate_id, lote...), guardar la imagen
         como aprobada en MySQL (image_candidate_approvals) con el resultado por tienda
    """
    stores_ok, stores_failed, errors, product_url = 0, 0, [], None
    try:
        logger.info(f"[{task_id}] Tarea recibida: brand={brand}, sku={sku}, is_diagram={is_diagram}, is_thumbnail={is_thumbnail}")
        result = process_bigcommerce_image_update(
            brand=brand,
            sku=sku,
            processed_image_url=processed_image_url,
            task_id=task_id,
            is_diagram=is_diagram,
            is_thumbnail=is_thumbnail,
        )
        stores_ok = sum(1 for s in result["stores_processed"] if s.get("uploaded_image_id"))
        stores_failed = len(result["stores_processed"]) - stores_ok
        errors = [str(e) for e in result.get("errors") or []]
        product_url = result.get("product_url")
        logger.info(
            f"[{task_id}] Tarea completada: "
            f"productos={result['products_found']}, "
            f"tiendas={len(result['stores_processed'])}, "
            f"errores={len(result['errors'])}"
        )
    except Exception as e:
        logger.error(f"[{task_id}] Error inesperado subiendo a BigCommerce: {e}", exc_info=True)
        errors.append(str(e))

    if approval is not None:
        # Se guarda aunque BigCommerce haya fallado (bc_stores_ok = 0) para que se vea en el reporte
        save_candidate_approvals([{
            "chunk_id": task_id, "brand": brand, "mpn": sku, "image_url": processed_image_url,
            "is_diagram": int(bool(is_diagram)), "is_thumbnail": int(bool(is_thumbnail)),
            "bc_stores_ok": stores_ok, "bc_stores_failed": stores_failed, "product_url": product_url,
            **approval,
        }])

    return {
        "taskId": task_id,
        "status": bigcommerce_status(stores_ok, stores_failed),
        "storesOk": stores_ok,
        "storesFailed": stores_failed,
        "productUrl": product_url,
        "errors": errors[:5],
    }


def process_bigcommerce_upload(brand: str, sku: str, processed_image_url: str, original_image_url: str, task_id: str, is_diagram: bool = False, is_thumbnail: bool = True, approval: Optional[dict] = None):
    """Tarea en background (sync: Starlette la corre en un hilo y no bloquea el event loop)."""
    run_bigcommerce_upload(brand, sku, processed_image_url, task_id, is_diagram=is_diagram,
                           is_thumbnail=is_thumbnail, approval=approval)


# ==================== ENDPOINTS ====================

@router.post("/upload-image", summary="Upload de imagen a BigCommerce", response_model=BigCommerceUploadResponse)
async def upload_image_to_bigcommerce(
    request: BigCommerceImageUploadRequest,
    background_tasks: BackgroundTasks
):
    """
    Upload de imagen a BigCommerce.
    
    **Headers:**
    - `x-api-key`: API Key (WAREHOUSE_API_KEY)
    
    **Payload:**
    - `brand`: Marca del producto
    - `sku`: SKU del producto
    - `processed_image_url`: URL de la imagen procesada (PNG 2000x2000)
    - `original_image_url`: URL de la imagen original
    - `is_diagram` (opcional, default false): true si es un diagrama
    - `is_thumbnail` (opcional, default true): true = imagen principal del producto; false = secundaria
    
    **Respuesta:**
    - `200 OK`: Tarea iniciada en segundo plano
    
    **Comportamiento:**
    - La solicitud retorna inmediatamente con taskId
    - El procesamiento ocurre en background
    - No espera a que se complete el upload
    
    **Ejemplo:**
    ```bash
    curl -X 'POST' 'http://localhost:3600/bigcommerce/upload-image' \\
      -H 'x-api-key: <API_KEY>' \\
      -H 'Content-Type: application/json' \\
      -d '{
        "brand": "Canon",
        "sku": "EF50MM",
        "processed_image_url": "https://cdn.example.com/processed.png",
        "original_image_url": "https://cdn.example.com/original.jpg"
      }'
    ```
    """
    
    # La API key (x-api-key = WAREHOUSE_API_KEY) se valida para todos los endpoints en app/security.py

    # ===== GENERAR TASK ID =====
    import uuid
    import time
    task_id = f"bc-upload-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    
    # ===== AGREGAR TAREA AL BACKGROUND =====
    background_tasks.add_task(
        process_bigcommerce_upload,
        brand=request.brand,
        sku=request.sku,
        processed_image_url=request.processed_image_url,
        original_image_url=request.original_image_url,
        task_id=task_id,
        is_diagram=request.is_diagram,
        is_thumbnail=request.is_thumbnail,
        approval={"source": BIGCOMMERCE_UPLOAD_SOURCE, "email": request.email},
    )

    logger.info(f"✓ Tarea iniciada: {task_id} ({request.brand}/{request.sku})")
    
    return BigCommerceUploadResponse(
        message=f"Upload iniciado en segundo plano",
        taskId=task_id,
        brand=request.brand,
        sku=request.sku,
        status="queued",
        productUrl=get_product_url(request.brand, request.sku),
    )
