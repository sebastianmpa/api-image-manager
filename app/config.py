"""
Configuración de la aplicación.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar variables de entorno desde la raíz del proyecto
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# ==================== API KEYS ====================

# API Key para BigCommerce uploads
BIGCOMMERCE_API_KEY = os.getenv("BIGCOMMERCE_API_KEY", "test-api-key-12345")

# Validar que la API key esté configurada
if not BIGCOMMERCE_API_KEY or BIGCOMMERCE_API_KEY == "test-api-key-12345":
    print("⚠️  ADVERTENCIA: BIGCOMMERCE_API_KEY está usando valor por defecto (test)")
    print("    Define BIGCOMMERCE_API_KEY en .env para producción")

# ==================== CONFIGURACIÓN DE BD ====================

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "image_optimizer")

# ==================== CONFIGURACIÓN DE STORAGE ====================

# Digital Ocean Spaces
SPACES_ENDPOINT = os.getenv("SPACES_ENDPOINT", "https://nyc3.digitaloceanspaces.com")
SPACES_REGION = os.getenv("SPACES_REGION", "nyc3")
SPACES_ACCESS_KEY = os.getenv("SPACES_ACCESS_KEY", "")
SPACES_SECRET_KEY = os.getenv("SPACES_SECRET_KEY", "")
SPACES_BUCKET = os.getenv("SPACES_BUCKET", "optimized-images")
SPACES_CDN_URL = os.getenv("SPACES_CDN_URL", "https://optimized-images.nyc3.cdn.digitaloceanspaces.com")

# ==================== CONFIGURACIÓN DE LOGGING ====================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "app.log")
