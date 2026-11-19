"""
Protección por API key para TODOS los endpoints de la API.

Cada petición debe traer el header `x-api-key` con el valor de WAREHOUSE_API_KEY
(la misma key para todos los clientes: frontend, Warehouse, bot de aprobación, etc.).

/docs y /openapi.json quedan abiertos para poder ver la documentación (no exponen la key);
desde Swagger se usa el botón "Authorize" para probar con la key.
"""

import secrets
from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import WAREHOUSE_API_KEY

api_key_header = APIKeyHeader(name="x-api-key", auto_error=False, description="API key de la API de imágenes (WAREHOUSE_API_KEY)")


def require_api_key(x_api_key: Optional[str] = Security(api_key_header)) -> str:
    if not WAREHOUSE_API_KEY:
        raise HTTPException(status_code=503, detail="WAREHOUSE_API_KEY no está configurada en el servidor")
    if not x_api_key:
        raise HTTPException(status_code=401, detail="x-api-key header requerido")
    if not secrets.compare_digest(x_api_key, WAREHOUSE_API_KEY):
        raise HTTPException(status_code=403, detail="x-api-key inválida o no autorizada")
    return x_api_key
