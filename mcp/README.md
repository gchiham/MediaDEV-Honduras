# mediadev-mcp — MCP Server v1.2

Servidor Model Context Protocol para MediaDEV.
Expone observabilidad, diagnóstico y acción del sistema a Claude Code y cualquier cliente MCP compatible.

## Herramientas disponibles (16 tools)

### Lectura
| Tool | Descripción |
|---|---|
| get_system_status() | Estado de todos los streams (OK/STALE/NO_M3U8/CB) |
| get_workers() | Procesos ffmpeg (supervisord) + servicios systemd |
| get_queue_stats(limit) | Motor Destroyer: corridas, detecciones, costos |
| get_service_health() | Gateways, WireGuard, DB, Privoxy |
| get_recent_errors(stream_id, hours) | Eventos DOWN/UP/CB_OPEN/CB_CLOSE |
| get_service_logs(service, lines, contains) | Logs de servicios systemd |
| get_error_digest(hours) | Resumen de errores en todos los servicios |
| get_host_resources() | CPU, RAM, disco de mediaCAP |
| get_stream_bandwidth() | Bitrate estimado por stream |
| get_destroyer_analytics(limit) | Analítica boot/trabajo/costo del Destroyer |
| get_droplets() | Inventario DigitalOcean + detección de huerfanos |

### Diagnóstico
| Tool | Descripción |
|---|---|
| verify_stream_url(url) | Prueba URL m3u8 — auto-detecta si necesita gateway |
| get_disk_usage() | Uso de disco por stream en /var/www/streams/ |
| get_uploader_status() | Backlog TV/radio local + registros s3_scan_log |

### Acción
| Tool | Descripción |
|---|---|
| restart_stream(stream_id) | Reinicia un stream via supervisorctl |
| add_stream(...) | Agrega canal nuevo (stations.json + supervisor + daemon) |
| update_stream(stream_id, fields) | Modifica campos de un canal existente |

## Uso con Claude Code

Agregar en :



## Estructura



## Seguridad

- Lectura PG:  en la conexion DB.
- Herramientas de accion: ejecutan supervisorctl/systemctl como root via SSH — usar con criterio.
- Credenciales desde  (chmod 600), nunca hardcodeadas.
- Transport stdio: acceso via SSH — la seguridad es la clave SSH.
