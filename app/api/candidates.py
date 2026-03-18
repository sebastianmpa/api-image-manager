
from fastapi import APIRouter, HTTPException, Query, status, File, UploadFile
import boto3
from botocore.client import Config
from app.services.spaces_service import upload_image_to_spaces

@router.post("/upload-image", summary="Subir imagen a DigitalOcean Spaces")
async def upload_image_to_spaces(file: UploadFile = File(...)):
    """Sube una imagen a DigitalOcean Spaces y retorna la URL pública."""
    try:
        contents = await file.read()
        filename = file.filename
        url = upload_image_to_spaces(contents, filename, file.content_type)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {str(e)}")
from bson import ObjectId
from app.services.mongo_service import get_candidates_grouped_paginated
from typing import Optional

router = APIRouter()
from app.services.mongo_service import collection

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
