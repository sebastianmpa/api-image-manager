"""
Espejo en MySQL de las revisiones humanas de task-hub (tipo "images-update").

Esas aprobaciones/rechazos viven en el backend de task-hub, no en MongoDB, así que no
aparecen en image_manager_stats. Este servicio lee el endpoint
GET {TASKHUB_API_URL}/api/item-approval/v1 (todas las páginas) y hace upsert por `id`
en prontoweb.image_review_approvals: una fila por imagen revisada (o pendiente de
revisar), con quién la revisó y cuándo. Correrlo varias veces no duplica nada.

Se ejecuta solo cada TASKHUB_SYNC_MINUTES (ver start_review_sync_loop) y a mano con
POST /candidates/stats/sync-reviews.
"""

import os
import json
import logging
import threading
from datetime import datetime
from typing import Optional

import requests
from mysql.connector import Error as MySQLError

from app.services.mysql_stats_service import _get_connection

logger = logging.getLogger(__name__)

TABLE_NAME = "image_review_approvals"
REVIEWS_VIEW = "vw_image_review_aprobaciones"
TASK_TYPE = "images-update"
PAGE_SIZE = 500

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` varchar(36) NOT NULL,
  `item_id` varchar(36) DEFAULT NULL,
  `chunk_id` varchar(36) DEFAULT NULL,
  `task_type` varchar(50) DEFAULT NULL,
  `brand` varchar(100) DEFAULT NULL,
  `sku` varchar(100) DEFAULT NULL,
  `product_name` varchar(500) DEFAULT NULL,
  `store_ids` json DEFAULT NULL,
  `image_url` text,
  `approved` tinyint(1) DEFAULT NULL,
  `email` varchar(100) DEFAULT NULL,
  `user_id` varchar(36) DEFAULT NULL,
  `reviewed_at` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `synced_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_reviewed_at` (`reviewed_at`),
  KEY `idx_email` (`email`),
  KEY `idx_chunk_id` (`chunk_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Una fila por imagen aprobada y subida a BigCommerce, venga de donde venga:
#   - bot con POST /candidates/approve-images (source = la del candidato: scraper, warehouse...)
#   - subida manual con POST /candidates/upload-product-image (source = MANUAL_UPLOAD_SOURCE)
#   - subida directa con POST /bigcommerce/upload-image (source = BIGCOMMERCE_UPLOAD_SOURCE)
CANDIDATE_APPROVALS_TABLE = "image_candidate_approvals"
MANUAL_UPLOAD_SOURCE = "manual_upload"
BIGCOMMERCE_UPLOAD_SOURCE = "bigcommerce_upload"

CREATE_CANDIDATE_APPROVALS_SQL = f"""
CREATE TABLE IF NOT EXISTS `{CANDIDATE_APPROVALS_TABLE}` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `chunk_id` varchar(100) DEFAULT NULL,
  `candidate_id` varchar(24) DEFAULT NULL,
  `brand` varchar(100) DEFAULT NULL,
  `mpn` varchar(100) DEFAULT NULL,
  `image_url` text,
  `source` varchar(50) DEFAULT NULL,
  `lote` varchar(50) DEFAULT NULL,
  `email` varchar(100) DEFAULT NULL,
  `bc_stores_ok` int(11) NOT NULL DEFAULT 0,
  `bc_stores_failed` int(11) NOT NULL DEFAULT 0,
  `is_diagram` tinyint(1) DEFAULT NULL,
  `is_thumbnail` tinyint(1) DEFAULT NULL,
  `approved_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_approved_at` (`approved_at`),
  KEY `idx_email` (`email`),
  KEY `idx_candidate_id` (`candidate_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Vista para Power BI: sin JSON, con el estado en texto y `fecha` para relacionar con
# vw_image_manager_calendario. Contar filas = contar imágenes (no productos).
# Junta las revisiones humanas de task-hub con las aprobaciones automáticas del bot (/approve-images);
# `origen` las distingue.
REVIEWS_VIEW_SQL = f"""
CREATE OR REPLACE VIEW `{REVIEWS_VIEW}` AS
SELECT
  r.`id`,
  'Revisión task-hub (images-update)' AS `origen`,
  r.`chunk_id`,
  r.`brand`,
  r.`sku`,
  r.`product_name`,
  r.`image_url`,
  CASE WHEN r.`approved` = 1 THEN 'Aprobada' WHEN r.`approved` = 0 THEN 'Rechazada' ELSE 'Pendiente' END AS `estado`,
  IFNULL(r.`approved` = 1, 0) AS `es_aprobada`,
  IFNULL(r.`approved` = 0, 0) AS `es_rechazada`,
  (r.`approved` IS NULL) AS `es_pendiente`,
  r.`email`,
  r.`reviewed_at`,
  DATE(IFNULL(r.`reviewed_at`, r.`created_at`)) AS `fecha`,
  r.`created_at`,
  NULL AS `es_diagrama`,
  NULL AS `es_principal`,
  NULL AS `bc_tiendas_ok`,
  NULL AS `bc_tiendas_error`
