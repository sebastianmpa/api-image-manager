
from dotenv import load_dotenv

# Cargar variables de entorno PRIMERO
load_dotenv()

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.api import image
from app.api import candidates
from app.api import bigcommerce
from app.services.review_approvals_sync import start_review_sync_loop
from app.security import require_api_key

app = FastAPI()

# Configurar CORS para permitir cualquier origen
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

# Todos los endpoints exigen el header x-api-key (ver app/security.py)
protected = [Depends(require_api_key)]
app.include_router(image.router, prefix="/image", tags=["image"], dependencies=protected)
app.include_router(candidates.router, prefix="/candidates", tags=["candidates"], dependencies=protected)
app.include_router(bigcommerce.router, dependencies=protected)


@app.on_event("startup")
def start_background_syncs():
	start_review_sync_loop()
