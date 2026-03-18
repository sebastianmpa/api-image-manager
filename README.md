# Optimized Image API

API para optimizar imágenes usando FastAPI y Clean Architecture.

## Uso rápido

1. Instala dependencias:
   ```sh
   pip install -r requirements.txt
   ```
2. Ejecuta el servidor:
   ```sh
   uvicorn app.main:app --reload
   ```
3. Endpoint principal:
   - POST `/image/optimize`
   - Body JSON: `{ "url": "https://..." }`
   - Devuelve: Imagen JPG optimizada

## Pipeline de optimización
- Convierte la imagen a cuadrado 2000x2000 (padding si es necesario)
- Elimina fondo y lo reemplaza por blanco
- Refleja horizontalmente
- Exporta como JPG

## Estructura
- `app/api`: Rutas y controladores
- `app/services`: Lógica de aplicación
- `app/domain`: Pipeline de imagen
- `app/utils`: Utilidades (descarga, etc)

## Extensión futura
Preparado para agregar más pasos o endpoints.
