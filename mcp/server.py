#!/usr/bin/env python3
"""
mediadev-mcp — Model Context Protocol Server v1.2
==================================================
Expone herramientas de observabilidad Y acción de MediaDEV.

Lectura:
  get_system_status      Estado de todos los streams
  get_workers            Procesos ffmpeg + servicios systemd
  get_queue_stats        Motor Destroyer: corridas, detecciones
  get_service_health     Gateways, VPN, DB, proxies
  get_recent_errors      Errores y eventos de transición
  get_service_logs       Logs de cualquier servicio
  get_error_digest       Resumen de errores en todos los servicios
  get_host_resources     CPU, RAM, disco
  get_stream_bandwidth   Bitrate por stream
  get_destroyer_analytics Analítica de corridas del Destroyer
  get_droplets           Inventario DO + detección de huérfanos

Diagnóstico:
  verify_stream_url      Prueba si una URL m3u8 responde correctamente
  get_disk_usage         Uso de disco por stream
  get_uploader_status    Estado del uploader: backlog local y DB

Acción:
  restart_stream         Reinicia un stream vía supervisorctl
  add_stream             Agrega un canal nuevo al sistema completo
  update_stream          Modifica campos de un canal existente
"""
import sys
import logging
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

# Herramientas de lectura existentes
from tools.system   import get_system_status    as _get_system_status
from tools.workers  import get_workers          as _get_workers
from tools.queue    import get_queue_stats      as _get_queue_stats
from tools.health   import get_service_health   as _get_service_health
from tools.errors   import get_recent_errors    as _get_recent_errors
from tools.logs     import get_service_logs     as _get_service_logs, get_error_digest as _get_error_digest
from tools.capacity import get_host_resources   as _get_host_resources, get_stream_bandwidth as _get_stream_bandwidth
from tools.cost     import get_destroyer_analytics as _get_destroyer_analytics, get_droplets as _get_droplets

# Herramientas nuevas
from tools.diagnostics import verify_stream_url  as _verify_stream_url
from tools.diagnostics import get_disk_usage     as _get_disk_usage
from tools.diagnostics import get_uploader_status as _get_uploader_status
from tools.actions     import restart_stream     as _restart_stream
from tools.actions     import add_stream         as _add_stream
from tools.actions     import update_stream      as _update_stream

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [mediadev-mcp] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mediadev-mcp")

mcp = FastMCP(
    name="mediadev",
    instructions=(
        "Servidor MCP de MediaDEV (mediaCAP) — captura 24/7 de 13 estaciones de "
        "Honduras (10 radio + 3 TV) via gateways SOCKS5. "
        "Incluye herramientas de observabilidad, diagnóstico Y acción. "
        "Zona horaria display: America/Tegucigalpa (GMT-6). "
        "Los streams activos están en /opt/media-ai/config/stations.json. "
        "Para cambios de infraestructura, usar las herramientas de acción en lugar de SSH."
    ),
)


# ── LECTURA ───────────────────────────────────────────────────────────────────

@mcp.tool()
def get_system_status() -> dict[str, Any]:
    """
    Estado actual de todos los streams de MediaDEV (radio y TV).

    Por stream: status (OK/STALE/NO_M3U8/DISABLED), segmentos activos,
    antigüedad del último segmento, estado del circuit breaker,
    reinicios del día, última caída/subida.

    OK = age_seconds < 30. STALE = m3u8 existe pero no se actualiza.
    NO_M3U8 = ffmpeg cayó. CB OPEN = 5 fallos, reset en 30 min.
    """
    try:
        return _get_system_status()
    except Exception as e:
        return {"error": str(e), "streams": []}