FROM `{TABLE_NAME}` r
WHERE r.`deleted_at` IS NULL
UNION ALL
SELECT
  CONCAT('cand-', a.`id`),
  CASE a.`source`
    WHEN 'warehouse' THEN 'Aprobación automática (bot) - Warehouse'
    WHEN '{MANUAL_UPLOAD_SOURCE}' THEN 'Subida manual'
    WHEN '{BIGCOMMERCE_UPLOAD_SOURCE}' THEN 'Subida directa a BigCommerce'
    ELSE 'Aprobación automática (bot)'
  END,
  a.`chunk_id`,
  a.`brand`,
  a.`mpn`,
  NULL,
  a.`image_url`,
  'Aprobada', 1, 0, 0,
  a.`email`,
  a.`approved_at`,
  DATE(a.`approved_at`),
  a.`approved_at`,
  a.`is_diagram`,
  a.`is_thumbnail`,
  a.`bc_stores_ok`,
  a.`bc_stores_failed`
FROM `{CANDIDATE_APPROVALS_TABLE}` a
"""

COLUMNS = [
    "id", "item_id", "chunk_id", "task_type", "brand", "sku", "product_name", "store_ids",
    "image_url", "approved", "email", "user_id", "reviewed_at", "created_at", "updated_at", "deleted_at",
]

_table_ready = False
_sync_lock = threading.Lock()


def _taskhub_url() -> str:
    return os.getenv("TASKHUB_API_URL", "http://10.1.10.124:3002").rstrip("/")


def _utc_to_local(value: Optional[str]) -> Optional[datetime]:
    """ISO UTC de task-hub ('2026-11-03T12:37:22.966Z') -> hora local del servidor, igual que image_manager_stats."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone().replace(tzinfo=None, microsecond=0)


def _reviewed_at(record: dict) -> Optional[datetime]:
    # date + time ya vienen en hora local; dateTime (UTC) como respaldo
    if record.get("date") and record.get("time"):
        return datetime.strptime(f"{record['date']} {record['time']}", "%Y-%m-%d %H:%M:%S")
    return _utc_to_local(record.get("dateTime"))


def _to_row(record: dict) -> list:
    image = (record.get("item") or {}).get("product_image") or {}
    names = image.get("product_name") or []
    if isinstance(names, str):
        names = [names]
    approved = record.get("approved")
    return [
        record["id"],
        record.get("itemId"),
        record.get("chunkId"),
        record.get("taskTypeInternalName"),
        (image.get("brand") or "").strip().upper() or None,
        image.get("sku"),
        (names[0][:500] if names else None),
        json.dumps(image.get("store_id")) if image.get("store_id") is not None else None,
        image.get("original_image_url") or image.get("original_candidate_url"),
        None if approved is None else int(bool(approved)),
        record.get("email"),
        record.get("userId"),
        _reviewed_at(record),
        _utc_to_local(record.get("createdAt")),
        _utc_to_local(record.get("updatedAt")),
        _utc_to_local(record.get("deletedAt")),
    ]


def fetch_review_records(task_type: str = TASK_TYPE) -> list:
    """Trae todas las páginas del endpoint de item-approval de task-hub."""
    records, page = [], 1
    while True:
        response = requests.get(
            f"{_taskhub_url()}/api/item-approval/v1",
            params={"page": page, "limit": PAGE_SIZE, "task_type": task_type},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        records.extend(data.get("items") or [])
        if page >= (data.get("totalPages") or 1):
            return records
        page += 1


def _ensure_table(cursor):
    """Crea las dos tablas que alimentan la vista (la vista falla si falta alguna) y la vista."""
    global _table_ready
    if not _table_ready:
        cursor.execute(CREATE_TABLE_SQL)
        cursor.execute(CREATE_CANDIDATE_APPROVALS_SQL)
        # Columnas agregadas después de crear la tabla (en producción ya existe sin ellas)
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (CANDIDATE_APPROVALS_TABLE,),
        )
        existing = {row[0] for row in cursor.fetchall()}
        for col in ("is_diagram", "is_thumbnail"):
            if col not in existing:
                cursor.execute(f"ALTER TABLE `{CANDIDATE_APPROVALS_TABLE}` ADD COLUMN `{col}` tinyint(1) DEFAULT NULL AFTER `bc_stores_failed`")
        cursor.execute(REVIEWS_VIEW_SQL)
        _table_ready = True


