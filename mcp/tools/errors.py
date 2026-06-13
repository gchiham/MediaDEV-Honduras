"""
tools/errors.py — get_recent_errors()
Últimos errores y eventos de transición del sistema.
"""
from datetime import datetime, timezone, timedelta
from typing import Any

from db import qall, qone

HN_TZ = timezone(timedelta(hours=-6))


def _fmt_ts(dt) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "astimezone"):
        return dt.astimezone(HN_TZ).isoformat()
    return str(dt)


def get_recent_errors(stream_id: str | None = None, hours: int = 6) -> dict[str, Any]:
    """
    Últimos errores y eventos de transición de MediaDEV.
    
    Parámetros:
      stream_id: filtrar por stream específico (ej. 'hch_tv'). Opcional.
      hours: cuántas horas hacia atrás buscar (default 6, max 48).
    """
    hours = min(max(1, hours), 48)

    # Eventos de streams (DOWN, UP, CB_OPEN, CB_CLOSE)
    filters = ["ts >= EXTRACT(EPOCH FROM NOW() - INTERVAL '%s hours')::bigint"]
    params: list = [hours]
    if stream_id:
        filters.append("stream_id = %s")
        params.append(stream_id)

    where = " AND ".join(filters)
    events = qall(f"""
        SELECT
            stream_id,
            etype,
            detail,
            ts
        FROM mediadev_events
        WHERE {where}
        ORDER BY ts DESC
        LIMIT 100
    """, tuple(params))

    formatted_events = []
    for e in events:
        formatted_events.append({
            "stream": e["stream_id"],
            "type":   e["etype"],
            "detail": e.get("detail"),
            "time_hn": _fmt_ts(
                datetime.fromtimestamp(e["ts"], tz=timezone.utc) if e.get("ts") else None
            ),
        })

    # Resumen por tipo de evento
    type_counts: dict[str, int] = {}
    for ev in formatted_events:
        t = ev["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    # Corridas de Destroyer con errores
    destroyer_errors = qall("""
        SELECT id, status, files_error, total_files, files_done, t2_started
        FROM destroyer_runs
        WHERE files_error > 0
          AND t2_started >= NOW() - INTERVAL '48 hours'
        ORDER BY t2_started DESC
        LIMIT 10
    """)

    # Failovers de gateway en la ventana
    failovers = qall("""
        SELECT stream_id, from_gateway_id, to_gateway_id, reason,
               trigger_score, auto_triggered, created_at, resolved_at
        FROM failover_events
        WHERE created_at >= NOW() - INTERVAL '%s hours'
        ORDER BY created_at DESC
        LIMIT 50
    """, (hours,))

    return {
        "period_hours":     hours,
        "stream_filter":    stream_id,
        "events":           formatted_events,
        "event_counts":     type_counts,
        "total_events":     len(formatted_events),
        "destroyer_errors": [
            {
                "run_id":      r["id"],
                "status":      r["status"],
                "files_error": r["files_error"],
                "files_done":  r["files_done"],
                "total_files": r["total_files"],
                "started_hn":  _fmt_ts(r["t2_started"]),
            }
            for r in destroyer_errors
        ],
        "gateway_failovers": [
            {
                "stream":        f["stream_id"],
                "from_gateway":  f["from_gateway_id"],
                "to_gateway":    f["to_gateway_id"],
                "reason":        f.get("reason"),
                "trigger_score": f.get("trigger_score"),
                "auto":          f.get("auto_triggered"),
                "time_hn":       _fmt_ts(f["created_at"]),
                "resolved_hn":   _fmt_ts(f.get("resolved_at")),
            }
            for f in failovers
        ],
    }
