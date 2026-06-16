# mediadev-mcp — MCP Server

Servidor Model Context Protocol para MediaDEV (nodo **mediaCAP**, captura). Expone
observabilidad, diagnóstico y acción del sistema a Claude Code y cualquier cliente MCP
compatible. Corre como `FastMCP` con transport `stdio`; el acceso desde Windows es vía SSH.

> El nodo **mediaAPP** tiene su propio MCP (modular bajo `tools/`: system, workers, queue,
> health, errors, logs, cost, capacity) — versionado en el repo `mediadev-infra`. Este README
> es el del MCP de mediaCAP (repo `MediaDEV-Honduras`, `mcp/server.py`).

## Herramientas (17 tools)

### Observabilidad (lectura)
| Tool | Descripción |
|---|---|
| `get_system_status()` | Estado de los 13 streams (OK/STALE/NO_M3U8/CB) |
| `get_workers()` | Procesos ffmpeg (supervisord) + servicios systemd |
| `get_queue_stats(limit)` | Motor Destroyer: corridas, detecciones, costos (DB) |
| `get_service_health()` | Gateways, WireGuard, DB, Privoxy |
| `get_recent_errors(stream_id, hours)` | Eventos DOWN/UP/CB_OPEN/CB_CLOSE + failovers + runs con error |
| `get_host_resources()` | CPU, RAM, disco, load de mediaCAP + agregado ffmpeg |
| `get_stream_bandwidth()` | Bitrate (Mbps) y GB/día por stream — el cuello al escalar TV es red/disco/S3 |
| `get_destroyer_analytics(limit)` | Boot/work/costo por corrida + detección automática de cuelgues |
| `get_droplets()` | Inventario de droplets DigitalOcean (los 2 nodos; el Destroyer ya no usa droplets — corre en AWS) |

### Diagnóstico
| Tool | Descripción |
|---|---|
| `get_service_logs(service, lines, contains)` | Tail/grep del journal de un servicio (allowlist) |
| `get_error_digest(hours)` | Escaneo consolidado de errores/tracebacks — "¿qué se rompe?" |
| `verify_stream_url(url)` | Prueba una URL m3u8 — auto-detecta si necesita gateway |
| `get_disk_usage()` | Uso de disco por stream en `/var/www/streams/` |
| `get_uploader_status()` | Backlog TV/radio local + registros en `s3_scan_log` |

### Acción (escriben/ejecutan como root — usar con criterio)
| Tool | Descripción |
|---|---|
| `restart_stream(stream_id)` | Reinicia un stream vía `supervisorctl` |
| `add_stream(...)` | Agrega canal nuevo (`stations.json` + supervisor + daemon) |
| `update_stream(stream_id, fields)` | Modifica campos de un canal existente |

## Uso con Claude Code (desde Windows)

Wrapper local que hace SSH al nodo y proxea stdin/stdout del protocolo MCP:
`C:\Users\Sedesol\.ssh\mediadev-mcp.py` (mediaCAP) · `mediadev-app-mcp.py` (mediaAPP).

```json
// claude_desktop_config.json
{
  "mcpServers": {
    "mediadev":     { "command": "python.exe", "args": ["C:\\Users\\Sedesol\\.ssh\\mediadev-mcp.py"] },
    "mediadev-app": { "command": "python.exe", "args": ["C:\\Users\\Sedesol\\.ssh\\mediadev-app-mcp.py"] }
  }
}
```

Flags críticos del wrapper para no corromper el protocolo: `-T` (sin pseudo-tty),
`-o LogLevel=QUIET` (sin banners SSH), `stderr=DEVNULL` (SSH stderr no contamina JSON-RPC).

## Estructura

```
/opt/media-ai/mcp/
├── server.py    ← FastMCP, transport="stdio", 17 tools
└── venv/        ← Python venv con mcp[server]
```

## Seguridad

- Lectura de PG: solo `SELECT` en la conexión a la DB.
- Herramientas de acción: ejecutan `supervisorctl`/edición de config como root — usar con criterio.
- Credenciales desde `/etc/mediadev-db.env` (chmod 600), nunca hardcodeadas.
- Transport `stdio` sobre SSH: la seguridad es la llave SSH (`keySED`).
