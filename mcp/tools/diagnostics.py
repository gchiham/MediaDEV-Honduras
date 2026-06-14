"""
tools/diagnostics.py — Herramientas de diagnóstico para el MCP de MediaDEV.

verify_stream_url  — Prueba si una URL de stream responde correctamente
get_disk_usage     — Uso de disco por stream (local)
get_uploader_status — Estado del uploader: pendientes en disco y en DB
"""
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from db import qall, qone

STREAMS_ROOT   = Path("/var/www/streams")
GATEWAY_CONF   = Path("/etc/mediadev/gateway.conf")
STATIONS_JSON  = Path("/opt/media-ai/config/stations.json")


# ── helpers ───────────────────────────────────────────────────────────────────

def _active_socks5() -> str | None:
    """Lee GW_SOCKS5 desde gateway.conf para pruebas vía gateway."""
    try:
        m = re.search(r"GW_SOCKS5=socks5://([^\s]+)", GATEWAY_CONF.read_text())
        if m:
            return m.group(1)  # host:port
    except Exception:
        pass
    return None


def _du_bytes(path: Path) -> int:
    try:
        r = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, timeout=10)
        return int(r.stdout.split()[0]) if r.returncode == 0 else 0
    except Exception:
        return 0


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── tools ─────────────────────────────────────────────────────────────────────

def _curl_probe(url: str, socks5: str | None = None) -> dict[str, Any]:
    """Hace un curl al URL y retorna el resultado parseado."""
    cmd = ["curl", "-si", "--max-time", "10", "-A", "MediaDEV/1.0", "-L"]
    if socks5:
        cmd += ["--socks5-hostname", socks5]
    cmd.append(url)

    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout (15s)", "http_code": 0}
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    output = r.stdout
    headers, _, body = output.partition("\r\n\r\n")
    if not body:
        headers, _, body = output.partition("\n\n")

    http_code = 0
    m = re.search(r"HTTP/[\d.]+ (\d+)", headers)
    if m:
        http_code = int(m.group(1))

    ct_m = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.IGNORECASE)
    content_type = ct_m.group(1).strip() if ct_m else None

    is_hls = body.strip().startswith("#EXTM3U")
    segments = re.findall(r"[^\s]+\.ts", body)

    ok = http_code in (200, 206) and is_hls
    result: dict = {
        "ok": ok,
        "http_code": http_code,
        "content_type": content_type,
        "response_ms": elapsed_ms,
        "is_hls": is_hls,
    }
    if segments:
        result["first_segment"] = segments[0]
    if not ok:
        result["body_preview"] = body[:200].strip()
    return result


def verify_stream_url(url: str) -> dict[str, Any]:
    """
    Prueba una URL m3u8 y determina automáticamente si necesita gateway o no.

    Lógica igual a route=auto de stream_run.sh:
      1. Prueba conexión directa desde mediaCAP
      2. Si falla, prueba vía el gateway SOCKS5 activo
      3. Reporta qué funciona y qué route recomendar

    Parámetros:
      url — URL del stream a probar (.m3u8)

    El campo 'recommended_route' indica qué poner en stations.json:
      'direct'  — funciona sin gateway
      'gateway' — requiere IP hondureña
      'none'    — no funciona por ninguna vía (URL incorrecta o stream offline)
    """
    # Intento 1: directo
    direct = _curl_probe(url)
    if direct["ok"]:
        return {
            "url": url,
            "recommended_route": "direct",
            "direct": direct,
            "gateway": None,
            "summary": "OK directo — no necesita gateway",
        }

    # Intento 2: vía gateway
    socks5 = _active_socks5()
    if not socks5:
        return {
            "url": url,
            "recommended_route": "none",
            "direct": direct,
            "gateway": None,
            "summary": "Falla directo y no hay gateway configurado",
        }

    gw = _curl_probe(url, socks5=socks5)
    if gw["ok"]:
        return {
            "url": url,
            "recommended_route": "gateway",
            "direct": direct,
            "gateway": {**gw, "socks5": socks5},
            "summary": "Falla directo pero OK vía gateway — stream geo-restringido",
        }

    # Ninguno funciona
    return {
        "url": url,
        "recommended_route": "none",
        "direct": direct,
        "gateway": {**gw, "socks5": socks5},
        "summary": "No responde ni directo ni por gateway — URL incorrecta o stream offline",
    }


