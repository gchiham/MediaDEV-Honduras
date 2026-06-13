# MediaDEV — Auditoría del Sistema y MCP Server

**Fecha:** 2026-06-13  
**Servidor:** `159.223.104.91` (DigitalOcean, 2 vCPU / 4 GB)  
**Branch:** `feature/mcp-server` · commit `0962233`  
**Autor del análisis:** Claude Sonnet 4.6 (Claude Code)

---

## Tabla de Contenidos

1. [Arquitectura General](#1-arquitectura-general)
2. [Inventario de Componentes](#2-inventario-de-componentes)
3. [Base de Datos](#3-base-de-datos)
4. [APIs Existentes](#4-apis-existentes)
5. [Workers y Colas](#5-workers-y-colas)
6. [NLP y Transcripción](#6-nlp-y-transcripción)
7. [Red y Gateways](#7-red-y-gateways)
8. [Riesgos de Seguridad](#8-riesgos-de-seguridad)
9. [Diseño MCP — mediadev-mcp](#9-diseño-mcp--mediadev-mcp)
10. [Implementación — Código](#10-implementación--código)
11. [Roadmap de Fases](#11-roadmap-de-fases)
12. [Despliegue e Integración](#12-despliegue-e-integración)
13. [Hallazgo Operativo: TV → MP3 → Destroyer → Clip](#13-hallazgo-operativo-tv--mp3--destroyer--clip)
14. [Estado Live al momento de la auditoría](#14-estado-live-al-momento-de-la-auditoría)

---

## 1. Arquitectura General

MediaDEV es un sistema de **monitoreo, grabación y auditoría 24/7** de 12 estaciones de radio y TV de Honduras. Captura streams vía gateways residenciales hondureños (necesarios por geo-restriction), los archiva en S3 y PostgreSQL, y alimenta un motor de detección de anuncios por fingerprinting acústico (Destroyer).

```
Internet Honduras ──► Gateways HN (RPi/PC, IP residencial, SOCKS5 :1080)
   ice42 / streamhch /          │  WireGuard VPN wg0  10.101.0.0/24
   streamtheworld / etc.        ▼
              DigitalOcean 159.223.104.91  (2 vCPU / 4 GB RAM)
              ┌──────────────────────────────────────────────────────────┐
              │                                                          │
              │  nginx :80 ──────────────────────── nginx :8080         │
              │    └─► gunicorn :9000                └─► uvicorn :9001  │
              │        (dashboard Flask)                (publiaudit API) │
              │                                                          │
              │  supervisord: 12x ffmpeg ──► /var/www/streams/          │
              │  stream-daemon (Python)              │                   │
              │  mediadev-gateway-api :8765          │                   │
              │  mediadev-health-engine              │   PostgreSQL      │
              │  mediadev-monitor                    │◄► media-db (DO)  │
              │  video-segment-uploader ──────────► S3                  │
              │  medio-orchestrator (Supabase)       │                   │
              │  publiaudit-api (FastAPI)            │                   │
              └──────────────────────────────────────────────────────────┘
                                      │
                          cron 4x/día │
                                      ▼
                          Destroyer (droplet efímero c-16)
                          fingerprinting MP3 → detecciones → DB
```

### Flujo de datos principal

```
[Stream HN] → [Gateway SOCKS5] → [ffmpeg HLS] → /var/www/streams/{id}/
                                                     │
                               stream-daemon ─────── │ ─────► PostgreSQL
                               (health 15s)          │         (estado, métricas,
                               (grabación 2h)        │          eventos)
                                                     │
                         video-uploader ─────────────┘──────► S3
                         (TV .ts cada 15s)                    video_segments/
                                                                     │
                              cron 0,6,12,18h                        │
                              Destroyer ──────────── MP3s/S3 ────────┘
                              (c-16 efímero)         fingerprint → detecciones → DB
```

---

## 2. Inventario de Componentes

### Servicios systemd (todos activos en producción)

| Servicio | Puerto | Rol |
|---|---|---|
| `supervisor` | — | Gestiona 12 procesos ffmpeg (uno por stream) |
| `stream-daemon` | — | Health check 15s, grabación MP3 2h, espejo a PG |
| `mediadev-gateway-api` | `:8765` (0.0.0.0) | Recibe heartbeats de gateways (Flask, Bearer token) |
| `mediadev-health-engine` | — | Calcula health score, ejecuta failover automático |
| `mediadev-monitor` | — | Vigila WireGuard, alertas Telegram |
| `video-segment-uploader` | — | Sube segmentos .ts de TV a S3 cada 15s |
| `dashboard-mediadev` | `:9000` (localhost) | Flask/Gunicorn 1 worker, API REST pública |
| `publiaudit-api` | `:9001` (localhost) | FastAPI + JWT, API de negocio PubliAudit |
| `nginx` | `:80`, `:8080` | Reverse proxy para ambas APIs |
| `privoxy` | `:3128` (localhost) | Bridge HTTP→SOCKS5 para ffmpeg |
| `medio-orchestrator` | — | MVP Medios: pipeline Whisper+LLM (Supabase) |
| `mediadev-bot` | — | Telegram bot |
| `wg-quick@wg0` | `:51820/udp` | Túnel WireGuard con Honduras |

### Procesos supervisord (12 streams)

| ID | Nombre | Tipo | Transporte |
|---|---|---|---|
| `fm_941` | 94.1 FM | Radio | gateway (ice42) |
| `radio_america` | Radio América | Radio | auto (streamtheworld) |
| `radio_choluteca` | Radio Choluteca | Radio | gateway (ice42) |
| `radio_el_patio` | Radio El Patio | Radio | auto (directo) |
| `radio_globo` | Radio Globo | Radio | auto (directo) |
| `radio_satelite` | Radio Satélite | Radio | gateway (ice42) |
| `suave_fm` | Suave FM | Radio | gateway (ice42) |
| `xy_hrn` | XY HRN | Radio | gateway (ice42) |
| `xy_sps` | XY SPS | Radio | gateway (ice42) |
| `xy_tgu` | XY TGU | Radio | gateway (ice42) |
| `hch_tv` | HCH TV | **TV (video)** | auto (streamhch) |
| `teleceiba` | Teleceiba | **TV (video)** | auto (directo) |

### Archivos clave del sistema

| Ruta | Descripción |
|---|---|
| `/opt/media-ai/config/stations.json` | Estaciones activas + definición de gateways |
| `/opt/media-ai/daemon/stream_daemon.py` | Daemon principal de health + grabación |
| `/opt/media-ai/dashboard/dashboard_v4.py` | API REST + dashboard Flask |
| `/opt/media-ai/scripts/stream_run.sh` | Runner unificado (todos los streams) |
| `/opt/media-ai/scripts/video_segment_uploader.py` | TV .ts → S3 |
| `/opt/media-ai/scripts/gateway_switch.sh` | Cambia gateway activo (NO editar a mano) |
| `/opt/destroyer/worker.py` | Motor de fingerprinting (corre en Destroyer) |
| `/opt/destroyer/launcher.py` | Lanza el droplet Destroyer vía DO API |
| `/opt/destroyer/gateway/engine/gateway_api.py` | API de heartbeats de gateways |
| `/opt/destroyer/gateway/engine/health_engine.py` | Motor de salud + failover |
| `/opt/publiaudit-api/main.py` | API FastAPI de PubliAudit |
| `/opt/medio-ai/mvp-medios/pipeline.py` | Pipeline NLP: Whisper→LLM→clips |
| `/etc/mediadev/gateway.conf` | Gateway activo (fuente de verdad) |
| `/etc/mediadev-db.env` | Credenciales PostgreSQL (chmod 600) |
| `/etc/mediadev-s3.env` | Credenciales AWS S3 (chmod 600) |
| `/var/www/streams/{id}/index.m3u8` | Playlist HLS activa |
| `/var/www/streams/{id}/recordings/` | MP3 horarios |

---

## 3. Base de Datos

**PostgreSQL managed** (DigitalOcean) — instancia única `destroyer_db`, compartida por todos los componentes.

Host: `private-media-db-do-user-2116998-0.d.db.ondigitalocean.com:25060`

### Tablas (22 total)

#### Operación de streams

```sql
mediadev_stream_status          -- Estado live de los 12 streams (1 fila por stream)
  stream_id TEXT PK
  status    TEXT    -- OK | STALE | NO_M3U8 | DISABLED
  sup       TEXT    -- Estado supervisord (RUNNING | FATAL | BACKOFF)
  segs      INT     -- Segmentos .ts activos en disco
  age       INT     -- Segundos desde última actualización del m3u8
  cb_state  TEXT    -- CLOSED | OPEN (circuit breaker)
  cb_fails  INT     -- Fallos consecutivos actuales
  restart_today INT -- Reinicios en el día actual
  last_down BIGINT  -- Unix epoch de última caída
  last_up   BIGINT  -- Unix epoch de última recuperación
  updated_at BIGINT

mediadev_metrics                -- Snapshot cada 60s (retención 7 días)
  stream_id, ts, status, segs, bytes

mediadev_events                 -- Transiciones DOWN/UP/CB_OPEN/CB_CLOSE (retención 30 días)
  stream_id, ts, etype, detail
```

#### Red de gateways

```sql
gateways                        -- Inventario de nodos Honduras
  gateway_id TEXT PK            -- hn01 | hn02 | hn03
  name, city, device_type
  wg_ip     TEXT                -- IP en la VPN (10.101.0.x)
  status    TEXT                -- healthy | degraded | failing | failed | probation
  score     INT                 -- 0-100
  priority  INT                 -- 1=primary, 2=failover-1, 3=failover-2
  maintenance BOOL
  last_heartbeat TIMESTAMPTZ
  circuit_breaker_until TIMESTAMPTZ

gateway_health_log              -- Telemetría por heartbeat (cada 30s)
  gateway_id, recorded_at
  cpu_pct, ram_pct, temp_c, uptime_s
  wg_handshake_age_s, internet_ok, socks5_ok
  latency_ms, packet_loss_pct, external_ip

stream_assignments              -- Qué streams van por qué gateway
failover_events                 -- Historial de failovers automáticos
```

#### Negocio PubliAudit

```sql
clients                         -- Empresas/agencias cliente
users                           -- Usuarios por cliente (bcrypt + JWT)
campaigns                       -- Campañas publicitarias
plans                           -- Planes de suscripción

advertisements                  -- Catálogo de spots registrados
  id UUID PK, name, duration_sec, s3_key (fingerprint .mp3)
  match_min INT                 -- Umbral mínimo de matches para detección

fingerprint_detections          -- Detecciones de anuncios (tabla central)
  client_id UUID FK → clients
  campaign_id UUID FK → campaigns
  ad_id UUID FK → advertisements
  stream_id VARCHAR FK → stream_catalog
  air_time TIMESTAMPTZ          -- Hora al aire (HN GMT-6)
  ts_seconds INT                -- Offset en el archivo MP3
  score INT                     -- Fuerza del match (> 300 = alta confianza)
  confidence_level VARCHAR      -- very_high | high | medium | low (generado)
  clip_s3_key VARCHAR           -- Clave S3 del clip MP4 de evidencia
  algorithm VARCHAR             -- 'Constellation Map (Shazam)'
  deleted_at TIMESTAMPTZ        -- Soft delete

detection_summary_daily         -- Agregados diarios precalculados
monitored_streams               -- Qué streams monitorea cada cliente
client_streams                  -- Alias de monitored_streams
report_public_links             -- Reportes compartibles con QR
audit_log                       -- Auditoría de acciones
```

#### Destroyer (motor de detección)

```sql
destroyer_runs                  -- Historial de corridas del motor
  id SERIAL PK
  status TEXT                   -- deploying | running | done | timeout | error | destroyed
  t1_deployed, t2_started, t3_completed, t4_destroyed  TIMESTAMPTZ
  total_files INT               -- MP3s a procesar
  files_done INT                -- MP3s completados
  files_error INT               -- MP3s con error
  total_detections INT          -- Detecciones en esta corrida
  cost_usd NUMERIC              -- Costo del droplet en USD
  work_seconds INT

s3_scan_log, scan_history       -- Logs de escaneo S3
stream_catalog                  -- Catálogo de 195 estaciones
```

---

## 4. APIs Existentes

### API pública (puerto 80, sin autenticación)

Servida por `dashboard_v4.py` vía nginx. CORS abierto.

| Endpoint | Método | Descripción |
|---|---|---|
| `/api/summary` | GET | KPIs globales: streams vivos, catálogo, detecciones hoy/total |
| `/api/streams` = `/api/status` | GET | Estado live de los 12 streams |
| `/api/stations` | GET | Catálogo 195 estaciones. Filtros: `?status=active\|inactive`, `?type=radio\|tv` |
| `/api/detections` | GET | Detecciones recientes. `?limit=N` (máx 500) |
| `/health` | GET | `200 OK` (nginx) |

```bash
curl http://159.223.104.91/api/summary
curl 'http://159.223.104.91/api/stations?type=tv'
curl 'http://159.223.104.91/api/detections?limit=20'
```

### Gateway API (puerto 8765, Bearer token)

Interna — accesible desde gateways vía WireGuard.

| Endpoint | Método | Auth | Descripción |
|---|---|---|---|
| `/api/gateway/heartbeat` | POST | Bearer | Recibe métricas del agente cada 30s |
| `/api/gateway/status` | GET | Bearer | Estado de todos los gateways con telemetría |
| `/api/gateway/command` | POST | Bearer | Encola comando para un gateway (drain/undrain) |
| `/api/gateway/health` | GET | Ninguna | Healthcheck del API |

### PubliAudit API (puerto 8080, JWT)

FastAPI. Docs en `http://159.223.104.91:8080/api/docs`.

| Endpoint | Método | Descripción |
|---|---|---|
| `/api/auth/login` | POST | Login → JWT token |
| `/api/auth/me` | GET | Perfil del usuario autenticado |
| `/api/dashboard/kpis` | GET | KPIs por cliente |
| `/api/campaigns` | GET | Campañas del cliente |
| `/api/campaigns/{id}/ads` | GET | Spots de una campaña |
| `/api/detections` | GET | Detecciones filtradas por cliente |
| `/api/timeline` | GET | Timeline de detecciones por stream y hora |
| `/api/comprobante/{ad_id}` | GET | PDF/HTML de comprobante |
| `/api/streams` | GET | Streams disponibles para monitorear |
| `/api/my-streams` | GET/POST/DELETE | Streams suscritos por el cliente |
| `/api/runs` | GET | Historial de corridas de Destroyer |
| `/api/reports` | GET/POST/PATCH/DELETE | Reportes compartibles |
| `/api/public/{token}` | GET | Portal público de evidencia (sin auth) |
| `/api/mediadev/summary` | GET | Proxy a la API pública de MediaDEV |
| `/api/health` | GET | Healthcheck |

---

## 5. Workers y Colas

### Cron jobs (crontab de root)

```
*/15 * * * *        python3 /opt/destroyer/watchdog.py
* * * * *           python3 /opt/media-ai/scripts/gateway_watchdog.py
0 0,6,12,18 * * *   /opt/destroyer/venv/bin/python /opt/destroyer/launcher.py
```

### Destroyer — Motor de detección de anuncios

Droplet efímero `c-16` (16 vCPU, nyc1). Flujo:

```
launcher.py (cron 4x/día)
  → Crea droplet c-16 con cloud-init
  → worker.py arranca: carga fingerprints de S3
  → Lista MP3s horarios en S3 (últimas horas)
  → Procesa en paralelo (28-32 workers):
       fingerprint.py → match() → detección
  → Guarda en fingerprint_detections
  → Genera clip MP4 de evidencia (video_segments/ en S3)
  → Notifica por Telegram
  → Se destruye a sí mismo
```

Snapshot: `232634281` (v8) en DigitalOcean. Se recrea si cambia `worker.py`.

Parámetros clave:
- `CLIP_PAD = 5` segundos por defecto, con override por anuncio vía `advertisements.clip_pad_seconds`
- `MATCH_MIN = auto` (max(50, duration × 2.5))
- `WORKERS = 28-32` threads paralelos
- `CHUNK_SEC = 600` (10 min por chunk de audio)

### Stream Daemon

Loop único, intervalos mínimos seguros para 2 vCPU:

| Tarea | Intervalo | Descripción |
|---|---|---|
| Health check | 15s | Verifica m3u8 age + segs. UPSERT a PG. Circuit Breaker |
| Metrics | 60s | Snapshot a `mediadev_metrics` |
| Recordings | 120s | Genera MP3 horario de la hora anterior |
| Cleanup | 30 min | Elimina .ts > 8h, purga métricas/eventos en PG |

### Video Segment Uploader

Escanea cada 15s los segmentos `.ts` de TV y los sube a S3:

```
s3://mediadev-recordings/video_segments/{stream}/{YYYY}/{MM}/{DD}/{epoch_start}_{epoch_end}.ts
```

Los epochs se calculan del `mtime` del archivo. Estos segmentos son la fuente de clips de evidencia que usa Destroyer.

---

## 6. NLP y Transcripción

Stack separado en `/opt/medio-ai/mvp-medios/` (MVP Medios). **No comparte DB con MediaDEV/Destroyer** — usa Supabase.

### Pipeline completo

```
Input (URL YouTube o MP3 local)
  ↓ transcribe_audio.py    Whisper API (OpenAI) → words_path.json
  ↓ chunk_words.py         Segmentación en chunks → chunks_path.json
  ↓ analyze_narrative_llm.py  LLM (GPT) → propuestas de noticias
  ↓ apply_rules.py         Validación con reglas deterministas
  ↓ map_words_to_time.py   Mapeo palabras → timestamps reales
  ↓ clip_audio.py          Generación de clips MP3 por noticia
  ↓ merge_clips.py         Fusión de clips adyacentes
  ↓ summarize_news.py      Resúmenes con corrección de entidades
  ↓ detect_ads.py          Detección de anuncios (pista paralela)
```

### Orquestador

`medio-orchestrator` (systemd) sondea tabla `sources` de Supabase cada 30s y lanza un proceso `run.py` por fuente activa (máx 7 simultáneas).

### Dependencias NLP

```
openai>=1.30.0
ffmpeg-python>=0.2.0
supabase>=2.0.0
numpy, scipy (para audfprint)
```

---

## 7. Red y Gateways

### Topología WireGuard

```
MediaDEV 159.223.104.91 (wg0: 10.101.0.1/24)
  ├── hn01 RPi Honduras 01   vpn: 10.101.0.2  (failover-1,  disabled)
  ├── hn02 PC-LCE Gateway    vpn: 10.101.0.5  (primary,     ACTIVO)
  └── hn03 RPi-Levi          vpn: 10.101.0.6  (failover-2,  disabled)
```

Puerto WireGuard: `51820/udp`.

### Failover automático

`health_engine.py` mantiene una máquina de estados por gateway:

```
healthy → degraded → failing → failed → probation → healthy
```

Score 0-100 basado en: latencia, packet loss, CPU, SOCKS5 ok, handshake WireGuard. Si el primary cae, el health engine ejecuta `gateway_switch.sh <id>` para cambiar el gateway activo en todos los scripts de ffmpeg sin editar nada manualmente.

### Proxy Layer

- **SOCKS5 directo** (ice42/Icecast): `curl --socks5-hostname 10.101.0.5:1080 URL | ffmpeg -i pipe:0`
- **Privoxy HTTP→SOCKS5** (ffmpeg nativo): `ffmpeg -http_proxy http://127.0.0.1:3128`

Streams que van por gateway: `xy_hrn, xy_tgu, xy_sps, radio_satelite, fm_941, suave_fm, radio_choluteca`.  
Streams directos (auto): `radio_america, radio_globo, radio_el_patio, hch_tv, teleceiba`.

---

## 8. Riesgos de Seguridad

| # | Riesgo | Severidad | Detalle | Recomendación |
|---|---|---|---|---|
| 1 | JWT secret hardcodeado | 🔴 Alta | `publiaudit-api/main.py` línea 29: `JWT_SECRET = os.environ.get('JWT_SECRET', 'publiaudit_jwt_secret_change_in_prod_2026')`. Si la env var no está seteada, usa este valor. | Verificar que `JWT_SECRET` esté en el env del servicio. Rotar si el fallback está activo. |
| 2 | Credenciales PG en código | 🔴 Alta | Mismo archivo, líneas 24-28: `PG_PASS` tiene el password real como default. | Mover a `/etc/publiaudit-db.env` y cargar vía `EnvironmentFile` en systemd. Remover el hardcoded. |
| 3 | Gateway API en 0.0.0.0:8765 | 🟠 Media | El Gateway API escucha en todas las interfaces, no solo WireGuard. Cualquier IP puede intentar conectarse aunque necesite token. | Restringir a `10.101.0.0/24` en la config de Flask (`host='10.101.0.1'`) o via UFW: `ufw allow from 10.0.0.0/8 to any port 8765`. |
| 4 | PostgreSQL :5432 en 0.0.0.0 | 🟠 Media | `ss -tlnp` muestra Postgres escuchando en `0.0.0.0:5432`. Probablemente es una instancia local de prueba. | Verificar si es necesaria. Si no, `systemctl stop postgresql && systemctl disable postgresql`. |
| 5 | Dashboard público sin auth | 🟡 Baja | `/api/*` en puerto 80 es público sin token. Intencional para consumo externo, pero expone datos operativos. | Agregar IP allowlist en nginx o un Bearer token simple para `/api/` si se requiere más privacidad. |
| 6 | CORS `allow_origins: *` | 🟡 Baja | PubliAudit FastAPI tiene CORS abierto. Tiene JWT así que el riesgo es bajo, pero permite llamadas cross-origin desde cualquier dominio. | Restringir a dominios conocidos del frontend. |
| 7 | Credenciales en `.env` visible en git history | 🟡 Baja | `.env` está en `.gitignore` pero backups como `.env.backup.XXXX` podrían no estarlo. | Asegurar que todos los archivos `.env*` estén en `.gitignore`. Auditar `git log --all -- '*.env*'`. |

---

## 9. Diseño MCP — mediadev-mcp

### Principios de diseño

- **Solo lectura en v1**: conexión PG con `default_transaction_read_only=on`. Ninguna herramienta escribe, reinicia ni despliega.
- **Sin secretos en output**: IPs públicas de gateways enmascaradas, credenciales nunca en respuestas.
- **Transport stdio**: el servidor se invoca via SSH. La seguridad es la llave SSH — no se abre ningún puerto adicional.
- **Credenciales desde archivos del sistema**: lee `/etc/mediadev-db.env` (chmod 600), igual que el daemon y el dashboard.
- **Tolerante a fallos**: cada tool captura excepciones y retorna `{"error": "..."}` en lugar de crashear.

### Herramientas — Fase A (implementadas)

#### `get_system_status()`

**Descripción:** Estado actual de los 12 streams de radio y TV.

**Parámetros:** ninguno

**Fuente de datos:** `mediadev_stream_status` (PostgreSQL)

**Respuesta:**
```json
{
  "streams_ok": 12,
  "streams_stale": 0,
  "streams_no_m3u8": 0,
  "streams_disabled": 0,
  "circuit_breakers_open": 0,
  "total": 12,
  "updated": "2026-06-13T08:36:20-06:00",
  "streams": [
    {
      "id": "xy_hrn",
      "status": "OK",
      "supervisord": "RUNNING",
      "segments": 10,
      "age_seconds": 4,
      "cb_state": "CLOSED",
      "cb_fails": 0,
      "restarts_today": 0,
      "last_down": null,
      "last_up": "2026-06-12T18:02:00-06:00"
    }
  ]
}
```

**Riesgos:** Ninguno — datos operativos no confidenciales.

---

#### `get_workers()`

**Descripción:** Estado de los 12 procesos ffmpeg (supervisord) y servicios systemd.

**Parámetros:** ninguno

**Fuente de datos:** `supervisorctl status` + `systemctl is-active`

**Respuesta:**
```json
{
  "supervisord": {
    "running": 12,
    "fatal": 0,
    "total": 12,
    "processes": [
      { "name": "stream_xy_hrn", "state": "RUNNING", "pid": 12345, "uptime": "2:03:14" }
    ]
  },
  "systemd": {
    "all_active": true,
    "inactive": [],
    "services": {
      "stream-daemon": "active",
      "nginx": "active",
      "mediadev-gateway-api": "active",
      "mediadev-health-engine": "active",
      "publiaudit-api": "active",
      "video-segment-uploader": "active",
      "privoxy": "active"
    }
  }
}
```

**Riesgos:** Bajo — expone nombres de procesos y PIDs.

---

#### `get_queue_stats(limit=5)`

**Descripción:** Estado del motor Destroyer: corridas, archivos procesados, detecciones, costos.

**Parámetros:** `limit` (int, 1-20, default 5)

**Fuente de datos:** `destroyer_runs` + `fingerprint_detections` + `advertisements`

**Respuesta:**
```json
{
  "last_run": {
    "id": 23,
    "status": "timeout",
    "started_hn": "2026-06-13T06:00:00-06:00",
    "completed_hn": null,
    "files_done": 59,
    "total_files": 97,
    "files_error": 0,
    "total_detections": 0,
    "cost_usd": 0.19,
    "work_seconds": 3600,
    "droplet": "destroyer-20260613-060000"
  },
  "recent_runs": [ ... ],
  "detections_last_24h": 105,
  "detections_total": 398,
  "advertisements_registered": 7,
  "cron_schedule": "0 0,6,12,18 * * * (4 veces/día)"
}
```

**Riesgos:** Bajo — no expone audio, clips ni datos de clientes individuales.

---

#### `get_service_health()`

**Descripción:** Salud del ecosistema: gateways Honduras, WireGuard, PostgreSQL, Privoxy.

**Parámetros:** ninguno

**Fuente de datos:** `gateways` + `gateway_health_log` (DB) o `stations.json` (fallback) + `wg show wg0` + ping DB

**Respuesta:**
```json
{
  "wireguard": {
    "status": "up",
    "peers": 4,
    "active_peers": 3
  },
  "database": {
    "status": "ok",
    "latency_ms": 46.7
  },
  "privoxy": { "status": "active" },
  "active_gateway": "hn02",
  "gateway_source": "stations.json",
  "gateways": [
    {
      "id": "hn02",
      "name": "PC-LCE Gateway",
      "vpn_ip": "10.101.0.5",
      "status": "healthy",
      "enabled": true,
      "role": "primary",
      "priority": 1,
      "vpn_reachable": true,
      "score": null,
      "last_heartbeat": null
    }
  ]
}
```

**Nota sobre `gateway_source`:** la tabla `gateways` en DB está actualmente vacía (health_engine aún no escribe ahí). El tool usa `stations.json` como fallback. Cuando se popule la DB, cambiará automáticamente a `"database"`.

**Riesgos:** Bajo — IPs públicas de gateways enmascaradas (solo muestra VPN IPs internas 10.x.x.x).

---

#### `get_recent_errors(stream_id="", hours=6)`

**Descripción:** Últimos errores y eventos de transición del sistema.

**Parámetros:**
- `stream_id` (str, opcional): filtrar por stream. Ej: `"hch_tv"`, `"xy_hrn"`. Vacío = todos.
- `hours` (int, 1-48, default 6): ventana de búsqueda.

**Fuente de datos:** `mediadev_events` + `destroyer_runs`

**Respuesta:**
```json
{
  "period_hours": 6,
  "stream_filter": null,
  "total_events": 2,
  "event_counts": { "DOWN": 1, "UP": 1 },
  "events": [
    {
      "stream": "hch_tv",
      "type": "DOWN",
      "detail": null,
      "time_hn": "2026-06-13T07:38:00-06:00"
    },
    {
      "stream": "hch_tv",
      "type": "UP",
      "detail": null,
      "time_hn": "2026-06-13T07:42:00-06:00"
    }
  ],
  "destroyer_errors": []
}
```

**Tipos de evento:**
- `DOWN` → stream dejó de transmitir
- `UP` → stream recuperado
- `CB_OPEN` → circuit breaker abierto (5 fallos consecutivos)
- `CB_CLOSE` → circuit breaker cerrado (reset automático 30 min)

**Riesgos:** Bajo — patrones de fallo internos visibles, ningún dato de negocio.

---

## 10. Implementación — Código

### Estructura en el servidor

```
/opt/media-ai/mcp/
├── server.py              ← Punto de entrada MCP (FastMCP, stdio transport)
├── db.py                  ← Conexión PostgreSQL read-only con fallback a /etc/mediadev-db.env
├── tools/
│   ├── __init__.py
│   ├── system.py          ← get_system_status
│   ├── workers.py         ← get_workers
│   ├── queue.py           ← get_queue_stats
│   ├── health.py          ← get_service_health
│   └── errors.py          ← get_recent_errors
├── requirements.txt
├── install.sh
├── README.md
└── venv/                  ← Python 3.12, mcp 1.27.2
```

### Dependencias

```
mcp[cli]>=1.3.0
psycopg2-binary>=2.9.9
python-dotenv>=1.0.0
httpx>=0.27.0
```

### Decisiones de diseño

| Decisión | Razón |
|---|---|
| `stdio` transport (no HTTP) | Sin nueva superficie de ataque. Seguridad delegada a SSH. |
| `FastMCP` (no MCP raw) | API de alto nivel, decoradores `@mcp.tool()`, menos boilerplate |
| Credenciales desde `/etc/mediadev-db.env` | Mismo patrón que daemon y dashboard. Nunca en el repo. |
| `default_transaction_read_only=on` en PG | Garantía a nivel de conexión: imposible escribir aunque haya un bug |
| Fallback `stations.json` para gateways | La tabla `gateways` en DB está vacía; el tool funciona sin datos en DB |
| IPs de gateway enmascaradas | Las IPs residenciales hondureñas son dato sensible |
| Captura de excepciones por tool | El servidor no crashea si una tool falla; retorna `{"error": "..."}` |

### Branch y commit

```
Branch: feature/mcp-server
Commit: 0962233
Mensaje: feat: add mediadev-mcp server (Phase A - read-only observability)
```

---

## 11. Roadmap de Fases

### Fase A — Observabilidad ✅ IMPLEMENTADA

5 tools read-only. Tiempo: ~4h. **Seguro para exponer a IA.**

### Fase B — Consultas de base de datos

**Herramientas propuestas:**

| Tool | Descripción |
|---|---|
| `search_detections(stream_id, ad_name, date_from, date_to, limit)` | Busca detecciones con filtros |
| `get_detection_detail(detection_id)` | Detalle completo de una detección + URL del clip |
| `get_advertisement_catalog()` | Lista de spots registrados con duración y match_min |
| `get_stream_catalog(type, status)` | Catálogo de estaciones con URL y metadatos |

**Restricciones de seguridad:**
- Whitelist de tablas consultables (no `users`, no `clients`, no credenciales)
- Límite máximo de filas (500)
- Solo tablas con prefijo `mediadev_`, `fingerprint_detections`, `advertisements`, `stream_catalog`, `destroyer_runs`

**Complejidad:** Media. **Tiempo estimado:** 3-4h. **Seguro para IA** con whitelist.

### Fase C — Logs

**Herramientas propuestas:**

| Tool | Descripción |
|---|---|
| `search_logs(service, pattern, hours, limit)` | Busca en journald por servicio y patrón |
| `get_stream_log(stream_id, lines)` | Últimas N líneas del log de un stream |
| `get_destroyer_log(run_id)` | Log de una corrida específica del Destroyer |

**Restricciones de seguridad:**
- Sanitizar output: nunca retornar líneas con tokens, passwords o IPs privadas (regex filter)
- Whitelist de servicios consultables
- Límite de líneas (500)
- No permitir pattern `.*` sin límite — forzar ventana temporal

**Complejidad:** Media. **Tiempo estimado:** 4-6h. **Requiere sanitización antes de exponer a IA.**

### Fase D — GitHub

**Herramientas propuestas:**

| Tool | Descripción |
|---|---|
| `get_recent_commits(branch, limit)` | Últimos commits del repo |
| `get_open_issues()` | Issues abiertos |
| `compare_deployed_vs_main()` | Diff entre lo que está en producción y main |

**Complejidad:** Baja. **Tiempo estimado:** 2h. **Seguro para IA** (GitHub API read-only).

### Fase E — SSH controlado

**Herramientas propuestas:**

| Tool | Descripción |
|---|---|
| `run_safe_command(command_key, params)` | Ejecuta un comando de una whitelist estricta |

**Whitelist de comandos seguros:**
```python
SAFE_COMMANDS = {
    "supervisorctl_status": ["supervisorctl", "status"],
    "wg_show": ["wg", "show", "wg0"],
    "disk_usage": ["du", "-sh", "/var/www/streams/"],
    "df": ["df", "-h"],
}
```

**Comandos que NUNCA deben estar en la whitelist:**
`restart`, `stop`, `start`, `switch_gateway`, `rm`, `mv`, `deploy`, `git push`, cualquier cosa con `|`, `>`, `&&`, `;`

**Complejidad:** Alta. **Tiempo estimado:** 6-8h. **Requiere revisión de seguridad antes de exponer a IA.**

### Resumen del Roadmap

| Fase | Herramientas | Complejidad | Seguro para IA | Estado |
|---|---|---|---|---|
| A — Observabilidad | 5 tools | Baja | ✅ Sí | ✅ **HECHO** |
| B — Consultas DB | 4 tools | Media | ✅ Con whitelist | ⏳ Pendiente |
| C — Logs | 3 tools | Media | ⚠️ Con sanitización | ⏳ Pendiente |
| D — GitHub | 3 tools | Baja | ✅ Sí | ⏳ Pendiente |
| E — SSH controlado | 1 tool | Alta | ⚠️ Solo whitelist estricta | ⏳ Pendiente |

---

## 12. Despliegue e Integración

### En el servidor (ya hecho)

```bash
# El servidor ya está instalado en feature/mcp-server
git -C /opt/media-ai log feature/mcp-server --oneline -1
# 0962233 feat: add mediadev-mcp server (Phase A - read-only observability)

# Para verificar que funciona:
export PG_HOST=private-media-db-do-user-2116998-0.d.db.ondigitalocean.com
export PG_PORT=25060; export PG_DB=destroyer_db
export PG_USER=destroyer; export PG_PASS=<ver /etc/mediadev-db.env>

/opt/media-ai/mcp/venv/bin/python -c "
import sys; sys.path.insert(0, '/opt/media-ai/mcp')
from tools.system import get_system_status
import json; print(json.dumps(get_system_status(), indent=2))
"
```

### Integración con Claude Code

El archivo `C:\Users\Sedesol\AppData\Roaming\Claude\claude_desktop_config.json` ya fue actualizado con:

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

**Requiere reiniciar Claude Code** para activar el MCP server.

### Integración con otras máquinas o devs

Cualquier desarrollador con acceso SSH al servidor puede usar el MCP:

```json
{
  "mcpServers": {
    "mediadev": {
      "command": "ssh",
      "args": [
        "-i", "/ruta/a/keySED",
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

### Integración con Codex / OpenAI Agents (Python SDK)

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command="ssh",
    args=[
        "-i", "/ruta/a/keySED",
        "-o", "StrictHostKeyChecking=no",
        "root@159.223.104.91",
        "/opt/media-ai/mcp/venv/bin/python",
        "/opt/media-ai/mcp/server.py"
    ]
)

async def main():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Listar herramientas disponibles
            tools = await session.list_tools()
            print([t.name for t in tools.tools])

            # Llamar una herramienta
            result = await session.call_tool("get_system_status", {})
            print(result.content[0].text)

            result = await session.call_tool("get_recent_errors", {
                "stream_id": "hch_tv",
                "hours": 24
            })
            print(result.content[0].text)

asyncio.run(main())
```

### Integración con OpenAI Agents SDK

```python
from agents.mcp import MCPServerStdio

server = MCPServerStdio(
    params={
        "command": "ssh",
        "args": [
            "-i", "/ruta/a/keySED",
            "-o", "StrictHostKeyChecking=no",
            "root@159.223.104.91",
            "/opt/media-ai/mcp/venv/bin/python",
            "/opt/media-ai/mcp/server.py"
        ]
    }
)

# El agente tendrá acceso automático a las 5 tools de mediadev-mcp
```

### Ejemplos de uso en lenguaje natural

```
# Observabilidad básica
"¿Cuántos streams están vivos ahora mismo?"
"¿Algún stream tiene el circuit breaker abierto?"
"¿Cuál es el gateway activo de Honduras?"
"¿La base de datos está respondiendo bien?"

# Estado del motor de detección
"¿Cuántas detecciones de anuncios hubo hoy?"
"¿Cuál fue el resultado de la última corrida del Destroyer?"
"¿Cuántos anuncios tenemos registrados?"
"¿El Destroyer tuvo errores en la madrugada?"

# Diagnóstico de problemas
"¿Hay errores en hch_tv de las últimas 6 horas?"
"¿Qué servicios de systemd están caídos?"
"¿Algún proceso ffmpeg está en FATAL?"
"Muéstrame los eventos de todas las estaciones de las últimas 12 horas"
```

---

## 13. Hallazgo Operativo: TV → MP3 → Destroyer → Clip

### Resumen ejecutivo

La auditoría de Claude fue buena para mapear arquitectura, servicios y superficies MCP, pero no capturó el bug operativo fino del pipeline de evidencia TV. El problema real no estaba en el MCP ni en el fingerprinting base, sino en la derivación del `air_time` absoluto cuando Destroyer intentaba convertir una detección sobre un MP3 horario de TV en un clip MP4 real desde `video_segments/`.

### Bug real identificado

En TV existían dos convenciones temporales mezcladas:

- Los segmentos de video en S3 (`video_segments/.../{epoch_start}_{epoch_end}.ts`) están anclados a epoch UTC real.
- Los MP3 horarios que consume Destroyer se nombran por hora Honduras (`YYYY-MM-DD_HHh.mp3`) en el flujo actual.
- Pero algunos MP3 históricos de TV quedaron etiquetados como si esa hora textual fuera UTC.

El efecto era sutil pero operativo: Destroyer detectaba bien el anuncio dentro del MP3, pero al derivar `air_time` desde el nombre del archivo podía quedar corrido varias horas para TV histórica. Cuando luego buscaba los `.ts` correctos en S3 para construir el MP4 de evidencia, el clip salía desalineado o directamente no encontraba segmentos compatibles.

### Corrección aplicada en código

La corrección real está en `air_time_from_item()` de `/opt/destroyer/worker.py`:

- Para radio, y para TV nueva, se sigue interpretando el nombre del MP3 en hora Honduras.
- Para TV, si `s3_scan_log.created_at` está disponible, Destroyer evalúa dos candidatos:
  - interpretar el nombre como hora Honduras
  - interpretar el nombre como hora UTC y convertirlo a HN
- Luego elige la interpretación más consistente con la hora en que el archivo fue registrado en `s3_scan_log`.
- Si la opción correcta es la UTC histórica, deja un log `[timefix] ... interpretado como hora UTC para alinear video/audio`.

En otras palabras: no se cambió el fingerprinting; se corrigió la traducción de “detección en MP3” a “instante absoluto para cortar evidencia en video”.

### Comportamiento esperado del padding por anuncio

El documento auditado decía `CLIP_PAD = 10`, pero el código actual implementa otra cosa:

- El padding global por defecto es `CLIP_PAD = 5`.
- Ese valor puede variar por anuncio usando `advertisements.clip_pad_seconds`.
- Al cargar referencias, Destroyer construye `_CLIP_PADS` por `ad_name`.
- Al procesar una detección, usa `clip_pad=_CLIP_PADS.get(ad_name, CLIP_PAD)`.

Comportamiento esperado:

- Audio radio: clip desde `ts_sec - clip_pad` hasta `ts_sec + ref_dur + clip_pad`.
- TV video: misma ventana temporal, pero aplicada sobre los `.ts` de `video_segments/` usando `air_time` absoluto ya corregido.
- Resultado: cada anuncio puede tener evidencia más corta o más generosa sin tocar el worker ni redeployar lógica.

### Riesgos todavía abiertos para TV evidence

Siguen abiertos varios riesgos que no son el bug principal, pero sí pueden afectar evidencia TV:

- Si la fuente TV cae en modo audio-only, `cut_video_clip()` descarta el MP4 y hoy no hace fallback a clip MP3 para TV.
- Si faltan segmentos `.ts` en S3 o hay huecos alrededor del intervalo, el MP4 puede salir incompleto o no generarse.
- La heurística de `created_at` resuelve bien TV histórica conocida, pero depende de que `s3_scan_log.created_at` exista y sea confiable.
- El uploader de TV y Destroyer están acoplados temporalmente: si cambia la convención de nombre del MP3 sin mantener la relación con los epochs del video, el desalineamiento puede reaparecer.
- El clip se arma por concatenación de segmentos y corte con `ffmpeg`; errores de continuidad entre `.ts` todavía pueden producir evidencia visual defectuosa aunque el anuncio sí haya sido detectado.

### Conclusión operativa

La lectura corta correcta es esta: el sistema sí detectaba anuncios, pero el punto frágil estaba en la sincronización temporal entre el MP3 horario de TV y los segmentos de video usados como evidencia. La corrección aplicada ataca justo esa frontera y deja al MCP fuera del problema.

---

## 14. Estado Live al Momento de la Auditoría

**Fecha de captura:** 2026-06-13 ~08:36 HN (GMT-6)

| Métrica | Valor |
|---|---|
| Streams activos (OK) | **12 / 12** |
| Circuit breakers abiertos | **0** |
| Procesos supervisord RUNNING | **12 / 12** |
| Servicios systemd inactive | **0** |
| Gateway activo | **hn02** (PC-LCE, prioridad 1) |
| WireGuard peers conectados | **4 peers, 3 activos** |
| Latencia a PostgreSQL | **~47 ms** |
| Detecciones totales en DB | **398** |
| Detecciones últimas 24h | **105** |
| Anuncios registrados | **7** |
| Última corrida Destroyer (#23) | **timeout** (59/97 archivos procesados) |
| Corridas totales Destroyer | **23** |

---

*Documento generado por Claude Code (claude-sonnet-4-6) — 2026-06-13*  
*Repo: `/opt/media-ai` branch `feature/mcp-server` commit `0962233`*
