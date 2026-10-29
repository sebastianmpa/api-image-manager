"""
Espejo en MySQL de las estadísticas globales de MongoDB (get_global_stats).

Cada vez que algo cambia las estadísticas se inserta una fila nueva en
prontoweb.image_manager_stats con TODOS los totales en ese momento (snapshot),
más qué evento la disparó. La última fila es el estado actual; las anteriores
son el histórico.

Nunca lanza excepciones hacia el endpoint: si MySQL falla solo se loguea, para
que la API siga funcionando aunque el espejo no se pueda escribir.
"""

import os
import json
import logging
from typing import Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from app.services.mongo_service import get_global_stats

logger = logging.getLogger(__name__)

TABLE_NAME = "image_manager_stats"

# clave de get_global_stats() -> columna MySQL
STATS_COLUMNS = {
    "pending":                 "pending",
    "processed":               "processed",
    "deleted":                 "deleted",
    "notFound":                "not_found",
    "failedImages":            "failed_images",
    "failedCandidates":        "failed_candidates",
    "total":                   "total",
    "manualUploads":           "manual_uploads",
    "images":                  "images",
    "diagrams":                "diagrams",
    "uniqueSkus":              "unique_skus",
    "secondaryImages":         "secondary_images",
    "botFoundCandidates":      "bot_found_candidates",
    "userApprovedCandidates":  "user_approved_candidates",
    "userRejectedCandidates":  "user_rejected_candidates",
    "autoProcessedImages":     "auto_processed_images",
    "totalManualImages":       "total_manual_images",
    "manualImagesPrimary":     "manual_images_primary",
    "manualImagesSecondary":   "manual_images_secondary",
    "totalManualDiagrams":     "total_manual_diagrams",
    "manualDiagramsPrimary":   "manual_diagrams_primary",
    "manualDiagramsSecondary": "manual_diagrams_secondary",
    "warehousePending":        "warehouse_pending",
    "warehouseApproved":       "warehouse_approved",
    "warehouseRejected":       "warehouse_rejected",
}

_stats_columns_sql = ",\n".join(f"  `{col}` int(11) NOT NULL DEFAULT 0" for col in STATS_COLUMNS.values())

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event` varchar(50) NOT NULL,
  `email` varchar(100) DEFAULT NULL,
  `brand` varchar(100) DEFAULT NULL,
  `mpn` varchar(100) DEFAULT NULL,
  `chunk_id` varchar(100) DEFAULT NULL,
{_stats_columns_sql},
  `raw_stats` json DEFAULT NULL,
  `backfilled` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_event` (`event`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# ==================== VISTAS PARA POWER BI ====================
#
# La tabla guarda snapshots de totales acumulados (no se pueden sumar). Para reportes
# se exponen vistas sin la columna JSON:
#   - vw_image_manager_stats_eventos: una fila por evento con delta_* (cuánto cambió cada
#     métrica en ese evento) -> estas sí se pueden sumar en Power BI.
#   - vw_image_manager_stats_diario: una fila por día (incluye días sin actividad) con los
#     totales al cierre del día -> usar "último valor" / no resumir.
#   - vw_image_manager_calendario: dimensión de fechas para relacionar ambas por `fecha`.

CALENDAR_VIEW = "vw_image_manager_calendario"
EVENTS_VIEW = "vw_image_manager_stats_eventos"
DAILY_VIEW = "vw_image_manager_stats_diario"

EVENT_LABELS = {
    "delete_candidate":    "Candidato rechazado (eliminado)",
    "manual_upload":       "Subida manual",
    "process_images":      "Lote procesado (optimizado)",
    "mark_as_processed":   "Lote marcado como procesado",
    "failed_image_status": "Cambio de estado de imagen fallida",
    "manual_sync":         "Sincronización manual",
    "warehouse_upload":    "Fotos subidas por Warehouse",
}

_event_label_sql = "CASE s.`event` " + " ".join(
    f"WHEN '{event}' THEN '{label}'" for event, label in EVENT_LABELS.items()
) + " ELSE s.`event` END"

# fila anterior en orden cronológico (created_at, id)
_previous_row_sql = f"""(
    SELECT p2.`id` FROM `{TABLE_NAME}` p2
    WHERE p2.`created_at` < s.`created_at` OR (p2.`created_at` = s.`created_at` AND p2.`id` < s.`id`)
    ORDER BY p2.`created_at` DESC, p2.`id` DESC LIMIT 1
)"""

_digits_sql = " UNION ALL ".join(f"SELECT {i} AS d" for i in range(10))
_delta_columns_sql = ",\n".join(f"  s.`{col}` - IFNULL(p.`{col}`, 0) AS `delta_{col}`" for col in STATS_COLUMNS.values())
_daily_columns_sql = ",\n".join(f"  IFNULL(s.`{col}`, 0) AS `{col}`" for col in STATS_COLUMNS.values())

POWERBI_VIEWS_SQL = [
    f"""
CREATE OR REPLACE VIEW `{CALENDAR_VIEW}` AS
SELECT
  c.`fecha`,
  YEAR(c.`fecha`) AS `anio`,
  QUARTER(c.`fecha`) AS `trimestre`,
  MONTH(c.`fecha`) AS `mes`,
  ELT(MONTH(c.`fecha`), 'Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto',
      'Septiembre','Octubre','Noviembre','Diciembre') AS `nombre_mes`,
  DATE_FORMAT(c.`fecha`, '%Y-%m') AS `anio_mes`,
  WEEK(c.`fecha`, 3) AS `semana_iso`,
  WEEKDAY(c.`fecha`) + 1 AS `dia_semana`,
  ELT(WEEKDAY(c.`fecha`) + 1, 'Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo') AS `nombre_dia`
FROM (
  SELECT DATE(mn.`min_fecha`) + INTERVAL n.`n` DAY AS `fecha`
  FROM (SELECT MIN(`created_at`) AS `min_fecha` FROM `{TABLE_NAME}`) mn
  CROSS JOIN (
    SELECT a.d + b.d * 10 + c.d * 100 + e.d * 1000 AS `n`
    FROM ({_digits_sql}) a CROSS JOIN ({_digits_sql}) b CROSS JOIN ({_digits_sql}) c CROSS JOIN ({_digits_sql}) e
  ) n
  WHERE DATE(mn.`min_fecha`) + INTERVAL n.`n` DAY <= CURDATE()
) c
""",
    f"""
CREATE OR REPLACE VIEW `{EVENTS_VIEW}` AS
SELECT
  s.`id`,
  s.`created_at` AS `fecha_hora`,
  DATE(s.`created_at`) AS `fecha`,
  HOUR(s.`created_at`) AS `hora`,
  s.`event`,
  {_event_label_sql} AS `evento`,
  s.`email`,
  s.`brand`,
  s.`mpn`,
  s.`chunk_id`,
  s.`backfilled` AS `es_migrado`,
{_delta_columns_sql}
FROM `{TABLE_NAME}` s
LEFT JOIN `{TABLE_NAME}` p ON p.`id` = {_previous_row_sql}
""",
    f"""
CREATE OR REPLACE VIEW `{DAILY_VIEW}` AS
SELECT
  c.`fecha`,
  (SELECT COUNT(*) FROM `{TABLE_NAME}` e
   WHERE e.`created_at` >= c.`fecha` AND e.`created_at` < c.`fecha` + INTERVAL 1 DAY) AS `eventos_del_dia`,
{_daily_columns_sql}
FROM `{CALENDAR_VIEW}` c
LEFT JOIN `{TABLE_NAME}` s ON s.`id` = (
  SELECT s2.`id` FROM `{TABLE_NAME}` s2
  WHERE s2.`created_at` < c.`fecha` + INTERVAL 1 DAY
  ORDER BY s2.`created_at` DESC, s2.`id` DESC LIMIT 1
)
""",
]

_table_ready = False


def _get_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "prontoweb"),
        connection_timeout=5,
    )


