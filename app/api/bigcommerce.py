"""
Router para integraciones con BigCommerce.
Incluye endpoints para upload de imágenes.
"""

from fastapi import APIRouter, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel, Field, validator
from typing import Optional
from app.config import BIGCOMMERCE_API_KEY
from app.services.bigcommerce_service import process_bigcommerce_image_update
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


# ==================== FUNCIONES DE BACKGROUND ====================

async def process_bigcommerce_upload(brand: str, sku: str, processed_image_url: str, original_image_url: str, task_id: str):
    """
    Procesa el upload de imagen a BigCommerce en segundo plano.

    Flujo:
      1. Buscar producto en MongoDB (brand + SKU/MPN)
      2. Obtener credenciales de tienda desde MySQL
      3. Eliminar imágenes actuales del producto (logos/default)
      4. Subir la imagen procesada nueva
    """
    try:
        logger.info(f"[{task_id}] Tarea recibida: brand={brand}, sku={sku}")
        result = process_bigcommerce_image_update(
            brand=brand,
            sku=sku,
            processed_image_url=processed_image_url,
            task_id=task_id,
        )
        logger.info(
            f"[{task_id}] Tarea completada: "
            f"productos={result['products_found']}, "
            f"tiendas={len(result['stores_processed'])}, "
            f"errores={len(result['errors'])}"
        )
    except Exception as e:
        logger.error(f"[{task_id}] Error inesperado en background task: {e}", exc_info=True)


# ==================== ENDPOINTS ====================

@router.post("/upload-image", summary="Upload de imagen a BigCommerce", response_model=BigCommerceUploadResponse)
async def upload_image_to_bigcommerce(
    request: BigCommerceImageUploadRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(None, description="API Key para BigCommerce")
):
    """
    Upload de imagen a BigCommerce.
    
    **Headers:**
    - `x-api-key`: API Key para autorización
    
    **Payload:**
    - `brand`: Marca del producto
    - `sku`: SKU del producto
    - `processed_image_url`: URL de la imagen procesada (PNG 2000x2000)
    - `original_image_url`: URL de la imagen original
    
    **Respuesta:**
    - `200 OK`: Tarea iniciada en segundo plano
    
    **Comportamiento:**
    - La solicitud retorna inmediatamente con taskId
    - El procesamiento ocurre en background
    - No espera a que se complete el upload
    
    **Ejemplo:**
    ```bash
    curl -X 'POST' 'http://localhost:3600/bigcommerce/upload-image' \\
      -H 'x-api-key: test-api-key-12345' \\
      -H 'Content-Type: application/json' \\
      -d '{
        "brand": "Canon",
        "sku": "EF50MM",
        "processed_image_url": "https://cdn.example.com/processed.png",
        "original_image_url": "https://cdn.example.com/original.jpg"
      }'
    ```
    """
    
    # ===== VALIDAR API KEY =====
    if not x_api_key:
        logger.warning("❌ Intento de upload sin x-api-key")
        raise HTTPException(
            status_code=401,
            detail="x-api-key header requerido"
        )
    
    if x_api_key != BIGCOMMERCE_API_KEY:
        logger.warning(f"❌ Intento de upload con x-api-key inválida")
        raise HTTPException(
            status_code=403,
            detail="x-api-key inválida o no autorizada"
        )
    
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
        task_id=task_id
    )
    
    logger.info(f"✓ Tarea iniciada: {task_id} ({request.brand}/{request.sku})")
    
    return BigCommerceUploadResponse(
        message=f"Upload iniciado en segundo plano",
        taskId=task_id,
        brand=request.brand,
        sku=request.sku,
        status="queued"
    )
