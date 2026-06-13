"""
tools/system.py — get_system_status()
Estado actual de los 12 streams de MediaDEV.
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from db import qall, qone

HN_TZ = timezone(timedelta(hours=-6))


def _ts_to_hn(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=HN_TZ).isoformat()


def get_system_status() -> dict[str, Any]:
    """
    Estado actual de los 12 streams de MediaDEV.
    Retorna salud, segmentos activos, circuit breaker y uptime por stream.
    """
    rows = qall("""
        SELECT
            stream_id,
            status,
            sup,
            segs,
            age,
            cb_state,
            cb_fails,
            restart_today,
            last_down,
            last_up,
            updated_at
        FROM mediadev_stream_status
        ORDER BY stream_id
    """)

    streams = []
    ok_count = 0
    for r in rows:
        status = r.get("status") or "UNKNOWN"
        if status == "OK":
            ok_count += 1
        streams.append({
            "id":             r["stream_id"],
            "status":         status,
            "supervisord":    r.get("sup") or "unknown",
            "segments":       r.get("segs") or 0,
            "age_seconds":    r.get("age") or 0,
            "cb_state":       r.get("cb_state") or "CLOSED",
            "cb_fails":       r.get("cb_fails") or 0,
            "restarts_today": r.get("restart_today") or 0,
            "last_down":      _ts_to_hn(r.get("last_down")),
            "last_up":        _ts_to_hn(r.get("last_up")),
        })

    summary = qone("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'OK')       AS ok,
            COUNT(*) FILTER (WHERE status = 'STALE')    AS stale,
            COUNT(*) FILTER (WHERE status = 'NO_M3U8')  AS no_m3u8,
            COUNT(*) FILTER (WHERE status = 'DISABLED') AS disabled,
            COUNT(*) FILTER (WHERE cb_state = 'OPEN')   AS cb_open
        FROM mediadev_stream_status
    """) or {}

    return {
        "streams_ok":      int(summary.get("ok") or 0),
        "streams_stale":   int(summary.get("stale") or 0),
        "streams_no_m3u8": int(summary.get("no_m3u8") or 0),
        "streams_disabled":int(summary.get("disabled") or 0),
        "circuit_breakers_open": int(summary.get("cb_open") or 0),
        "streams":         streams,
        "total":           len(streams),
        "updated":         datetime.now(tz=HN_TZ).isoformat(),
    }