def create_powerbi_views(cursor):
    """Crea/actualiza las vistas para Power BI. Si falla solo se loguea: no bloquea los inserts."""
    try:
        for sql in POWERBI_VIEWS_SQL:
            cursor.execute(sql)
    except MySQLError as e:
        logger.error(f"Error creando vistas de Power BI: {e}")


def _add_missing_columns(cursor):
    """Agrega a la tabla ya existente las columnas de STATS_COLUMNS que se sumaron después de crearla."""
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (TABLE_NAME,),
    )
    existing = {row[0] for row in cursor.fetchall()}
    for col in STATS_COLUMNS.values():
        if col not in existing:
            cursor.execute(f"ALTER TABLE `{TABLE_NAME}` ADD COLUMN `{col}` int(11) NOT NULL DEFAULT 0 AFTER `chunk_id`")
            logger.info(f"Columna `{col}` agregada a {TABLE_NAME}")


def _ensure_table(cursor):
    global _table_ready
    if not _table_ready:
        cursor.execute(CREATE_TABLE_SQL)
        _add_missing_columns(cursor)
        create_powerbi_views(cursor)
        _table_ready = True


def insert_stats_rows(cursor, rows: list):
    """
    Inserta filas en image_manager_stats. Cada fila es un dict con `stats` (salida de
    get_global_stats) y opcionalmente event, email, brand, mpn, chunk_id, backfilled y
    created_at (si no viene created_at, MySQL usa la hora actual).
    """
    _ensure_table(cursor)
    columns = ["event", "email", "brand", "mpn", "chunk_id"] + list(STATS_COLUMNS.values()) + ["raw_stats", "backfilled", "created_at"]
    values = []
    for row in rows:
        stats = row["stats"]
        values.append(
            [row.get("event"), row.get("email"), row.get("brand"), row.get("mpn"), row.get("chunk_id")]
            + [int(stats.get(key) or 0) for key in STATS_COLUMNS]
            + [json.dumps(stats), int(bool(row.get("backfilled"))), row.get("created_at")]
        )
    cursor.executemany(
        f"INSERT INTO `{TABLE_NAME}` ({', '.join(f'`{c}`' for c in columns)}) "
        f"VALUES ({', '.join(['%s'] * (len(columns) - 1))}, COALESCE(%s, NOW()))",
        values,
    )


def save_stats_snapshot(
    event: str,
    email: Optional[str] = None,
    brand: Optional[str] = None,
    mpn: Optional[str] = None,
    chunk_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Calcula get_global_stats() e inserta una fila con esos valores en MySQL.

    :param event: qué disparó el snapshot (ej. "process_images", "delete_candidate", "manual_sync")
    :return: las estadísticas insertadas, o None si falló
    """
    connection = None
    try:
        stats = get_global_stats()

        connection = _get_connection()
        cursor = connection.cursor()
        insert_stats_rows(cursor, [{
            "stats": stats, "event": event, "email": email, "brand": brand, "mpn": mpn, "chunk_id": chunk_id
        }])
        connection.commit()
        logger.info(f"Snapshot de estadísticas guardado en MySQL (event={event}, id={cursor.lastrowid})")
        return stats

    except MySQLError as e:
        logger.error(f"Error MySQL guardando snapshot de estadísticas (event={event}): {e}")
    except Exception as e:
        logger.error(f"Error guardando snapshot de estadísticas (event={event}): {e}", exc_info=True)
    finally:
        if connection and connection.is_connected():
            connection.close()
    return None
