# MediaDEV Stream Monitor — Contexto raíz

## Propósito del proyecto
Sistema de monitoreo, grabación y auditoría 24/7 de ~20 estaciones de Honduras
(radios audio + canales de TV con video; la lista real vive en `capture_config`, ver abajo). Captura streams vía gateways residenciales
hondureños (geo-restriction), los sirve como HLS, archiva el audio horario (.ts crudo; el Destroyer
lo pasa a MP3) y el video de TV en el bucket, y alimenta el motor de detección de anuncios (Destroyer).

## Arquitectura — 2 nodos (split 14 jun 2026)
Este repo es el código de **mediaCAP** (nodo de captura). El producto (`media-app`) y la
orquestación del Destroyer viven en **mediaAPP** (nodo aparte, misma VPC nyc1).
```
[Streams HN] → [Gateways SOCKS5] ──WireGuard──► mediaCAP (159.223.104.91 · 2vCPU/4GB)
                                                  │  stream-daemon (lanza 1 ffmpeg por estación)
                                                  │  video-uploader · gateway-api · health-engine
                                                  │  wireguard · privoxy · monitor · MCP
                                                  └──────────► PostgreSQL media-db (DO Managed)
                                                                      ▲
   mediaAPP (137.184.53.234 · 2vCPU/2GB) ─────────────────────────────┘ (DB privada, misma VPC)
     media-app + nginx (producto SaaS + evidence portal)
     Destroyer launcher + watchdog (orquestación AWS: EventBridge+Lambda+EC2 Spot) · chihambot · MCP
```

## Hardware (por nodo)
- **mediaCAP: 2 vCPU / 4 GB** (captura) — el diseño prioriza este constraint: no glob masivo en
  disco, no reducir intervalos del daemon. mediaCAP debe quedar lo más liviano posible para grabar.
- **mediaAPP: 2 vCPU / 2 GB** (app/control).

## Componentes principales
| Componente | Ruta | Descripción |
|---|---|---|
| Stream daemon | `daemon/stream_daemon.py` | Lanza ffmpeg, health/CB, hora de audio de radios, espejo a PG |
| Dashboard + API | `archive/dashboard/` | ELIMINADO de prod (14 jun); solo referencia |
| Captura ffmpeg | `daemon/stream_daemon.py` (`spawn_stream`) | El daemon lanza ffmpeg directo (Popen). `scripts/stream_*.sh`/`stream_run.sh` ya NO se usan |
| Video uploader | `scripts/video_segment_uploader.py` | Sube .ts de TV + audio horario TV. **Lista TV desde `stations.json`** |
| Gateways | `/opt/destroyer/gateway/` (repo `destroyer`, `cap/`) | API de heartbeats + health engine (failover) |
| Monitor | `monitor/monitor.py` | Vigila WireGuard, alertas Telegram |
| Config de captura | tabla `capture_config` JOIN `media_sources` (PG) | **Única fuente de verdad** de URLs, route y banderas por estación |
| `config/stations.json` | (fuera de git) | Lo regenera `sync_streams.py`. Lo usan el uploader (lista TV), el bootstrap del daemon y el fallback de gateways del MCP. Sus URLs **no** se usan para capturar |

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
- **Dashboard viejo (`dashboard_v4.py`) ELIMINADO** el 14 jun 2026. El código sigue en
  `archive/dashboard/` como referencia; sus endpoints `/api/*` ya no corren.
- **`media-app`** (producto SaaS + evidence portal) corre en **mediaAPP** (`137.184.53.234`),
  NO en este repo. Lo desplegado sigue a `carlosrl19/publiaudit_Back`; `gchiham/media-app` quedó
  atrás (ver su README).

