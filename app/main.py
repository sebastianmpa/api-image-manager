
from dotenv import load_dotenv

# Cargar variables de entorno PRIMERO
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import image
from app.api import candidates

app = FastAPI()

# Configurar CORS para permitir cualquier origen
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

app.include_router(image.router, prefix="/image", tags=["image"])
app.include_router(candidates.router, prefix="/candidates", tags=["candidates"])
