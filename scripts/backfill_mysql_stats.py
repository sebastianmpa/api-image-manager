"""
Migra el histórico de estadísticas de MongoDB a la tabla MySQL image_manager_stats.

Mongo no guarda snapshots de get_global_stats(), así que se reconstruyen a partir de
las fechas de cada documento. Se genera una fila por cada acción que en vivo habría
disparado un snapshot, con los mismos nombres de evento que usa la API:

  - delete_candidate   uno por documento de deleted_candidates (email = deletedBy, brand, mpn)
  - manual_upload      una por subida manual (imágenes del mismo email+brand+mpn que llegaron
                       con menos de MANUAL_UPLOAD_GAP de diferencia, incluidos los not found
                       que esa subida resolvió)
  - process_images /   uno por chunkId (aprobados del bot + imágenes fallidas del lote);
    mark_as_processed  brand/mpn solo si el lote tenía un único producto, igual que en vivo

Cada fila lleva los totales tal como estaban justo después de esa acción. Estado de un
candidato del bot en un instante T:
  - pending   si createdAt <= T y todavía no se había aprobado/eliminado en T
  - processed si processedAt <= T
  - deleted   si deletedAt <= T
Los not found resueltos por subida manual cuentan como notFound hasta su processedAt.

Las filas se marcan con backfilled=1 y created_at en hora local del servidor (la misma
zona que usa MySQL para los snapshots en vivo). Es idempotente: borra las filas
backfilled=1 anteriores antes de insertar.

Uso (desde la raíz del proyecto):
    python scripts/backfill_mysql_stats.py            # inserta
    python scripts/backfill_mysql_stats.py --dry-run  # solo muestra lo que insertaría
"""

import sys
import argparse
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.services import mongo_service as m
from app.services.mysql_stats_service import TABLE_NAME, insert_stats_rows, _get_connection, _ensure_table

RESOLVED_NOT_FOUND_METHOD = "manual_upload_resolved_not_found"
MANUAL_UPLOAD_GAP = timedelta(seconds=30)


def load_docs():
    common = {"_id": 0, "brand": 1, "mpn": 1, "createdAt": 1, "email": 1}
    return {
        "pending": list(m.collection.find({}, common)),
        "processed": list(m.processed_candidates_collection.find({}, {
            **common, "processedAt": 1, "processedMethod": 1, "source": 1, "isDiagram": 1, "isPrimaryManual": 1,
            "processedData.chunkId": 1, "processedData.processedMethod": 1
        })),
        "deleted": list(m.deleted_candidates_collection.find({}, {**common, "deletedAt": 1, "deletedBy": 1})),
        "notFound": list(m.not_found_candidates_collection.find({}, common)),
        "failed": list(m.failed_images_collection.find({}, {"_id": 0, "brand": 1, "mpn": 1, "failedAt": 1, "chunkId": 1})),
    }


def stats_at(docs, t):
    """Replica get_global_stats() tal como habría dado en el instante t (UTC naive)."""
    processed_docs = [d for d in docs["processed"] if d["processedAt"] <= t]
    deleted_docs = [d for d in docs["deleted"] if d["deletedAt"] <= t]
    failed_docs = [d for d in docs["failed"] if d["failedAt"] <= t]

    pending = (
        sum(1 for d in docs["pending"] if d["createdAt"] <= t)
        + sum(1 for d in docs["processed"]
              if d.get("source") != "manual_upload" and d["createdAt"] <= t < d["processedAt"])
        + sum(1 for d in docs["deleted"] if d["createdAt"] <= t < d["deletedAt"])
    )
    not_found = (
        sum(1 for d in docs["notFound"] if d["createdAt"] <= t)
        + sum(1 for d in docs["processed"]
              if d.get("processedMethod") == RESOLVED_NOT_FOUND_METHOD and d["createdAt"] <= t < d["processedAt"])
    )

    processed = len(processed_docs)
    deleted = len(deleted_docs)
    manual = [d for d in processed_docs if d.get("source") == "manual_upload"]
    diagrams = sum(1 for d in processed_docs if d.get("isDiagram") is True)

    sku_counts = Counter((d.get("brand"), d.get("mpn")) for d in processed_docs)
    user_approved = processed - len(manual)

    def manual_count(is_diagram, is_primary):
        return sum(1 for d in manual if d.get("isDiagram") is is_diagram and d.get("isPrimaryManual") is is_primary)

    mip, mis = manual_count(False, True), manual_count(False, False)
    mdp, mds = manual_count(True, True), manual_count(True, False)

    return {
        "pending": pending,
        "processed": processed,
        "deleted": deleted,
        "notFound": not_found,
        "failedImages": len(failed_docs),
        "failedCandidates": len({d["mpn"] for d in failed_docs if "mpn" in d}),
        "total": pending + processed + deleted,
        "manualUploads": len(manual),
        "images": processed - diagrams,
        "diagrams": diagrams,
        "uniqueSkus": len(sku_counts),
        "secondaryImages": sum(max(c - 1, 0) for c in sku_counts.values()),
        "botFoundCandidates": pending + user_approved + deleted,
        "userApprovedCandidates": user_approved,
        "userRejectedCandidates": deleted,
        "autoProcessedImages": user_approved,
        "totalManualImages": mip + mis,
        "manualImagesPrimary": mip,
        "manualImagesSecondary": mis,
        "totalManualDiagrams": mdp + mds,
        "manualDiagramsPrimary": mdp,
        "manualDiagramsSecondary": mds,
    }


