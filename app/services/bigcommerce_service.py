"""
Servicio de integración con BigCommerce.
Flujo: Buscar producto en MongoDB → obtener credenciales MySQL → gestionar imágenes BigCommerce.
"""

import logging
import os
import atexit
from typing import List, Dict, Optional, Tuple

import requests
import mysql.connector
from mysql.connector import Error as MySQLError
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ==================== MONGO CLIENT (REUTILIZABLE) ====================

_mongo_client: Optional[MongoClient] = None


def _get_mongo_client() -> Optional[MongoClient]:
    """Cliente MongoDB reutilizable con pool mínimo."""
    global _mongo_client
    try:
        if _mongo_client is None:
            host = os.getenv("MONGO_HOST")
            port = int(os.getenv("MONGO_PORT", 27017))
            username = os.getenv("MONGO_USER", "")
            password = os.getenv("MONGO_PASS", "")

            conn_kwargs = {
                "host": host,
                "port": port,
                "serverSelectionTimeoutMS": 5000,
                "connectTimeoutMS": 5000,
                "socketTimeoutMS": 5000,
                "maxPoolSize": 5,
                "minPoolSize": 0,
                "maxIdleTimeMS": 30000,
            }
            if username and password:
                conn_kwargs["username"] = username
                conn_kwargs["password"] = password

            _mongo_client = MongoClient(**conn_kwargs)
            logger.debug(f"MongoDB conectado: {host}:{port}")
        return _mongo_client
    except Exception as e:
        logger.error(f"Error conectando a MongoDB: {e}")
        return None


def _close_mongo_client():
    global _mongo_client
    if _mongo_client:
        _mongo_client.close()
        _mongo_client = None


atexit.register(_close_mongo_client)


# ==================== NORMALIZACIÓN DE MARCA ====================

def _normalize_brand(brand: str) -> List[str]:
    """
    Genera variantes en MAYÚSCULAS de la marca para búsqueda flexible.
    """
    brand_clean = brand.strip().upper()
    variants = [brand_clean]

    known_mappings = {
        "BRIGGS": ["BRIGGS & STRATTON", "BRIGGS&STRATTON", "BRIGGS AND STRATTON"],
        "ECHO": ["ECHO", "ECHO INC"],
        "HONDA": ["HONDA", "HONDA MOTOR"],
        "HUSTLER": ["HUSTLER", "HUSTLER TURF"],
        "SCAG": ["SCAG", "SCAG POWER EQUIPMENT"],
    }
    if brand_clean in known_mappings:
        variants.extend(known_mappings[brand_clean])

    # Generar variantes con/sin & para marcas multipalabra
    if "&" not in brand_clean and len(brand_clean.split()) == 2:
        w = brand_clean.split()
        variants += [f"{w[0]} & {w[1]}", f"{w[0]}&{w[1]}", f"{w[0]} AND {w[1]}"]
    elif "&" in brand_clean:
        variants.append(brand_clean.replace("&", "").replace("  ", " ").strip())
        variants.append(brand_clean.replace("&", " ").replace("  ", " ").strip())
        variants.append(brand_clean.replace("&", "AND").replace("  ", " ").strip())

    # Eliminar duplicados manteniendo orden
    seen = set()
    unique = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


# ==================== BÚSQUEDA EN MONGODB ====================

def search_products_by_brand_and_sku(brand: str, sku: str) -> List[Dict]:
    """
    Busca productos en la colección Products de MongoDB por BRAND y SKU/MPN.

    :param brand: Marca del producto (cualquier capitalización)
    :param sku: SKU o MPN del producto
    :return: Lista de productos encontrados
    """
    try:
        client = _get_mongo_client()
        if not client:
            logger.error("No se pudo conectar a MongoDB")
            return []

        db_name = os.getenv("MONGO_DBNAME", "Prontoweb")
        collection = client[db_name]["Products"]

        brand_variants = _normalize_brand(brand)
        logger.debug(f"Buscando brand={brand_variants}, sku/mpn={sku}")

        search_filter = {
            "BRAND": {"$in": brand_variants},
            "$or": [{"SKU": sku}, {"MPN": sku}],
        }

        products = list(collection.find(search_filter))

        if products:
            logger.info(f"Se encontraron {len(products)} producto(s) para brand={brand}, sku={sku}")
            for p in products:
                logger.debug(
                    f"  → ID={p.get('ID')} | BRAND={p.get('BRAND')} "
                    f"| SKU={p.get('SKU')} | MPN={p.get('MPN')} | STOREID={p.get('STOREID')}"
                )
        else:
            logger.warning(f"Sin resultados para brand={brand}, sku={sku}")

            # Debug: buscar solo por marca o solo por sku para diagnóstico
            brand_sample = list(collection.find({"BRAND": brand_variants[0]}).limit(2))
            sku_sample = list(collection.find({"$or": [{"SKU": sku}, {"MPN": sku}]}).limit(2))
            if brand_sample:
                logger.debug(f"  Productos con esa BRAND: {[p.get('SKU') for p in brand_sample]}")
            if sku_sample:
                logger.debug(f"  Productos con ese SKU/MPN: {[p.get('BRAND') for p in sku_sample]}")

        return products

    except Exception as e:
        logger.error(f"Error al buscar en MongoDB: {e}", exc_info=True)
        return []


