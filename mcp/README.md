# mediadev-mcp — MCP Server v1.0

Servidor Model Context Protocol para MediaDEV.
Expone observabilidad del sistema a Claude Code, Codex, OpenAI Agents y cualquier
cliente compatible con MCP.

## Fase A (v1) — Solo Lectura

| Tool | Descripción |
|---|---|
| `get_system_status()` | Estado de los 12 streams (OK/STALE/NO_M3U8/CB) |
| `get_workers()` | Procesos ffmpeg (supervisord) + servicios systemd |
| `get_queue_stats(limit)` | Motor Destroyer: corridas, detecciones, costos |
| `get_service_health()` | Gateways, WireGuard, DB, Privoxy |
| `get_recent_errors(stream_id, hours)` | Eventos DOWN/UP/CB_OPEN, errores Destroyer |

## Uso con Claude Code

Agregar en `~/.claude/claude_desktop_config.json` (o `%APPDATA%\Claude\claude_desktop_config.json` en Windows):

```json
{
  "mcpServers": {
    "mediadev": {
      "command": "ssh",
      "args": [
        "-i", "C:/Users/Sedesol/.ssh/keySED",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "root@159.223.104.91",
        "/opt/media-ai/mcp/venv/bin/python",
        "/opt/media-ai/mcp/server.py"
      ]
    }
  }
}
```

## Uso con Codex / OpenAI Agents (SSE)

Próxima fase. Por ahora usar stdio via SSH.

## Estructura

```
/opt/media-ai/mcp/
├── server.py          ← Punto de entrada MCP (stdio)
├── db.py              ← Conexión PostgreSQL read-only
├── tools/
│   ├── system.py      ← get_system_status
│   ├── workers.py     ← get_workers
│   ├── queue.py       ← get_queue_stats
│   ├── health.py      ← get_service_health
│   └── errors.py      ← get_recent_errors
├── requirements.txt
└── install.sh
```

## Dependencias

```
mcp[cli]>=1.3.0
psycopg2-binary>=2.9.9
python-dotenv>=1.0.0
httpx>=0.27.0
```

## Instalación

```bash
cd /opt/media-ai/mcp
bash install.sh
```

## Seguridad

- **Solo lectura**: `default_transaction_read_only=on` en la conexión PG.
- **Credenciales desde** `/etc/mediadev-db.env` (chmod 600), nunca hardcodeadas.
- **IPs de gateways enmascaradas** en el output.
- **Transport stdio**: acceso vía SSH — la seguridad es la clave SSH.
- **Sin endpoints HTTP** en v1: no hay superficie de ataque adicional.

## Roadmap

- **Fase B**: `query_database_readonly`, `search_detections`, `get_detection_detail`
- **Fase C**: `search_logs`, `get_stream_log`
- **Fase D**: integración GitHub (commits, issues)
- **Fase E**: `run_safe_command` (whitelist estricta)
