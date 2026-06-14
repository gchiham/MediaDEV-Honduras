"""
tools/actions.py — Herramientas de acción (escritura) para el MCP de MediaDEV.

restart_stream   — Reinicia un stream por supervisorctl
add_stream       — Agrega un canal nuevo (stations.json + supervisor + daemon)
update_stream    — Modifica campos de un canal existente
"""
import json
import re
import subprocess
from pathlib import Path
from typing import Any

STATIONS_JSON  = Path("/opt/media-ai/config/stations.json")
SUPERVISOR_CONF = Path("/etc/supervisor/conf.d/mediadev_streams.conf")
DAEMON_PY      = Path("/opt/media-ai/daemon/stream_daemon.py")

ALLOWED_UPDATE_FIELDS = {"url", "route", "gateway", "referer", "enabled", "name"}


# ── helpers ───────────────────────────────────────────────────────────────────

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


def _daemon_add_stream(stream_id: str, stream_type: str) -> dict:
    """Agrega stream_id a STREAMS[] y TV_STREAMS en stream_daemon.py."""
    content = DAEMON_PY.read_bytes()

    # Agregar a STREAMS[]
    m = re.search(rb'STREAMS\s*=\s*\[([^\]]+)\]', content)
    if not m:
        return {"ok": False, "error": "No se encontró STREAMS[] en stream_daemon.py"}

    ids_raw = m.group(1).decode()
    existing = [s.strip().strip('"').strip("'") for s in ids_raw.split(",") if s.strip()]
    if stream_id not in existing:
        existing.append(stream_id)
        new_list = ", ".join(f'"{s}"' for s in existing)
        content = content[:m.start(1)] + new_list.encode() + content[m.end(1):]

    # Agregar a TV_STREAMS si aplica
    if stream_type == "tv":
        m2 = re.search(rb'TV_STREAMS\s*=\s*\{([^\}]*)\}', content)
        if m2:
            tv_raw = m2.group(1).decode()
            tv_ids = [s.strip().strip('"').strip("'") for s in tv_raw.split(",") if s.strip()]
            if stream_id not in tv_ids:
                tv_ids.append(stream_id)
                new_set = ", ".join(f'"{s}"' for s in tv_ids)
                content = content[:m2.start(1)] + new_set.encode() + content[m2.end(1):]

    DAEMON_PY.write_bytes(content)
    return {"ok": True}


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
      url         — URL del stream m3u8
      route       — 'auto', 'gateway' o 'direct'
      gateway     — ID del gateway (ej: 'hn02')
      referer     — Header Referer si el servidor lo requiere (puede quedar vacío)
      enabled     — True para activar inmediatamente
    """
    if stream_type not in ("radio", "tv"):
        return {"ok": False, "error": "stream_type debe ser 'radio' o 'tv'"}

    if not re.match(r'^[a-z0-9_]+$', stream_id):
        return {"ok": False, "error": "stream_id solo puede tener letras minúsculas, números y _"}

    # 1. stations.json
    cfg = _load_stations()
    if any(s["id"] == stream_id for s in cfg["stations"]):
        return {"ok": False, "error": f"Ya existe un stream con id '{stream_id}'"}

    entry: dict = {
        "id": stream_id, "name": name, "type": stream_type,
        "route": route, "gateway": gateway, "url": url, "enabled": enabled,
    }
    if referer:
        entry["referer"] = referer
    cfg["stations"].append(entry)
    _save_stations(cfg)

    # 2. Supervisor config
    block = _supervisor_block(stream_id)
    with SUPERVISOR_CONF.open("a") as f:
        f.write(block)

    # 3. stream_daemon.py
    daemon_result = _daemon_add_stream(stream_id, stream_type)
    if not daemon_result["ok"]:
        return {"ok": False, "step": "daemon_py", "error": daemon_result["error"]}

    # 4. Activar en supervisor
    _run(["supervisorctl", "reread"])
    rc, out, err = _run(["supervisorctl", "update"])

    # 5. Reiniciar daemon para que reconozca el stream en su lista interna
    _run(["systemctl", "restart", "stream-daemon"])

    # Estado final del proceso
    _, status_out, _ = _run(["supervisorctl", "status", f"stream_{stream_id}"])

    return {
        "ok": True,
        "stream_id": stream_id,
        "name": name,
        "type": stream_type,
        "url": url,
        "supervisor_status": status_out,
        "supervisor_update": out,
        "note": "stream-daemon reiniciado para reconocer el nuevo canal",
    }


def update_stream(stream_id: str, fields: dict) -> dict[str, Any]:
    """
    Modifica campos de un stream existente en stations.json.
    Campos permitidos: url, route, gateway, referer, enabled, name.

    Si se cambia url o route, reinicia el stream automáticamente.

    Parámetros:
      stream_id — ID del stream a modificar
      fields    — Dict con los campos a cambiar, ej: {"url": "...", "route": "gateway"}
    """
    invalid = set(fields) - ALLOWED_UPDATE_FIELDS
    if invalid:
        return {"ok": False, "error": f"Campos no permitidos: {invalid}. Permitidos: {ALLOWED_UPDATE_FIELDS}"}

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

    # Reiniciar si cambió url, route o enabled
    restart_result = None
    needs_restart = bool(changed.keys() & {"url", "route", "enabled", "referer"})
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