# ==================== CREDENCIALES MYSQL ====================

def get_store_credentials(store_id: int) -> Dict:
    """
    Obtiene ACCESSTOKEN, STOREHASH y CLIENTID desde MySQL prontoweb.stores.

    :param store_id: ID de la tienda
    :return: Dict con las credenciales
    :raises ValueError: si no se encuentra la tienda o falla la conexión
    """
    connection = None
    try:
        connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST"),
            user=os.getenv("MYSQL_USER"),
            password=os.getenv("MYSQL_PASSWORD"),
            database=os.getenv("MYSQL_DATABASE", "prontoweb"),
            connection_timeout=5,
        )

        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT ACCESSTOKEN, STOREHASH, CLIENTID FROM stores WHERE id = %s",
            (store_id,),
        )
        row = cursor.fetchone()

        if not row:
            raise ValueError(f"No se encontró la tienda con id={store_id}")

        logger.info(f"Credenciales obtenidas para store_id={store_id} | STOREHASH={row.get('STOREHASH')}")
        return {
            "access_token": row["ACCESSTOKEN"],
            "store_hash": row["STOREHASH"],
            "client_id": row["CLIENTID"],
        }

    except MySQLError as e:
        logger.error(f"Error MySQL para store_id={store_id}: {e}")
        raise ValueError(f"Error de base de datos: {e}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


# ==================== BIGCOMMERCE API ====================

def _bc_headers(access_token: str, client_id: str = "") -> Dict:
    return {
        "X-Auth-Token": access_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_product_images(
    product_id: int,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> List[Dict]:
    """
    Retorna la lista de imágenes del producto en BigCommerce (v3).
    """
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/images"
    try:
        resp = requests.get(url, headers=_bc_headers(access_token, client_id), timeout=10)
        if resp.status_code == 200:
            images = resp.json().get("data", [])
            logger.info(f"Producto {product_id}: {len(images)} imagen(es) actuales")
            return images
        elif resp.status_code == 204:
            return []
        else:
            logger.warning(f"BigCommerce GET images: status={resp.status_code} body={resp.text[:200]}")
            return []
    except Exception as e:
        logger.error(f"Error obteniendo imágenes del producto {product_id}: {e}")
        return []


def delete_product_image(
    product_id: int,
    image_id: int,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> Tuple[bool, str]:
    """
    Elimina una imagen de un producto en BigCommerce (v3).

    :return: (éxito, mensaje)
    """
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/images/{image_id}"
    try:
        resp = requests.delete(url, headers=_bc_headers(access_token), timeout=10)
        if resp.status_code == 204:
            logger.info(f"  ✓ Imagen {image_id} eliminada del producto {product_id}")
            return True, f"Imagen {image_id} eliminada"
        elif resp.status_code == 404:
            logger.warning(f"  ⚠ Imagen {image_id} no encontrada (404)")
            return False, f"Imagen {image_id} no encontrada"
        elif resp.status_code == 401:
            logger.error(f"  ✗ Error de autenticación al eliminar imagen {image_id}")
            return False, "Error de autenticación en BigCommerce"
        else:
            msg = f"status={resp.status_code} body={resp.text[:200]}"
            logger.warning(f"  ✗ No se pudo eliminar imagen {image_id}: {msg}")
            return False, msg
    except Exception as e:
        logger.error(f"Error eliminando imagen {image_id}: {e}")
        return False, str(e)


def upload_product_image(
    product_id: int,
    image_url: str,
    access_token: str,
    store_hash: str,
    client_id: str,
    is_thumbnail: bool = True,
    description: str = "",
    sort_order: int = 0,
) -> Tuple[bool, str]:
    """
    Sube una imagen a un producto en BigCommerce desde una URL pública (v3).
    La API v3 acepta image_url directamente sin necesidad de subir el archivo.

    :return: (éxito, mensaje o id de la imagen creada)
    """
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/images"
    payload = {
        "image_url": image_url,
        "is_thumbnail": is_thumbnail,
        "description": description,
        "sort_order": sort_order,
    }
    try:
        resp = requests.post(
            url,
            headers=_bc_headers(access_token, client_id),
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json().get("data", {})
            image_id = data.get("id", "?")
            logger.info(f"  ✓ Imagen subida al producto {product_id}: image_id={image_id}")
            return True, str(image_id)
        else:
            msg = f"status={resp.status_code} body={resp.text[:300]}"
            logger.error(f"  ✗ Error subiendo imagen al producto {product_id}: {msg}")
            return False, msg
    except Exception as e:
        logger.error(f"Error subiendo imagen al producto {product_id}: {e}")
        return False, str(e)


# ==================== CUSTOM FIELDS (v3) ====================

def _get_custom_fields(
    product_id: int,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> List[Dict]:
    """Retorna todos los custom fields del producto."""
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/custom-fields"
    try:
        resp = requests.get(url, headers=_bc_headers(access_token, client_id), timeout=10)
        if resp.status_code == 200:
            return resp.json().get("data", [])
        logger.warning(f"GET custom-fields status={resp.status_code} body={resp.text[:200]}")
        return []
    except Exception as e:
        logger.error(f"Error obteniendo custom fields del producto {product_id}: {e}")
        return []


def _create_custom_field(
    product_id: int,
    name: str,
    value: str,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> Tuple[bool, str]:
    """Crea un nuevo custom field."""
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/custom-fields"
    try:
        resp = requests.post(
            url,
            headers=_bc_headers(access_token, client_id),
            json={"name": name, "value": value},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            field_id = resp.json().get("data", {}).get("id", "?")
            logger.info(f"  ✓ Custom field '{name}' creado (id={field_id}) en producto {product_id}")
            return True, str(field_id)
        msg = f"status={resp.status_code} body={resp.text[:200]}"
        logger.error(f"  ✗ Error creando custom field '{name}': {msg}")
        return False, msg
    except Exception as e:
        logger.error(f"Error creando custom field '{name}': {e}")
        return False, str(e)


def _update_custom_field(
    product_id: int,
    field_id: int,
    value: str,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> Tuple[bool, str]:
    """Actualiza el valor de un custom field existente."""
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/custom-fields/{field_id}"
    try:
        resp = requests.put(
            url,
            headers=_bc_headers(access_token, client_id),
            json={"value": value},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"  ✓ Custom field id={field_id} actualizado a '{value}' en producto {product_id}")
            return True, str(field_id)
        msg = f"status={resp.status_code} body={resp.text[:200]}"
        logger.error(f"  ✗ Error actualizando custom field id={field_id}: {msg}")
        return False, msg
    except Exception as e:
        logger.error(f"Error actualizando custom field id={field_id}: {e}")
        return False, str(e)


def _delete_custom_field(
    product_id: int,
    field_id: int,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> bool:
    """Elimina un custom field por id."""
    url = f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/custom-fields/{field_id}"
    try:
        resp = requests.delete(url, headers=_bc_headers(access_token, client_id), timeout=10)
        return resp.status_code == 204
    except Exception as e:
        logger.error(f"Error eliminando custom field id={field_id}: {e}")
        return False


def upsert_custom_field(
    product_id: int,
    name: str,
    value: str,
    access_token: str,
    store_hash: str,
    client_id: str,
) -> Tuple[bool, str]:
    """
    Crea o actualiza el custom field 'name' con 'value'.
    Si hay duplicados los elimina y crea uno limpio.

    :return: (éxito, acción realizada)
    """
    existing = _get_custom_fields(product_id, access_token, store_hash, client_id)
    matches = [f for f in existing if f.get("name") == name]

    if len(matches) == 0:
        # No existe → crear
        ok, result = _create_custom_field(product_id, name, value, access_token, store_hash, client_id)
        return ok, "created" if ok else result

    elif len(matches) == 1:
        field = matches[0]
        if str(field.get("value", "")).strip() == str(value).strip():
            logger.info(f"  ─ Custom field '{name}' ya tiene valor '{value}', sin cambios")
            return True, "no_change"
        # Actualizar
        ok, result = _update_custom_field(product_id, field["id"], value, access_token, store_hash, client_id)
        return ok, "updated" if ok else result

    else:
        # Duplicados → eliminar todos y crear uno nuevo
        logger.warning(f"  Duplicados detectados para '{name}' ({len(matches)}), limpiando...")
        for f in matches:
            _delete_custom_field(product_id, f["id"], access_token, store_hash, client_id)
        ok, result = _create_custom_field(product_id, name, value, access_token, store_hash, client_id)
        return ok, "cleaned_and_created" if ok else result


# ==================== ORQUESTADOR PRINCIPAL ====================

def process_bigcommerce_image_update(
    brand: str,
    sku: str,
    processed_image_url: str,
    task_id: str,
) -> Dict:
    """
    Orquesta el proceso completo:
      1. Buscar producto en MongoDB
      2. Obtener credenciales de tienda desde MySQL
      3. Eliminar imágenes actuales del producto en BigCommerce
      4. Subir la imagen procesada nueva

    :param brand: Marca del producto
    :param sku: SKU o MPN del producto
    :param processed_image_url: URL pública de la imagen procesada
    :param task_id: ID de tarea (para logging)
    :return: Resumen de resultados
    """
    summary = {
        "task_id": task_id,
        "brand": brand,
        "sku": sku,
        "products_found": 0,
        "stores_processed": [],
        "errors": [],
    }

    logger.info(f"[{task_id}] ── Iniciando actualización BigCommerce: brand={brand}, sku={sku}")

    # 1. Buscar productos en MongoDB
    products = search_products_by_brand_and_sku(brand, sku)
    if not products:
        msg = f"No se encontraron productos en MongoDB para brand={brand}, sku={sku}"
        logger.warning(f"[{task_id}] {msg}")
        summary["errors"].append(msg)
        return summary

    summary["products_found"] = len(products)

    # 2. Procesar cada producto (puede estar en varias tiendas)
    for product in products:
        bc_product_id = product.get("ID")
        store_id = product.get("STOREID")

        if not bc_product_id or not store_id:
            msg = f"Producto sin ID o STOREID: {product.get('_id')}"
            logger.warning(f"[{task_id}] {msg}")
            summary["errors"].append(msg)
            continue

        store_result = {
            "store_id": store_id,
            "bc_product_id": bc_product_id,
            "deleted_images": [],
            "uploaded_image_id": None,
            "error": None,
        }

        try:
            # 3. Obtener credenciales MySQL
            credentials = get_store_credentials(store_id)
            access_token = credentials["access_token"]
            store_hash = credentials["store_hash"]
            client_id = credentials["client_id"]

            logger.info(
                f"[{task_id}] Procesando store_id={store_id} | "
                f"store_hash={store_hash} | bc_product_id={bc_product_id}"
            )

            # 4. Obtener imágenes actuales
            existing_images = get_product_images(bc_product_id, access_token, store_hash, client_id)

            # 5. Eliminar solo imágenes de logo/default (no las que ya fueron procesadas)
            logger.info(f"[{task_id}] Revisando {len(existing_images)} imagen(es) existente(s)...")
            for img in existing_images:
                img_id = img.get("id")
                alt_text = img.get("alt", "").lower()
                is_thumbnail = img.get("is_thumbnail", False)
                
                # Solo eliminar si es logo o default (primera vez)
                # Si tiene alt="NWM" o es una imagen procesada, no eliminar
                if img_id and ("logo" in alt_text or "default" in alt_text or is_thumbnail == False):
                    success, msg = delete_product_image(
                        bc_product_id, img_id, access_token, store_hash, client_id
                    )
                    if success:
                        store_result["deleted_images"].append(img_id)
                        logger.info(f"[{task_id}] ✓ Eliminado logo/default: {img_id}")
                    else:
                        logger.warning(f"[{task_id}] No se pudo eliminar imagen {img_id}: {msg}")
                else:
                    logger.info(f"[{task_id}] ⊝ Conservando imagen existente: {img_id} (alt='{alt_text}')")

            # 6. Subir la imagen procesada nueva (description = PRODUCTNAME del producto)
            product_name = product.get("PRODUCTNAME", f"{brand} {sku}")
            logger.info(f"[{task_id}] Subiendo nueva imagen: {processed_image_url}")
            success, result = upload_product_image(
                bc_product_id,
                processed_image_url,
                access_token,
                store_hash,
                client_id,
                is_thumbnail=True,
                description=product_name,
                sort_order=0,
            )

            if success:
                store_result["uploaded_image_id"] = result
                logger.info(f"[{task_id}] ✅ Imagen actualizada en store={store_id}, product={bc_product_id}")

                # 7. Upsert custom field __IMG = "NWM"
                logger.info(f"[{task_id}] Actualizando custom field __IMG...")
                cf_ok, cf_action = upsert_custom_field(
                    bc_product_id, "__IMG", "NWM",
                    access_token, store_hash, client_id,
                )
                store_result["custom_field_img"] = cf_action if cf_ok else f"error: {cf_action}"
                if cf_ok:
                    logger.info(f"[{task_id}] ✅ Custom field __IMG=NWM → {cf_action}")
                else:
                    logger.error(f"[{task_id}] ✗ Error en custom field __IMG: {cf_action}")
            else:
                store_result["error"] = f"Upload fallido: {result}"
                logger.error(f"[{task_id}] ✗ Upload fallido para store={store_id}: {result}")

        except ValueError as e:
            store_result["error"] = str(e)
            logger.error(f"[{task_id}] Error en store_id={store_id}: {e}")

        summary["stores_processed"].append(store_result)

    logger.info(
        f"[{task_id}] ── Finalizado: {len(summary['stores_processed'])} tienda(s) procesada(s), "
        f"{len(summary['errors'])} error(es)"
    )
    return summary