def get_disk_usage() -> dict[str, Any]:
    """
    Uso de disco en /var/www/streams/ desglosado por stream.
    Muestra: total local, recordings (MP3), segmentos .ts y conteo de archivos.
    Útil para detectar acumulación de archivos sin limpiar.
    """
    if not STREAMS_ROOT.exists():
        return {"ok": False, "error": f"{STREAMS_ROOT} no existe"}

    streams = []
    total_bytes = 0

    for stream_dir in sorted(STREAMS_ROOT.iterdir()):
        if not stream_dir.is_dir():
            continue

        dir_bytes   = _du_bytes(stream_dir)
        rec_dir     = stream_dir / "recordings"
        rec_bytes   = _du_bytes(rec_dir) if rec_dir.exists() else 0
        ts_files    = list(stream_dir.glob("seg_*.ts"))
        ts_bytes    = sum(f.stat().st_size for f in ts_files if f.exists())
        mp3_pending = list(rec_dir.glob("*.mp3")) if rec_dir.exists() else []

        total_bytes += dir_bytes
        streams.append({
            "id":           stream_dir.name,
            "total":        _human(dir_bytes),
            "total_bytes":  dir_bytes,
            "recordings":   _human(rec_bytes),
            "ts_segments":  len(ts_files),
            "ts_size":      _human(ts_bytes),
            "mp3_pending":  len(mp3_pending),
        })

    streams.sort(key=lambda x: x["total_bytes"], reverse=True)

    return {
        "ok": True,
        "total": _human(total_bytes),
        "total_bytes": total_bytes,
        "streams": streams,
        "root": str(STREAMS_ROOT),
    }


def get_uploader_status() -> dict[str, Any]:
    """
    Estado del video-segment-uploader:
    - Archivos locales pendientes de subir (MP3 radio + .ts TV)
    - Registros en s3_scan_log por estado (pending / done / error)
    - Estado del servicio systemd
    - Últimas 5 subidas registradas en la DB

    Útil para responder: ¿el uploader está al día? ¿hay backlog?
    """
    # Estado del servicio
    r = subprocess.run(
        ["systemctl", "is-active", "video-segment-uploader"],
        capture_output=True, text=True
    )
    service_active = r.stdout.strip()

    # Pendientes locales por stream
    import json
    stations = json.loads(STATIONS_JSON.read_text())["stations"]
    tv_ids    = [s["id"] for s in stations if s.get("type") == "tv"  and s.get("enabled", True)]
    radio_ids = [s["id"] for s in stations if s.get("type") == "radio" and s.get("enabled", True)]

    tv_pending = []
    for sid in tv_ids:
        ts_dir = STREAMS_ROOT / sid
        count  = len(list(ts_dir.glob("seg_*.ts"))) if ts_dir.exists() else 0
        if count > 12:  # HLS_KEEP
            tv_pending.append({"stream": sid, "segments_pending": count - 12})

    radio_pending = []
    for sid in radio_ids:
        rec_dir = STREAMS_ROOT / sid / "recordings"
        mp3s    = list(rec_dir.glob("*.mp3")) if rec_dir.exists() else []
        if mp3s:
            radio_pending.append({"stream": sid, "mp3_pending": len(mp3s)})

    # DB: conteo por status en s3_scan_log
    db_summary = qall("""
        SELECT status, COUNT(*) AS count
        FROM s3_scan_log
        GROUP BY status
        ORDER BY status
    """)

    # Últimas 5 subidas
    recent = qall("""
        SELECT s3_key, stream, status, updated_at
        FROM s3_scan_log
        ORDER BY updated_at DESC
        LIMIT 5
    """)

    return {
        "ok": True,
        "service": service_active,
        "tv_backlog":    tv_pending,
        "radio_backlog": radio_pending,
        "s3_scan_log": {row["status"]: int(row["count"]) for row in db_summary},
        "recent_uploads": [
            {
                "key":        r["s3_key"],
                "stream":     r["stream"],
                "status":     r["status"],
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in recent
        ],
    }
