"""
tools/actions.py — Herramientas de acción (escritura) para el MCP de MediaDEV.

restart_stream   — Reinicia un stream por supervisorctl
add_stream       — Agrega un canal nuevo (stations.json + supervisor + daemon + stream_catalog)
update_stream    — Modifica campos de un canal existente (+ sincroniza stream_catalog)
"""
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import dotenv_values

STATIONS_JSON   = Path("/opt/media-ai/config/stations.json")
SUPERVISOR_CONF = Path("/etc/supervisor/conf.d/mediadev_streams.conf")

ALLOWED_UPDATE_FIELDS = {"url", "urls", "route", "gateway", "referer", "enabled", "name"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _pg_write(sql: str, params: tuple = ()) -> None:
    """Ejecuta una escritura en PostgreSQL. Nunca lanza — loguea el error."""
    try:
        cfg = dotenv_values("/etc/mediadev-db.env")
        conn = psycopg2.connect(
            host=cfg.get("PG_HOST"), port=int(cfg.get("PG_PORT", "25060")),
            dbname=cfg.get("PG_DB"), user=cfg.get("PG_USER"), password=cfg.get("PG_PASS"),
            connect_timeout=5, sslmode="require",
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.close()
    except Exception as e:
        print(f"[pg_write] error: {e}")


def _catalog_upsert(stream_id: str, name: str, stream_type: str) -> None:
    """Inserta o actualiza la entrada en stream_catalog con el tipo correcto."""
    _pg_write(
        """INSERT INTO stream_catalog (id, name, type, country_code, timezone, s3_prefix, status)
           VALUES (%s, %s, %s, 'HN', 'America/Tegucigalpa', %s, 'active')
           ON CONFLICT (id) DO UPDATE SET
             name   = EXCLUDED.name,
             type   = EXCLUDED.type,
             status = EXCLUDED.status""",
        (stream_id, name, stream_type, stream_id),
    )


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _load_stations() -> dict:
    return json.loads(STATIONS_JSON.read_text())


def _save_stations(cfg: dict) -> None:
    STATIONS_JSON.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def _supervisor_block(stream_id: str) -> str:
    return (
        f"\n[program:stream_{stream_id}]\n"
        f"command=/opt/media-ai/scripts/stream_run.sh {stream_id}\n"
        f"autostart=true\nautorestart=true\nstartsecs=5\nstartretries=999\nstopwaitsecs=10\n"
        f"stdout_logfile=/var/log/streams/{stream_id}.log\n"
        f"stdout_logfile_maxbytes=5MB\nstdout_logfile_backups=2\n"
        f"stderr_logfile=/var/log/streams/{stream_id}.err\n"
        f"stderr_logfile_maxbytes=5MB\nstderr_logfile_backups=2\n"
        f"environment=HOME=\"/root\"\n"
    )



# ── tools ─────────────────────────────────────────────────────────────────────

def restart_stream(stream_id: str) -> dict[str, Any]:
    """
    Reinicia un stream específico vía supervisorctl.

    Parámetros:
      stream_id — ID del stream (ej: 'radio_globo', 'canal_11')

    Retorna el nuevo estado del proceso.
    """
    # Validar que existe en stations.json
    cfg = _load_stations()
    station = next((s for s in cfg["stations"] if s["id"] == stream_id), None)
    if station is None:
        return {"ok": False, "error": f"Stream '{stream_id}' no encontrado en stations.json"}

    prog = f"stream_{stream_id}"
    rc, out, err = _run(["supervisorctl", "restart", prog])
    if rc != 0:
        return {"ok": False, "stream_id": stream_id, "error": err or out}

    # Leer estado nuevo
    _, status_out, _ = _run(["supervisorctl", "status", prog])
    return {
        "ok": True,
        "stream_id": stream_id,
        "result": status_out,
    }


def add_stream(
    stream_id: str,
    name: str,
    stream_type: str,
    url: str,
    urls: list[str] | None = None,
    route: str = "auto",
    gateway: str = "hn02",
    referer: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """
    Agrega un canal nuevo al sistema completo:
      1. stations.json
      2. Supervisor config (nuevo bloque [program:stream_X])
      3. stream_daemon.py STREAMS[] y TV_STREAMS si es TV
      4. supervisorctl reread + update (activa el proceso)
      5. Reinicio del stream-daemon para que reconozca el nuevo canal

    Parámetros:
      stream_id   — ID único sin espacios (ej: 'canal_5')
      name        — Nombre legible (ej: 'Canal 5 El Líder')
      stream_type — 'radio' o 'tv'
      url         — URL primaria del stream
      urls        — Lista de hasta 3 URLs con fallback automático; si se provee,
                    sustituye a 'url' y el script rotará entre ellas ante fallos
      route       — 'auto', 'gateway' o 'direct'
      gateway     — ID del gateway (ej: 'hn02')
      referer     — Header Referer si el servidor lo requiere (puede quedar vacío)
      enabled     — True para activar inmediatamente
    """
    if stream_type not in ("radio", "tv"):
        return {"ok": False, "error": "stream_type debe ser 'radio' o 'tv'"}

    if not re.match(r'^[a-z0-9_]+$', stream_id):
        return {"ok": False, "error": "stream_id solo puede tener letras minúsculas, números y _"}

    if urls is not None:
        if not isinstance(urls, list) or not (1 <= len(urls) <= 3):
            return {"ok": False, "error": "'urls' debe ser una lista de 1 a 3 URLs"}
        if not all(isinstance(u, str) and u.strip() for u in urls):
            return {"ok": False, "error": "Cada URL en 'urls' debe ser un string no vacío"}

    # 1. stations.json
    cfg = _load_stations()
    if any(s["id"] == stream_id for s in cfg["stations"]):
        return {"ok": False, "error": f"Ya existe un stream con id '{stream_id}'"}

    entry: dict = {
        "id": stream_id, "name": name, "type": stream_type,
        "route": route, "gateway": gateway, "enabled": enabled,
    }
    if urls:
        entry["urls"] = urls   # multi-URL con fallback automático
    else:
        entry["url"] = url     # URL única (formato legacy)
    if referer:
        entry["referer"] = referer
    cfg["stations"].append(entry)
    _save_stations(cfg)

    # 2. Supervisor config
    block = _supervisor_block(stream_id)
    with SUPERVISOR_CONF.open("a") as f:
        f.write(block)

    # 3. Registrar en stream_catalog (el dashboard lee el tipo de aquí)
    _catalog_upsert(stream_id, name, stream_type)

    # 4. Activar en supervisor (el daemon lee stations.json dinámicamente, no hace falta reiniciarlo)
    _run(["supervisorctl", "reread"])
    rc, out, err = _run(["supervisorctl", "update"])

    # Estado final del proceso
    _, status_out, _ = _run(["supervisorctl", "status", f"stream_{stream_id}"])

    return {
        "ok": True,
        "stream_id": stream_id,
        "name": name,
        "type": stream_type,
        "url": urls[0] if urls else url,
        "supervisor_status": status_out,
        "supervisor_update": out,
        "note": "stream-daemon lee stations.json dinámicamente; stream_catalog actualizado",
    }


def update_stream(stream_id: str, fields: dict) -> dict[str, Any]:
    """
    Modifica campos de un stream existente en stations.json.
    Campos permitidos: url, urls, route, gateway, referer, enabled, name.

    Si se cambia url, urls o route, reinicia el stream automáticamente.
    Para multi-URL con fallback, usar 'urls': ["url1", "url2", "url3"] (máx 3).

    Parámetros:
      stream_id — ID del stream a modificar
      fields    — Dict con los campos a cambiar, ej: {"urls": ["https://...", "https://..."]}
    """
    invalid = set(fields) - ALLOWED_UPDATE_FIELDS
    if invalid:
        return {"ok": False, "error": f"Campos no permitidos: {invalid}. Permitidos: {ALLOWED_UPDATE_FIELDS}"}

    if "urls" in fields:
        u = fields["urls"]
        if not isinstance(u, list) or not (1 <= len(u) <= 3):
            return {"ok": False, "error": "'urls' debe ser una lista de 1 a 3 URLs"}
        if not all(isinstance(x, str) and x.strip() for x in u):
            return {"ok": False, "error": "Cada URL en 'urls' debe ser un string no vacío"}

    cfg = _load_stations()
    station = next((s for s in cfg["stations"] if s["id"] == stream_id), None)
    if station is None:
        return {"ok": False, "error": f"Stream '{stream_id}' no encontrado"}

    changed = {}
    for k, v in fields.items():
        if station.get(k) != v:
            station[k] = v
            changed[k] = v

    if not changed:
        return {"ok": True, "stream_id": stream_id, "changed": {}, "note": "Sin cambios"}

    _save_stations(cfg)

    # Sincronizar stream_catalog si cambió nombre o tipo
    if changed.keys() & {"name", "type"}:
        _catalog_upsert(
            stream_id,
            station.get("name", stream_id),
            station.get("type", "radio"),
        )

    # Reiniciar si cambió url, route o enabled
    restart_result = None
    needs_restart = bool(changed.keys() & {"url", "urls", "route", "enabled", "referer"})
    if needs_restart and station.get("enabled", True):
        rc, out, _ = _run(["supervisorctl", "restart", f"stream_{stream_id}"])
        restart_result = out

    return {
        "ok": True,
        "stream_id": stream_id,
        "changed": changed,
        "restarted": needs_restart,
        "supervisor_result": restart_result,
    }
