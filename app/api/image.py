
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.image_service import optimize_image_from_url
from fastapi.responses import Response


router = APIRouter()

class ImageOptimizeRequest(BaseModel):
    url: str

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
