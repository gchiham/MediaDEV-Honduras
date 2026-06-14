"""
tools/logs.py — Logs de servicios para diagnóstico de errores.

get_service_logs()  — tail (y grep opcional) del journal de un servicio systemd.
get_error_digest()  — escaneo de tracebacks/errores en los servicios clave.

El MCP corre como root vía el wrapper SSH, así que puede leer el journal.
"""
import subprocess
import re
from typing import Any, Optional

# Allowlist — no se acepta cualquier string (evita journalctl con args arbitrarios)
ALLOWED_SERVICES = {
    "media-app",
    "stream-daemon",
    "mediadev-gateway-api",
    "mediadev-health-engine",
    "video-segment-uploader",
    "mediadev-monitor",
    "medio-orchestrator",
    "nginx",
    "privoxy",
}

# Patrones que indican un problema real (se buscan en el journal de cada servicio)
ERROR_PATTERNS = re.compile(
    r"Traceback|Exception|CRITICAL|\bERROR\b|Internal Server Error|"
    r"psycopg2\.|OperationalError|500 Internal|Connection refused|Fatal",
    re.IGNORECASE,
)


def _journal(service: str, lines: int = 50, since: Optional[str] = None) -> list[str]:
    cmd = ["journalctl", "-u", service, "--no-pager", "--output", "short-iso"]
    if since:
        cmd += ["--since", since]
    else:
        cmd += ["-n", str(lines)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.stdout.splitlines()


def get_service_logs(service: str, lines: int = 50, contains: str = "") -> dict[str, Any]:
    """
    Tail (y grep opcional) del journal de un servicio systemd.
    Devuelve las últimas N líneas, incluyendo tracebacks de Python.
    """
    service = (service or "").strip()
    if service not in ALLOWED_SERVICES:
        return {
            "error": f"Servicio no permitido: '{service}'",
            "allowed_services": sorted(ALLOWED_SERVICES),
        }
    lines = min(max(1, lines), 500)
    try:
        out = _journal(service, lines=lines)
    except subprocess.TimeoutExpired:
        return {"error": "journalctl timeout", "service": service}
    except Exception as e:
        return {"error": str(e), "service": service}

    if contains:
        c = contains.lower()
        out = [ln for ln in out if c in ln.lower()]

    return {
        "service": service,
        "lines_returned": len(out),
        "filter": contains or None,
        "log": out[-lines:],
    }


def get_error_digest(hours: int = 6) -> dict[str, Any]:
    """
    Escanea el journal de los servicios clave buscando errores/tracebacks
    en las últimas N horas. Una sola llamada para '¿algo se está rompiendo?'.
    """
    hours = min(max(1, hours), 48)
    since = f"{hours} hours ago"
    digest = []
    for svc in sorted(ALLOWED_SERVICES):
        try:
            out = _journal(svc, since=since)
        except Exception as e:
            digest.append({"service": svc, "error": str(e)})
            continue
        matches = [ln for ln in out if ERROR_PATTERNS.search(ln)]
        if matches:
            digest.append({
                "service": svc,
                "error_lines": len(matches),
                "last": matches[-1],
                "sample": matches[-5:],
            })
    return {
        "period_hours": hours,
        "services_with_errors": len(digest),
        "digest": digest if digest else "Sin errores en los servicios clave en la ventana.",
    }