def save_candidate_approvals(rows: list) -> bool:
    """
    Guarda en image_candidate_approvals las imágenes aprobadas y subidas a BigCommerce
    (bot, subida manual o subida directa). Cada fila es un dict con chunk_id, candidate_id,
    brand, mpn, image_url, source, lote, email, bc_stores_ok, bc_stores_failed y
    opcionalmente is_diagram / is_thumbnail. Nunca lanza: si MySQL falla solo se loguea.
    """
    if not rows:
        return True
    columns = ["chunk_id", "candidate_id", "brand", "mpn", "image_url", "source", "lote",
               "email", "bc_stores_ok", "bc_stores_failed", "is_diagram", "is_thumbnail"]
    connection = None
    try:
        connection = _get_connection()
        cursor = connection.cursor()
        _ensure_table(cursor)
        cursor.executemany(
            f"INSERT INTO `{CANDIDATE_APPROVALS_TABLE}` ({', '.join(f'`{c}`' for c in columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))})",
            [[row.get(c) for c in columns] for row in rows],
        )
        connection.commit()
        logger.info(f"{len(rows)} aprobación(es) de candidatos guardada(s) en MySQL")
        return True
    except MySQLError as e:
        logger.error(f"Error MySQL guardando aprobaciones de candidatos: {e}")
    except Exception as e:
        logger.error(f"Error guardando aprobaciones de candidatos: {e}", exc_info=True)
    finally:
        if connection and connection.is_connected():
            connection.close()
    return False


def get_candidate_approval_results(candidate_ids: list) -> dict:
    """
    Último resultado de BigCommerce guardado para cada candidate_id:
    {candidate_id: {bc_stores_ok, bc_stores_failed, is_thumbnail, approved_at, source}}.
    Los que no aparecen todavía no tienen resultado (envío en curso o anterior al registro).
    Nunca lanza: si MySQL falla devuelve {}.
    """
    ids = [str(c) for c in candidate_ids if c]
    if not ids:
        return {}
    connection = None
    try:
        connection = _get_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            f"SELECT candidate_id, bc_stores_ok, bc_stores_failed, is_thumbnail, approved_at, source "
            f"FROM `{CANDIDATE_APPROVALS_TABLE}` WHERE candidate_id IN ({', '.join(['%s'] * len(ids))}) "
            f"ORDER BY id",
            ids,
        )
        # ORDER BY id: si hay varias filas para el mismo candidato queda la más reciente
        return {row["candidate_id"]: row for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"Error leyendo resultados de BigCommerce de candidatos: {e}")
        return {}
    finally:
        if connection and connection.is_connected():
            connection.close()


def sync_review_approvals() -> Optional[dict]:
    """
    Sincroniza las revisiones de task-hub a MySQL.

    :return: resumen {fetched, approved, rejected, pending}, o None si falló
    """
    if not _sync_lock.acquire(blocking=False):
        logger.info("Sync de revisiones de task-hub ya en curso, se omite")
        return None
    connection = None
    try:
        records = fetch_review_records()
        rows = [_to_row(r) for r in records if r.get("id")]

        connection = _get_connection()
        cursor = connection.cursor()
        _ensure_table(cursor)
        if rows:
            updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in COLUMNS if c != "id")
            cursor.executemany(
                f"INSERT INTO `{TABLE_NAME}` ({', '.join(f'`{c}`' for c in COLUMNS)}) "
                f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) ON DUPLICATE KEY UPDATE {updates}",
                rows,
            )
        connection.commit()

        summary = {
            "fetched": len(rows),
            "approved": sum(1 for r in records if r.get("approved") is True),
            "rejected": sum(1 for r in records if r.get("approved") is False),
            "pending": sum(1 for r in records if r.get("approved") is None),
        }
        logger.info(f"Revisiones de task-hub sincronizadas en MySQL: {summary}")
        return summary

    except requests.RequestException as e:
        logger.error(f"Error leyendo revisiones de task-hub: {e}")
    except MySQLError as e:
        logger.error(f"Error MySQL sincronizando revisiones de task-hub: {e}")
    except Exception as e:
        logger.error(f"Error sincronizando revisiones de task-hub: {e}", exc_info=True)
    finally:
        if connection and connection.is_connected():
            connection.close()
        _sync_lock.release()
    return None


def start_review_sync_loop():
    """Hilo que sincroniza al arrancar y luego cada TASKHUB_SYNC_MINUTES (0 = desactivado)."""
    minutes = float(os.getenv("TASKHUB_SYNC_MINUTES", "15") or 0)
    if minutes <= 0:
        logger.info("Sync automático de revisiones de task-hub desactivado (TASKHUB_SYNC_MINUTES=0)")
        return
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            sync_review_approvals()
            stop.wait(minutes * 60)

    threading.Thread(target=loop, name="taskhub-review-sync", daemon=True).start()
    logger.info(f"Sync automático de revisiones de task-hub cada {minutes:g} min")
