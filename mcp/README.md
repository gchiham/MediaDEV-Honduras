# mediadev-mcp — MCP Server (mediaCAP)

> Verificado contra `/opt/media-ai/mcp/` desplegado (9-oct-2026).

Servidor Model Context Protocol del nodo **mediaCAP**. `FastMCP` con transport `stdio`: no es
un servicio y no queda corriendo. El cliente lo lanza por SSH en cada sesión (`start.sh` →
`venv/bin/python server.py`).

> mediaAPP tiene su propio MCP, versionado en `mediadev-infra/mediaapp/mcp/`.

## Herramientas (17)

**Estado real:** las tools se escribieron en junio, cuando supervisor manejaba los streams.
Varias siguen leyendo supervisor o `stations.json`, y hoy dan datos vacíos o incorrectos.

### Lectura
| Tool | Fuente | Estado |
|---|---|---|
| `get_system_status()` | `mediadev_stream_status` (PG) | ✅ correcta |
| `get_workers()` | `supervisorctl status` + systemd | ⚠️ la parte de ffmpeg sale vacía (supervisor ya no tiene programas). Para procesos: `pgrep -af ffmpeg` |
| `get_queue_stats(limit)` | tablas del Destroyer (PG) | ✅ |
| `get_service_health()` | gateways (DB, fallback `stations.json`), WireGuard, Privoxy | ✅ |
| `get_recent_errors(stream_id, hours)` | `mediadev_events` + failovers + runs | ✅ |
| `get_host_resources()` | `/proc`, ffmpeg | ✅ |
| `get_stream_bandwidth()` | disco por stream | ✅ |
| `get_destroyer_analytics(limit)` | corridas del Destroyer (PG) | ✅ (con AWS suspendida no hay corridas nuevas) |
| `get_droplets()` | API de DigitalOcean | ✅ |

### Diagnóstico
| Tool | Estado |
|---|---|
| `get_service_logs(service, lines, contains)` | ✅ journal con allowlist |
| `get_error_digest(hours)` | ✅ |
| `verify_stream_url(url)` | ⚠️ sirve para probar la URL, pero `recommended_route` usa la lógica `auto` de `stream_run.sh`. Los valores reales en `capture_config` son `socks5` o `direct` |
| `get_disk_usage()` | ✅ |
| `get_uploader_status()` | ✅ (la lista TV/radio la saca de `stations.json`, igual que el uploader) |

### Acción — ❌ NO USAR
| Tool | Por qué está rota |
|---|---|
| `restart_stream(stream_id)` | Hace `supervisorctl restart stream_<id>`, que ya no existe. Hoy: matar el ffmpeg del stream (el daemon lo respawnea en ≤ 15 s) o `systemctl restart stream-daemon` (reinicia todos) |
| `add_stream(...)` | Escribe `stations.json` y un bloque de supervisor con `stream_run.sh`. El daemon no lee ninguno de los dos. Hoy: `media_sources` + `capture_config` (ver `scripts/CLAUDE.md`) |
| `update_stream(stream_id, fields)` | Edita `stations.json`, que el daemon ignora. Hoy: `UPDATE capture_config` |

Pendiente: reescribir estas tres sobre `capture_config` o retirarlas.

## Uso desde Claude Code (Windows)
Wrapper local que hace SSH al nodo y pasa stdin/stdout del protocolo MCP:
`C:\Users\Sedesol\.ssh\mediadev-mcp.py` (mediaCAP) · `mediadev-app-mcp.py` (mediaAPP).

```json
{
  "mcpServers": {
    "mediadev":     { "command": "python.exe", "args": ["C:\\Users\\Sedesol\\.ssh\\mediadev-mcp.py"] },
    "mediadev-app": { "command": "python.exe", "args": ["C:\\Users\\Sedesol\\.ssh\\mediadev-app-mcp.py"] }
  }
}
```
Flags del wrapper que evitan corromper el protocolo: `-T`, `-o LogLevel=QUIET`, `stderr=DEVNULL`.

## Estructura
```
mcp/
├── server.py        FastMCP, registra las 17 tools
├── db.py            conexión PG (lee /etc/mediadev-db.env)
├── tools/           system, workers, queue, health, errors, logs, capacity, cost, diagnostics, actions
├── start.sh         entrypoint que lanza el cliente por SSH
├── install.sh       crea el venv
└── requirements.txt
```

## Seguridad
- Las lecturas de PG son solo `SELECT`. Las credenciales salen de `/etc/mediadev-db.env`.
- Las tools corren como root en el nodo. La seguridad es la llave SSH (`keySED`).