def build_events(docs):
    """Lista de eventos {t, event, email, brand, mpn, chunk_id}, con t = UTC naive."""
    events = []

    for d in docs["deleted"]:
        events.append({"t": d["deletedAt"], "event": "delete_candidate",
                       "email": d.get("deletedBy"), "brand": d.get("brand"), "mpn": d.get("mpn")})

    # Subidas manuales: agrupar las imágenes de una misma request
    manual = sorted((d for d in docs["processed"] if d.get("source") == "manual_upload"),
                    key=lambda d: d["processedAt"])
    open_uploads = {}
    for d in manual:
        key = (d.get("email"), d.get("brand"), d.get("mpn"))
        upload = open_uploads.get(key)
        if upload and d["processedAt"] - upload["t"] <= MANUAL_UPLOAD_GAP:
            upload["t"] = d["processedAt"]
        else:
            upload = {"t": d["processedAt"], "event": "manual_upload",
                      "email": key[0], "brand": key[1], "mpn": key[2]}
            open_uploads[key] = upload
            events.append(upload)

    # Lotes del bot: aprobados + imágenes fallidas, agrupados por chunkId
    chunks = defaultdict(lambda: {"times": [], "products": set(), "mark_as_processed": False})
    for i, d in enumerate(d for d in docs["processed"] if d.get("source") != "manual_upload"):
        data = d.get("processedData") or {}
        chunk = chunks[data.get("chunkId") or f"__sin_chunk_{i}"]
        chunk["times"].append(d["processedAt"])
        chunk["products"].add((d.get("brand"), d.get("mpn")))
        chunk["mark_as_processed"] |= data.get("processedMethod") == "mark_as_processed"
    for i, d in enumerate(docs["failed"]):
        chunk = chunks[d.get("chunkId") or f"__sin_chunk_failed_{i}"]
        chunk["times"].append(d["failedAt"])
        chunk["products"].add((d.get("brand"), d.get("mpn")))

    for chunk_id, chunk in chunks.items():
        single = chunk["products"].pop() if len(chunk["products"]) == 1 else (None, None)
        events.append({
            "t": max(chunk["times"]),
            "event": "mark_as_processed" if chunk["mark_as_processed"] else "process_images",
            "chunk_id": None if chunk_id.startswith("__sin_chunk") else chunk_id,
            "brand": single[0], "mpn": single[1],
        })

    return sorted(events, key=lambda e: e["t"])


def utc_naive_to_local(dt_utc):
    """UTC naive (como guarda Mongo) -> hora local del servidor naive (como guarda MySQL)."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No escribe en MySQL")
    args = parser.parse_args()

    docs = load_docs()

    # Validación: reconstruir "ahora" debe dar lo mismo que get_global_stats()
    live = m.get_global_stats()
    rebuilt = stats_at(docs, datetime.now(timezone.utc).replace(tzinfo=None))
    diff = {k: (live[k], rebuilt.get(k)) for k in live if live[k] != rebuilt.get(k)}
    if diff:
        print(f"La reconstrucción no coincide con get_global_stats(): {diff}")
        sys.exit(1)
    print("Validación OK: la reconstrucción al momento actual coincide con get_global_stats()")

    events = build_events(docs)
    rows = [{
        "stats": stats_at(docs, e["t"]),
        "event": e["event"],
        "email": e.get("email"),
        "brand": e.get("brand"),
        "mpn": e.get("mpn"),
        "chunk_id": e.get("chunk_id"),
        "backfilled": True,
        "created_at": utc_naive_to_local(e["t"]),
    } for e in events]

    print(f"{len(rows)} eventos: {dict(Counter(r['event'] for r in rows))}")
    for field in ("email", "brand", "mpn", "chunk_id"):
        print(f"  con {field}: {sum(1 for r in rows if r[field])}")
    for r in rows[:2] + rows[-2:]:
        print(f"  {r['created_at']}  {r['event']:<17} {r['email']} {r['brand']} {r['mpn']} {r['chunk_id']}")

    if args.dry_run:
        print("--dry-run: no se escribió nada")
        return

    connection = _get_connection()
    try:
        cursor = connection.cursor()
        _ensure_table(cursor)
        cursor.execute(f"SHOW COLUMNS FROM `{TABLE_NAME}` LIKE 'backfilled'")
        if not cursor.fetchall():
            cursor.execute(f"ALTER TABLE `{TABLE_NAME}` ADD COLUMN `backfilled` tinyint(1) NOT NULL DEFAULT 0 AFTER `raw_stats`")
        cursor.execute(f"DELETE FROM `{TABLE_NAME}` WHERE backfilled = 1 OR event = 'backfill'")
        print(f"Filas backfill anteriores borradas: {cursor.rowcount}")
        insert_stats_rows(cursor, rows)
        connection.commit()
        print(f"Insertadas {len(rows)} filas en {TABLE_NAME}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
