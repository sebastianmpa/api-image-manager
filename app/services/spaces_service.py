import os
import boto3
from botocore.client import Config
import re
from urllib.parse import quote


def _sanitize_filename(filename: str) -> str:
    """Sanitiza el nombre del archivo eliminando caracteres inválidos."""
    # Remover extensión original y agregar .jpg
    name = os.path.splitext(filename)[0]
    # Remover caracteres especiales, dejar solo alfanuméricos, guiones y guiones bajos
    name = re.sub(r'[^\w\-]', '', name)
    # Remover espacios
    name = name.replace(' ', '_')
    # Limitar a 100 caracteres
    name = name[:100]
    return f"{name}.jpg"


def _get_spaces_client():
    """Obtiene un cliente S3 configurado para Digital Ocean Spaces."""
    DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
    DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
    DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
    
    if not all([DO_SPACES_ENDPOINT, DO_SPACES_KEY, DO_SPACES_SECRET]):
        raise ValueError(
            "Variables de Digital Ocean Spaces no configuradas. "
            "Verifica que .env contenga: DO_SPACES_ENDPOINT, DO_SPACES_KEY, DO_SPACES_SECRET"
        )
    
    session = boto3.session.Session()
    return session.client(
        's3',
        region_name='sfo3',
        endpoint_url=DO_SPACES_ENDPOINT,
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET,
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'virtual'}  # IMPORTANTE: Digital Ocean requiere esto
        )
    )


def upload_image_to_spaces(file_bytes, filename, content_type):
    """Sube una imagen a DigitalOcean Spaces y retorna la URL pública directa."""
    DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
    DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
    
    if not all([DO_SPACES_ENDPOINT, DO_SPACES_BUCKET]):
        raise ValueError("Variables de Digital Ocean Spaces no configuradas.")
    
    # Sanitizar el nombre del archivo
    safe_filename = _sanitize_filename(filename)
    
    client = _get_spaces_client()
    
    try:
        # Subir el archivo con ACL público
        client.put_object(
            Bucket=DO_SPACES_BUCKET, 
            Key=safe_filename, 
            Body=file_bytes, 
            ContentType=content_type,
            ACL='public-read'
        )
        
        # URL directa simple con bucket en el hostname
        # Formato: https://bucket.sfo3.digitaloceanspaces.com/filename
        url = f"https://{DO_SPACES_BUCKET}.sfo3.digitaloceanspaces.com/{safe_filename}"
        
        print(f"✓ Imagen subida: {safe_filename} (tamaño: {len(file_bytes)} bytes)")
        print(f"  URL: {url}")
        print(f"  Longitud URL: {len(url)} caracteres")
        
        return url
    except Exception as e:
        print(f"✗ Error subiendo {safe_filename}: {str(e)}")
        raise


def get_image_from_spaces(filename):
    """Obtiene una imagen de Digital Ocean Spaces."""
    DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
    
    if not DO_SPACES_BUCKET:
        raise ValueError("Variables de Digital Ocean Spaces no configuradas.")
    
    client = _get_spaces_client()
    try:
        response = client.get_object(Bucket=DO_SPACES_BUCKET, Key=filename)
        return response['Body'].read(), response.get('ContentType', 'application/octet-stream')
    except client.exceptions.NoSuchKey:
        raise FileNotFoundError(f"La imagen '{filename}' no existe en Digital Ocean Spaces.")


def delete_image_from_spaces(filename):
    """Elimina una imagen de Digital Ocean Spaces."""
    DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
    
    if not DO_SPACES_BUCKET:
        raise ValueError("Variables de Digital Ocean Spaces no configuradas.")
    
    client = _get_spaces_client()
    client.delete_object(Bucket=DO_SPACES_BUCKET, Key=filename)
    return {"message": f"Imagen '{filename}' eliminada correctamente"}


def update_image_in_spaces(filename, new_file_bytes, content_type):
    """Actualiza una imagen en Digital Ocean Spaces."""
    DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
    DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
    
    if not all([DO_SPACES_ENDPOINT, DO_SPACES_BUCKET]):
        raise ValueError("Variables de Digital Ocean Spaces no configuradas.")
    
    client = _get_spaces_client()
    # Reemplazar el archivo
    client.put_object(Bucket=DO_SPACES_BUCKET, Key=filename, Body=new_file_bytes, ContentType=content_type)
    url = f"{DO_SPACES_ENDPOINT}/{filename}"
    return url


def list_images_from_spaces(prefix: str = "", page: int = 1, limit: int = 25):
    """
    Lista las imágenes en Digital Ocean Spaces con paginación.
    
    Args:
        prefix: Prefijo para filtrar imágenes
        page: Número de página (comenzando en 1)
        limit: Cantidad de elementos por página
    
    Returns:
        Dict con total, página, imágenes y metadatos de paginación
    """
    DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
    DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
    
    if not all([DO_SPACES_BUCKET, DO_SPACES_ENDPOINT]):
        raise ValueError("Variables de Digital Ocean Spaces no configuradas.")
    
    if page < 1:
        page = 1
    if limit < 1 or limit > 100:
        limit = 10
    
    client = _get_spaces_client()
    try:
        # Obtener todas las imágenes (Digital Ocean retorna hasta 1000 por defecto)
        response = client.list_objects_v2(Bucket=DO_SPACES_BUCKET, Prefix=prefix)
        
        if 'Contents' not in response:
            return {
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 0,
                "images": []
            }
        
        # Convertir a lista y ordenar por fecha (más recientes primero)
        images_list = []
        for obj in response['Contents']:
            images_list.append({
                "filename": obj['Key'],
                "size": obj['Size'],
                "last_modified": obj['LastModified'].isoformat(),
                "url": f"https://{DO_SPACES_BUCKET}.sfo3.digitaloceanspaces.com/{obj['Key']}"
            })
        
        # Ordenar por fecha (más recientes primero)
        images_list.sort(key=lambda x: x['last_modified'], reverse=True)
        
        # Calcular paginación
        total = len(images_list)
        total_pages = (total + limit - 1) // limit  # Redondear hacia arriba
        
        # Calcular índices
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        
        # Obtener página específica
        paginated_images = images_list[start_idx:end_idx]
        
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
            "images": paginated_images
        }
    except Exception as e:
        raise Exception(f"Error listando imágenes: {str(e)}")