@mcp.tool()
def get_workers() -> dict[str, Any]:
    """
    Estado de los procesos de MediaDEV.

    - supervisord: procesos ffmpeg por stream (RUNNING/FATAL/BACKOFF)
    - systemd: stream-daemon, nginx, gateway-api, health-engine,
      video-segment-uploader, mediadev-monitor, privoxy, wireguard

    FATAL requiere intervención. BACKOFF está siendo reiniciado.
    """
    try:
        return _get_workers()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_queue_stats(limit: int = 5) -> dict[str, Any]:
    """
    Estado del motor de detección de anuncios (Destroyer).

    Destroyer es un droplet efímero c-16 que corre 4x/día (0h, 6h, 12h, 18h HN)
    procesando MP3 de S3 por fingerprinting acústico.

    Parámetros:
      limit — corridas recientes a mostrar (1-20, default 5)
    """
    try:
        return _get_queue_stats(limit=limit)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_service_health() -> dict[str, Any]:
    """
    Salud del ecosistema: WireGuard VPN, gateways hondureños, PostgreSQL, Privoxy.

    Gateways: hn01/hn02/hn03 con score 0-100, latencia, CPU, estado SOCKS5.
    hn02 (PC-LCE) es el primary. Si active_gateway != hn02, hay failover activo.
    """
    try:
        return _get_service_health()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_recent_errors(stream_id: str = "", hours: int = 6) -> dict[str, Any]:
    """
    Errores y eventos de transición (DOWN/UP/CB_OPEN/CB_CLOSE).

    Parámetros:
      stream_id — filtrar por stream (ej: 'hch_tv'). Vacío = todos.
      hours     — ventana de búsqueda (1-48, default 6)
    """
    try:
        return _get_recent_errors(stream_id=stream_id.strip() or None, hours=hours)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_service_logs(service: str, lines: int = 50, contains: str = "") -> dict[str, Any]:
    """
    Logs recientes de un servicio systemd (incluye tracebacks Python).

    Parámetros:
      service  — publiaudit-api, stream-daemon, mediadev-gateway-api,
                 mediadev-health-engine, video-segment-uploader,
                 mediadev-monitor, nginx, privoxy
      lines    — líneas a devolver (1-500, default 50)
      contains — filtro de texto opcional (ej: 'Error', 'Traceback')
    """
    try:
        return _get_service_logs(service=service, lines=lines, contains=contains)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_error_digest(hours: int = 6) -> dict[str, Any]:
    """
    Resumen de errores en TODOS los servicios en una sola llamada.
    Responde: '¿algo se está rompiendo ahora mismo?'

    Parámetros:
      hours — ventana (1-48, default 6)
    """
    try:
        return _get_error_digest(hours=hours)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_host_resources() -> dict[str, Any]:
    """
    CPU, RAM, disco y procesos top de mediaCAP (2 vCPU / 4 GB).
    Incluye agregado de los ffmpeg de captura y veredicto de headroom.
    """
    try:
        return _get_host_resources()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_stream_bandwidth() -> dict[str, Any]:
    """
    Bitrate estimado (Mbps) por stream de los segmentos .ts recientes.
    Útil para modelar costo de S3 y dimensionar el nodo de captura.
    """
    try:
        return _get_stream_bandwidth()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_destroyer_analytics(limit: int = 12) -> dict[str, Any]:
    """
    Analítica de corridas del Destroyer: boot vs trabajo vs costo, detección de cuelgues.

    Parámetros:
      limit — corridas a analizar (1-50, default 12)
    """
    try:
        return _get_destroyer_analytics(limit=limit)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_droplets() -> dict[str, Any]:
    """
    Inventario de droplets en DigitalOcean + detección de Destroyer huérfanos.
    Un worker activo sin auto-destruirse desperdicia ~$0.95/hora.
    """
    try:
        return _get_droplets()
    except Exception as e:
        return {"error": str(e)}


# ── DIAGNÓSTICO ───────────────────────────────────────────────────────────────

