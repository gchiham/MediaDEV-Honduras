"""
tools/workers.py — get_workers()
Estado de los 12 procesos ffmpeg (supervisord) y servicios systemd clave.
"""
import subprocess
import re
from typing import Any


SYSTEMD_SERVICES = [
    "stream-daemon",
    "nginx",
    "mediadev-gateway-api",
    "mediadev-health-engine",
    "mediadev-monitor",
    "video-segment-uploader",
    "dashboard-mediadev",
    "publiaudit-api",
    "medio-orchestrator",
    "privoxy",
    "supervisor",
    "wg-quick@wg0",
]


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


def _supervisord_status() -> list[dict]:
    """Parsea la salida de 'supervisorctl status'."""
    output = _run(["supervisorctl", "status"])
    processes = []
    for line in output.splitlines():
        # Formato: stream_xy_hrn   RUNNING   pid 12345, uptime 2:03:14
        parts = re.split(r"\s+", line.strip(), maxsplit=2)
        if len(parts) < 2:
            continue
        name  = parts[0]
        state = parts[1]
        detail = parts[2] if len(parts) > 2 else ""
        pid_match    = re.search(r"pid (\d+)", detail)
        uptime_match = re.search(r"uptime (.+)$", detail)
        processes.append({
            "name":   name,
            "state":  state,
            "pid":    int(pid_match.group(1)) if pid_match else None,
            "uptime": uptime_match.group(1).strip() if uptime_match else None,
        })
    return processes


def _systemd_status(services: list[str]) -> dict[str, str]:
    """Verifica is-active de cada servicio systemd."""
    result = {}
    for svc in services:
        out = _run(["systemctl", "is-active", svc], timeout=5)
        result[svc] = out or "unknown"
    return result


def get_workers() -> dict[str, Any]:
    """
    Estado de los procesos de MediaDEV:
    - 12 ffmpeg vía supervisord (streams de radio/TV)
    - Servicios systemd críticos del stack
    """
    sup_procs = _supervisord_status()
    sys_svc   = _systemd_status(SYSTEMD_SERVICES)

    # Resumen rápido
    running = sum(1 for p in sup_procs if p["state"] == "RUNNING")
    fatal   = sum(1 for p in sup_procs if p["state"] in ("FATAL", "BACKOFF"))

    inactive_svc = [k for k, v in sys_svc.items() if v != "active"]

    return {
        "supervisord": {
            "running": running,
            "fatal":   fatal,
            "total":   len(sup_procs),
            "processes": sup_procs,
        },
        "systemd": {
            "all_active": len(inactive_svc) == 0,
            "inactive":   inactive_svc,
            "services":   sys_svc,
        },
    }