## Servicios systemd
**mediaCAP (captura):**
```bash
systemctl status stream-daemon mediadev-gateway-api mediadev-health-engine \
                 mediadev-monitor video-segment-uploader nginx privoxy wg-quick@wg0 \
                 mediadev-ffmpeg-reaper.timer mediadev-logs mediadev-metrics
pgrep -af ffmpeg       # normal = 1 por estación activa (supervisor sigue activo pero sin programas)
```
**mediaAPP (app/control):** `media-app`, `chihambot` (bot Telegram), `nginx`, `mediadev-logs/metrics`, MCP,
y crons de `/opt/destroyer` (deadman del Destroyer y de captura, `clip_refiner`, `onboard_lempira`). La
orquestación del Destroyer ya NO usa cron local — corre en **AWS** (EventBridge horario →
Lambda → EC2 Spot); el `launcher.py`/`watchdog.py` viven en `/opt/destroyer`.

## Versionado (GitHub)
Todo el código y la config operativa está espejado en GitHub (la verdad es lo desplegado):

| Repo | Contenido | Vis. |
|---|---|---|
| `gchiham/MediaDEV-Honduras` | **este repo** — mediaCAP `/opt/media-ai` (captura) | público |
| `gchiham/media-app` | mediaAPP `/opt/media-app` (producto SaaS). **Atrasado**: prod sigue a `carlosrl19/publiaudit_Back` | privado |
| `gchiham/destroyer` | `/opt/destroyer` ambos nodos (`app/`=mediaAPP, `cap/`=mediaCAP) | privado |
| `gchiham/mediadev-infra` | config operativa (systemd, supervisor, nginx, wireguard) + `INVENTORY.md` | privado |

Secretos (`/etc/*.env`, llaves WireGuard, `destroyer-worker.pem`) NUNCA en git — ver
`mediadev-infra/INVENTORY.md`.

## Red y gateways
- **WireGuard wg0**: MediaDEV `10.101.0.1/24`. Gateways: hn01 `10.101.0.2`, hn02 `10.101.0.5`, hn03 `10.101.0.6` (activo hoy). Su estado vive en la DB.
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
3. Circuit Breaker (8 fallos → OPEN, reset fijo a los 10 min, en memoria del daemon) evita restart storms.
4. Sin glob masivo en health check — solo lee el m3u8.
5. Batch queries (GROUP BY), nunca loops por stream.
6. Segmentos locales: el daemon guarda 8 h (radio) / 24 h (TV), pero un cron de root borra a las 2 h
   los de radio (y de TV fuera de hch_tv/teleceiba/canal_11). El uploader de TV borra cada segmento al subirlo.

## Configuración de captura (fuente de verdad)
- El `stream_daemon` lee `capture_config JOIN media_sources` (`load_config_from_db()`) y lo
  relee cada 300 s (hot-add/remove sin restart). `is_enabled=false` apaga la estación.
- `config/stations.json` y `stream_catalog.stream_url` están congelados y **mienten** sobre URLs.
- Una bandera nueva (ej. `hls_live_restart`, `ffmpeg_extra`) debe existir como columna **y** en
  ese `SELECT`; si no, el daemon cae al default en silencio.
- El repo quedó byte a byte igual a `/opt/media-ai` el 9-oct-2026, pero el deploy es manual por SSH:
  antes de proponer cambios, comparar con lo desplegado (`git status` en el server).
- Para diagnosticar captura (no graba, mudo, duplicados, load, banderas ffmpeg) usar el skill
  `mediacap-ffmpeg` (`.claude/skills/mediacap-ffmpeg/`).

## Instrucciones para AI
- Leer el CLAUDE.md más cercano a los archivos del task antes de explorar.
- Inspeccionar solo lo relacionado con la tarea; evitar búsquedas globales salvo necesidad.
- Preferir ediciones quirúrgicas; preservar la arquitectura (no cambiar infra sin pedido).
- Para cambios en streams: verificar `route` (socks5/direct) en `capture_config`, no en `stations.json`.
- No reducir intervalos del daemon sin justificación (2 vCPU).
- Credenciales siempre en `/etc/*.env` fuera del repo, nunca hardcodeadas.