@mcp.tool()
def verify_stream_url(url: str) -> dict[str, Any]:
    """
    Prueba una URL m3u8 y determina automáticamente si necesita gateway o no.

    Prueba primero directo, luego vía el gateway SOCKS5 activo si falla.
    Retorna 'recommended_route' con el valor exacto a poner en stations.json:
      'direct'  — funciona sin gateway
      'gateway' — requiere IP hondureña (geo-restringido)
      'none'    — no funciona por ninguna vía (URL incorrecta o stream offline)

    Parámetros:
      url — URL del stream m3u8 a verificar
    """
    try:
        return _verify_stream_url(url=url)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_disk_usage() -> dict[str, Any]:
    """
    Uso de disco en /var/www/streams/ desglosado por stream.

    Por stream: total, recordings (MP3 acumulados), segmentos .ts activos,
    MP3 pendientes de subir a S3. Ordenado de mayor a menor.

    Útil para detectar acumulación sin limpiar y verificar que el uploader
    está al día.
    """
    try:
        return _get_disk_usage()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_uploader_status() -> dict[str, Any]:
    """
    Estado del video-segment-uploader: backlog local y registros en S3.

    Muestra:
    - Estado del servicio systemd
    - TV: segmentos .ts pendientes de subir (por encima de HLS_KEEP=12)
    - Radio: MP3 de horas completadas aún en disco
    - s3_scan_log: conteo por estado (pending/done/error)
    - Últimas 5 subidas registradas

    Si radio_backlog tiene archivos, el uploader está procesando el backlog
    o hay un problema de conectividad con S3.
    """
    try:
        return _get_uploader_status()
    except Exception as e:
        return {"error": str(e)}


# ── ACCIÓN ────────────────────────────────────────────────────────────────────

@mcp.tool()
def restart_stream(stream_id: str) -> dict[str, Any]:
    """
    Reinicia un stream específico vía supervisorctl.

    Úsalo cuando un stream está STALE o NO_M3U8 y quieres forzar un
    reinicio inmediato sin esperar el ciclo del circuit breaker.

    Parámetros:
      stream_id — ID del stream (ej: 'radio_globo', 'canal_11', 'hch_tv')

    Retorna el nuevo estado del proceso supervisord.
    """
    try:
        return _restart_stream(stream_id=stream_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
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
      2. Bloque en supervisor config
      3. STREAMS[] y TV_STREAMS en stream_daemon.py (si es TV)
      4. supervisorctl reread + update (activa el proceso)
      5. Reinicio del stream-daemon

    Parámetros:
      stream_id   — ID único sin espacios ni mayúsculas (ej: 'canal_5')
      name        — Nombre legible (ej: 'Canal 5 El Líder')
      stream_type — 'radio' o 'tv'
      url         — URL del stream m3u8
      route       — 'auto', 'gateway' o 'direct' (default: 'auto')
      gateway     — ID del gateway (default: 'hn02')
      referer     — Header Referer si el servidor lo requiere
      enabled     — True para activar inmediatamente (default: True)

    Tip: usar verify_stream_url primero para confirmar que la URL funciona.
    """
    try:
        return _add_stream(
            stream_id=stream_id, name=name, stream_type=stream_type,
            url=url, route=route, gateway=gateway, referer=referer, enabled=enabled,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def update_stream(stream_id: str, fields: dict) -> dict[str, Any]:
    """
    Modifica campos de un stream existente en stations.json.

    Campos permitidos: url, route, gateway, referer, enabled, name.
    Si cambia url, route, referer o enabled, reinicia el stream automáticamente.

    Parámetros:
      stream_id — ID del stream a modificar (ej: 'teleceiba')
      fields    — Dict con los campos a cambiar
                  Ej: {"url": "http://...", "route": "gateway"}
                  Ej: {"enabled": false}   para pausar un stream
    """
    try:
        return _update_stream(stream_id=stream_id, fields=fields)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.warning("mediadev-mcp v1.2 iniciando (stdio transport)")
    mcp.run(transport="stdio")
