"""
tools/queue.py — get_queue_stats()
Estado del motor Destroyer: corridas, archivos procesados, detecciones, costos.
"""
from datetime import datetime, timezone, timedelta
from typing import Any

from db import qall, qone

HN_TZ = timezone(timedelta(hours=-6))

CRON_SCHEDULE = "0 0,6,12,18 * * * (4 veces/día)"


def _fmt_ts(dt) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "astimezone"):
        return dt.astimezone(HN_TZ).isoformat()
    return str(dt)


def get_queue_stats(limit: int = 5) -> dict[str, Any]:
    """
    Estado del motor de detección de anuncios (Destroyer):
    - Últimas N corridas con estado, archivos, detecciones y costo
    - Conteos de detecciones de hoy y totales
    - Próxima ventana de ejecución según cron
    """
    limit = min(max(1, limit), 20)  # Clampear entre 1 y 20

    # Últimas corridas
    runs = qall("""
        SELECT
            id,
            status,
            t2_started,
            t3_completed,
            t4_destroyed,
            files_done,
            total_files,
            files_error,
            total_detections,
            cost_usd,
            work_seconds,
            droplet_name
        FROM destroyer_runs
        ORDER BY id DESC
        LIMIT %s
    """, (limit,))

    last_run = None
    if runs:
        r = runs[0]
        last_run = {
            "id":               r["id"],
            "status":           r["status"],
            "started_hn":       _fmt_ts(r["t2_started"]),
            "completed_hn":     _fmt_ts(r["t3_completed"]),
            "files_done":       r["files_done"],
            "total_files":      r["total_files"],
            "files_error":      r.get("files_error") or 0,
            "total_detections": r["total_detections"],
            "cost_usd":         float(r["cost_usd"]) if r["cost_usd"] else None,
            "work_seconds":     r["work_seconds"],
            "droplet":          r.get("droplet_name"),
        }

    # Detecciones de hoy (en hora HN)
    today_stats = qone("""
        SELECT
            COUNT(*) FILTER (WHERE air_time >= NOW() - INTERVAL '24 hours') AS today,
            COUNT(*) FILTER (WHERE deleted_at IS NULL) AS total
        FROM fingerprint_detections
    """) or {}

    # Anuncios registrados
    ad_count = qone("SELECT COUNT(*) AS n FROM advertisements")
    ads_n = int(ad_count["n"]) if ad_count else 0

    return {
        "last_run":          last_run,
        "recent_runs":       [
            {
                "id": r["id"],
                "status": r["status"],
                "started_hn": _fmt_ts(r["t2_started"]),
                "files_done": r["files_done"],
                "detections": r["total_detections"],
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] else None,
            }
            for r in runs
        ],
        "detections_last_24h": int(today_stats.get("today") or 0),
        "detections_total":    int(today_stats.get("total") or 0),
        "advertisements_registered": ads_n,
        "cron_schedule":       CRON_SCHEDULE,
    }
