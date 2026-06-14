# MediaDEV Stream Monitor — Contexto raíz

## Propósito del proyecto
Sistema de monitoreo, grabación y auditoría 24/7 de 12 estaciones de Honduras
(10 radios audio + 2 canales de TV con video). Captura streams vía gateways residenciales
hondureños (geo-restriction), los sirve como HLS, archiva audio (MP3) y video (S3), expone
dashboards + API REST, y alimenta el motor de detección de anuncios (Destroyer).

## Arquitectura — 2 nodos (split 14 jun 2026)
Este repo es el código de **mediaCAP** (nodo de captura). El producto (`media-app`) y la
orquestación del Destroyer viven en **mediaAPP** (nodo aparte, misma VPC nyc1).
```
[Streams HN] → [Gateways SOCKS5] ──WireGuard──► mediaCAP (159.223.104.91 · 2vCPU/4GB)
                                                  │  supervisord (12 ffmpeg) · stream-daemon
                                                  │  video-uploader · gateway-api · health-engine
                                                  │  wireguard · privoxy · monitor · MCP
                                                  └──────────► PostgreSQL media-db (DO Managed)
                                                                      ▲
   mediaAPP (137.184.53.234 · 2vCPU/2GB) ─────────────────────────────┘ (DB privada, misma VPC)
     media-app + nginx (producto SaaS + evidence portal)
     Destroyer launcher + watchdog (cron) · MCP
```

## Hardware (por nodo)
- **mediaCAP: 2 vCPU / 4 GB** (captura) — el diseño prioriza este constraint: no glob masivo en
  disco, no reducir intervalos del daemon. mediaCAP debe quedar lo más liviano posible para grabar.
- **mediaAPP: 2 vCPU / 2 GB** (app/control).

## Componentes principales
| Componente | Ruta | Descripción |
|---|---|---|
| Stream daemon | `daemon/stream_daemon.py` | Health, grabación MP3, espejo de estado a PG |
| Dashboard + API | `dashboard/dashboard_v4.py` | Vistas web + endpoints JSON (lee de PG) |
| Scripts de stream | `scripts/stream_*.sh` | Un script ffmpeg por stream (proxy SOCKS5 o directo) |
| Video uploader | `scripts/video_segment_uploader.py` | Sube .ts de TV a S3 |
| Gateways | `/opt/destroyer/gateway/` | API de heartbeats + health engine (failover) |
| Monitor | `monitor/monitor.py` | Vigila WireGuard, alertas Telegram |
| Config | `config/stations.json` | Estaciones activas + definición de gateways |

## Base de datos — PostgreSQL (media-db), única persistencia
Ya NO se usa SQLite local. El daemon mantiene el estado en memoria y lo espeja a PG.
```sql
mediadev_stream_status  -- estado actual por stream (1 fila c/u)
mediadev_metrics        -- muestra cada 60s: status, segs, bytes (retención 7 días)
mediadev_events         -- transiciones DOWN/UP/CB_OPEN/CB_CLOSE (retención 30 días)
-- compartidas con Destroyer: stream_catalog, advertisements, fingerprint_detections, gateways...
```
Credenciales: `/etc/mediadev-db.env` (cargado por systemd). `monitor/events.db` es una SQLite
aparte que SÍ usa el monitor — no confundir.

## API / Dashboard
- **Dashboard viejo (`dashboard_v4.py`) ELIMINADO** el 14 jun 2026 (van a hacer uno nuevo). El
  código sigue en `dashboard/` como referencia; sus endpoints `/api/*` read-only ya no corren.
- **`media-app`** (producto SaaS + evidence portal) corre en **mediaAPP** (`137.184.53.234`),
  NO en este repo — es código aparte, aún sin versionar.

## Servicios systemd
**mediaCAP (captura):**
```bash
systemctl status stream-daemon mediadev-gateway-api mediadev-health-engine \
                 mediadev-monitor video-segment-uploader nginx privoxy wg-quick@wg0
supervisorctl status   # 12 procesos ffmpeg
```
**mediaAPP (app/control):** `media-app`, `nginx`, + cron del Destroyer (launcher + watchdog).

## Red y gateways
- **WireGuard wg0**: MediaDEV `10.101.0.1/24`. Gateways en `config/stations.json`.
- Fuente de verdad del gateway activo: `/etc/mediadev/gateway.conf` (cambiar SOLO con
  `gateway_switch.sh <id>`). Los scripts hacen `source` de ese archivo.
- Streams geo-restringidos usan SOCKS5; los de CDN global (streamtheworld, etc.) van directos.
- Failover automático lo decide `health_engine.py` por health score.

## Zona horaria
**UTC en backend, GMT-6 solo en display** (cutover: 13 jun 2026 16:07 UTC).
Timestamps en PG como `TIMESTAMPTZ` en UTC. `pipeline_version='legacy'` = pre-cutover, `'utc_v2'` = post.
Honduras sin DST — offset fijo `-6h` para presentación.

## Principios arquitectónicos
1. Un solo daemon de mantenimiento (evita condiciones de carrera).
2. Estado operativo en memoria + filesystem (mtime); PG es espejo tolerante a fallos.
3. Circuit Breaker (5 fallos → OPEN, reset 30 min) evita restart storms.
4. Sin glob masivo en health check — solo lee el m3u8.
5. Batch queries en el dashboard (GROUP BY), nunca loops por stream.
6. Segmentos persistentes (8h) para auditoría y para el uploader de video.

## Instrucciones para AI
- Leer el CLAUDE.md más cercano a los archivos del task antes de explorar.
- Inspeccionar solo lo relacionado con la tarea; evitar búsquedas globales salvo necesidad.
- Preferir ediciones quirúrgicas; preservar la arquitectura (no cambiar infra sin pedido).
- Para cambios en streams: verificar si usan SOCKS5 o conexión directa.
- No reducir intervalos del daemon sin justificación (2 vCPU).
- Credenciales siempre en `/etc/*.env` fuera del repo, nunca hardcodeadas.
