
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from app.services.image_service import optimize_image_from_url
from app.services.background_removal_service import apply_background_removal
from app.utils.download import download_image
from fastapi.responses import Response
from PIL import Image, ImageOps
import time
import io


router = APIRouter()

class ImageOptimizeRequest(BaseModel):
    url: str

class RemoveBackgroundQualityRequest(BaseModel):
    url: str
    quality: str = "high"  # high, normal

@router.post("/optimize")
async def optimize_image(body: ImageOptimizeRequest):
    url = body.url
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided")
    try:
        img_bytes = await optimize_image_from_url(url)
        return Response(content=img_bytes, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.post("/remove-background-combined", summary="Remover fondo COMBINADO (Threshold + Rembg)")
async def remove_bg_combined(body: RemoveBackgroundQualityRequest):
    """
    Remover fondo combinando dos métodos para mejor resultado.
    
    Estrategia:
    1. Primero limpia con Threshold (fondos uniformes)
    2. Luego refina con Rembg IA (bordes precisos)
    3. Redimensiona a 2000x2000 píxeles (MISMO QUE /candidates/process-images)
    
    - Quality: "high" (mejor, ~4-5s) o "normal" (~3-4s)
    - RECOMENDADO para productos con calidad inconsistente
    - Más preciso que cualquiera de los métodos solos
    - Devuelve JPEG 2000x2000 cuadrado (compatible con pipeline)
    
    curl -X 'POST' 'http://localhost:3600/image/remove-background-combined' \\
      -H 'Content-Type: application/json' \\
      -d '{"url": "https://...", "quality": "high"}'
    """
    if not body.url:
        raise HTTPException(status_code=400, detail="URL requerida")
    
    quality = body.quality.lower() if body.quality else "high"
    
    try:
        img = await download_image(body.url)
        result_img = apply_background_removal(img, method="combined", quality=quality)
        
        # ===== APLICAR MISMO PIPELINE QUE /candidates/process-images =====
        # 1. Asegurar RGBA
        if result_img.mode != 'RGBA':
            result_img = result_img.convert('RGBA')
        
        # 2. Reemplazar fondo transparente con blanco
        background = Image.new('RGBA', result_img.size, (255, 255, 255, 255))
        result_img = Image.alpha_composite(background, result_img)
        
        # 3. Hacer cuadrado
        max_side = max(result_img.size)
        square_img = Image.new('RGBA', (max_side, max_side), (255, 255, 255, 255))
        offset = ((max_side - result_img.width) // 2, (max_side - result_img.height) // 2)
        square_img.paste(result_img, offset)
        
        # 4. Redimensionar a 2000x2000 con mejor interpolación
        resized_img = square_img.resize((2000, 2000), Image.Resampling.LANCZOS)
        
        # 5. Guardar como PNG (sin pérdida de calidad, mejor compresión)
        output = io.BytesIO()
        resized_img.save(output, format="PNG", optimize=True)
        output.seek(0)
        img_bytes = output.getvalue()
        
        return Response(content=img_bytes, media_type="image/png")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
