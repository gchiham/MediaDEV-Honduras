#!/usr/bin/env python3
"""
mediadev-mcp — Model Context Protocol Server v1.0
==================================================
Expone herramientas de observabilidad de MediaDEV a clientes MCP:
Claude Code, Codex, OpenAI Agents, y cualquier cliente compatible.

Fase A (v1) — Solo lectura:
  - get_system_status    Estado de los 12 streams
  - get_workers          Procesos ffmpeg + servicios systemd
  - get_queue_stats      Motor Destroyer: corridas, detecciones, costos
  - get_service_health   Gateways, VPN, DB, proxies
  - get_recent_errors    Errores y eventos de transición

Transport: stdio (Claude Code vía SSH) — el más simple y seguro.

Uso con Claude Code:
  Agregar en ~/.claude/claude_desktop_config.json:
  {
    "mcpServers": {
      "mediadev": {
        "command": "ssh",
        "args": ["-i", "~/.ssh/keySED", "-o", "StrictHostKeyChecking=no",
                 "root@159.223.104.91",
                 "/opt/media-ai/mcp/venv/bin/python",
                 "/opt/media-ai/mcp/server.py"]
      }
    }
  }
"""
import sys
import os
import json
import logging
from pathlib import Path
from typing import Any

# Añadir el directorio del MCP al path para importar db.py y tools/
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

# Importar herramientas
from tools.system import get_system_status as _get_system_status
from tools.workers import get_workers as _get_workers
from tools.queue import get_queue_stats as _get_queue_stats
from tools.health import get_service_health as _get_service_health
from tools.errors import get_recent_errors as _get_recent_errors

# Logging a stderr (stdio transport usa stdout para el protocolo)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [mediadev-mcp] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mediadev-mcp")

# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="mediadev",
    instructions=(
        "Servidor MCP de MediaDEV — sistema de monitoreo 24/7 de 12 estaciones de "
        "radio y TV de Honduras. Proporciona observabilidad de streams, gateways, "
        "motor de detección de anuncios (Destroyer) y salud del ecosistema. "
        "Todas las herramientas son de SOLO LECTURA. "
        "Zona horaria: America/Tegucigalpa (GMT-6)."
    ),
)


# ── TOOL 1: get_system_status ─────────────────────────────────────────────────

@mcp.tool()
def get_system_status() -> dict[str, Any]:
    """
    Estado actual de los 12 streams de radio y TV de MediaDEV.
    
    Retorna por stream: status (OK/STALE/NO_M3U8/DISABLED), segmentos activos,
    antigüedad del último segmento en segundos, estado del circuit breaker,
    reinicios del día, y última caída/subida.
    
    Un stream OK tiene age_seconds < 30. STALE indica que el m3u8 existe pero
    no se está actualizando. NO_M3U8 indica que ffmpeg cayó.
    Circuit breaker OPEN = 5 fallos consecutivos, se resetea en 30 minutos.
    """
    try:
        return _get_system_status()
    except Exception as e:
        log.error(f"get_system_status error: {e}")
        return {"error": str(e), "streams": []}


# ── TOOL 2: get_workers ───────────────────────────────────────────────────────

@mcp.tool()
def get_workers() -> dict[str, Any]:
    """
    Estado de los procesos de MediaDEV.
    
    Incluye:
    - supervisord: los 12 procesos ffmpeg (uno por stream). Estado RUNNING/FATAL/BACKOFF.
    - systemd: servicios críticos (stream-daemon, nginx, gateway-api, health-engine,
      publiaudit-api, video-segment-uploader, medio-orchestrator, privoxy, wireguard).
    
    Un stream en FATAL necesita intervención. BACKOFF está siendo reiniciado por supervisord.
    Servicios inactive/failed en systemd requieren atención inmediata.
    """
    try:
        return _get_workers()
    except Exception as e:
        log.error(f"get_workers error: {e}")
        return {"error": str(e)}


# ── TOOL 3: get_queue_stats ───────────────────────────────────────────────────

@mcp.tool()
def get_queue_stats(limit: int = 5) -> dict[str, Any]:
    """
    Estado del motor de detección de anuncios (Destroyer).
    
    Destroyer es un droplet efímero (c-16) que corre 4 veces al día (0h, 6h, 12h, 18h HN)
    y procesa los MP3 horarios de S3 buscando anuncios por fingerprinting acústico.
    
    Parámetros:
      limit: cuántas corridas recientes mostrar (1-20, default 5)
    
    Retorna: última corrida, N corridas recientes, conteo de detecciones 24h/total,
    cantidad de anuncios registrados y horario cron.
    """
    try:
        return _get_queue_stats(limit=limit)
    except Exception as e:
        log.error(f"get_queue_stats error: {e}")
        return {"error": str(e)}


# ── TOOL 4: get_service_health ────────────────────────────────────────────────

@mcp.tool()
def get_service_health() -> dict[str, Any]:
    """
    Salud del ecosistema de infraestructura de MediaDEV.
    
    Incluye:
    - wireguard: estado del túnel VPN con Honduras (peers, handshake reciente)
    - gateways: 3 nodos hondureños (hn01/hn02/hn03) con score 0-100, latencia,
      CPU, estado SOCKS5, último heartbeat. La IP pública está enmascarada.
    - database: conectividad y latencia a PostgreSQL (media-db DO Managed)
    - privoxy: bridge HTTP→SOCKS5 local
    - active_gateway: el gateway que está sirviendo el tráfico ahora mismo
    
    Gateway hn02 (PC-LCE) es el primary. hn01 y hn03 son failover.
    Si active_gateway != hn02, hay un failover activo.
    """
    try:
        return _get_service_health()
    except Exception as e:
        log.error(f"get_service_health error: {e}")
        return {"error": str(e)}


# ── TOOL 5: get_recent_errors ─────────────────────────────────────────────────

@mcp.tool()
def get_recent_errors(stream_id: str = "", hours: int = 6) -> dict[str, Any]:
    """
    Últimos errores y eventos de transición de MediaDEV.
    
    Parámetros:
      stream_id: filtrar por stream específico (ej: 'hch_tv', 'xy_hrn'). 
                 Vacío = todos los streams.
      hours: ventana de búsqueda en horas (1-48, default 6)
    
    Tipos de evento:
      DOWN      → stream dejó de transmitir
      UP        → stream recuperado
      CB_OPEN   → circuit breaker abierto (5 fallos consecutivos)
      CB_CLOSE  → circuit breaker cerrado (reset automático 30 min)
    
    También retorna corridas de Destroyer con archivos en error (últimas 48h).
    
    IDs de stream válidos: xy_hrn, xy_tgu, xy_sps, radio_satelite, fm_941,
    suave_fm, radio_america, radio_globo, radio_el_patio, hch_tv, teleceiba,
    radio_choluteca.
    """
    try:
        sid = stream_id.strip() if stream_id else None
        return _get_recent_errors(stream_id=sid, hours=hours)
    except Exception as e:
        log.error(f"get_recent_errors error: {e}")
        return {"error": str(e)}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.warning("mediadev-mcp v1.0 iniciando (stdio transport)")
    mcp.run(transport="stdio")
