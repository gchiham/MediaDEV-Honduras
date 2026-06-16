# live_mediaDEV.md — Ecosistema MediaDEV: Referencia Viva

**Última actualización:** 15 junio 2026  
**Versión del documento:** 2.0  
**Servidores:** mediaCAP `159.223.104.91` (captura) · mediaAPP `137.184.53.234` (app/control)  
**Mantenido por:** equipo MediaDEV — actualizar cada vez que cambie arquitectura, schema, servicios, o decisiones de diseño

> Este documento es la fuente de verdad del ecosistema MediaDEV. Cualquier dev o AI que lo lea debe poder entender el sistema completo sin necesidad de preguntas adicionales. Vive en el repo de GitHub `gchiham/MediaDEV-Honduras`.

---

## 0. Versionado en GitHub

Todo el código y la config operativa está espejado en GitHub. **La verdad es lo desplegado
en los servidores; los repos son el espejo versionado.** Secretos nunca en git.

| Repo | Contenido | Vis. |
|---|---|---|
| `gchiham/MediaDEV-Honduras` | mediaCAP `/opt/media-ai` (captura: daemon, scripts, monitor, mcp, dashboard, config) | público |
| `gchiham/media-app` | mediaAPP `/opt/media-app` (producto SaaS + evidence portal, FastAPI) | privado |
| `gchiham/destroyer` | `/opt/destroyer` de ambos nodos (`app/`=mediaAPP orquestación, `cap/`=mediaCAP gateway engine) | privado |
| `gchiham/mediadev-infra` | config operativa (systemd, supervisor, nginx, privoxy, wireguard redactado) + bot + mcp mediaAPP + `INVENTORY.md` | privado |

`mediaCAP:/opt/media-ai` es un working tree git real (pushea con llave SSH autorizada). Los
otros son espejos: si cambiás algo en producción, hay que re-capturarlo y commitear (clones
locales en `C:\GusHD\destroyer_sync` y `C:\GusHD\infra_sync`). Mapa servicio→código→repo
completo en `mediadev-infra/INVENTORY.md`.

---

## 1. Infraestructura

### Topología — 2 nodos DO (split 14 jun 2026) + Destroyer en AWS

El ecosistema de captura/app corre en **2 droplets** DigitalOcean en `nyc1`, **misma VPC**
(`b9207f9e-dc84-11e8-8650-3cfdfea9f8c8`) → red privada + DB privada entre ellos. La DB managed
y S3 son compartidos. **El Destroyer ya NO corre en DigitalOcean** — migró a **AWS** (ver §4).

**mediaCAP** — nodo de CAPTURA (antes "mediaDEV", droplet `575328180`)
| Campo | Valor |
|---|---|
| IP pública / privada | `159.223.104.91` / `10.136.0.7` |
| Tamaño | 2 vCPU / 4 GB · Ubuntu 24.04 |
| Acceso | `ssh -i ~/.ssh/keySED root@159.223.104.91` |
| Corre | 13× ffmpeg (supervisord), stream-daemon, video-segment-uploader, wireguard, privoxy, gateway-api, health-engine, mediadev-monitor, MCP (captura) |

**mediaAPP** — nodo de APP/CONTROL (droplet `577521810`)
| Campo | Valor |
|---|---|
| IP pública / privada | `137.184.53.234` / `10.136.0.6` |
| Tamaño | 2 vCPU / 2 GB · Ubuntu 24.04 |
| Acceso | `ssh -i ~/.ssh/keySED root@137.184.53.234` |
| Corre | media-app + nginx (producto SaaS + evidence portal), chihambot (bot Telegram), MCP (app). El código del Destroyer (`launcher.py`/`watchdog.py`) vive en `/opt/destroyer` pero la orquestación corre en AWS, NO por cron local. |

> **Regla:** mediaCAP solo captura (máximo recurso para grabar); mediaAPP el producto + apps de control. Servicios que leen estado local de captura (ffmpeg, segmentos en disco) se quedan en mediaCAP. `medio-orchestrator` (legacy mvp-medios) y el dashboard viejo (`dashboard_v4.py`) fueron eliminados — `dashboard_mediadev.service` existe pero está **inactivo**, no corre ningún dashboard.

### Base de datos — PostgreSQL Managed (DigitalOcean)

| Campo | Valor |
|---|---|
| Host privado | `private-media-db-do-user-2116998-0.d.db.ondigitalocean.com` |
| Puerto | `25060` |
| Usuario Destroyer | `destroyer` |
| Base de datos | `destroyer_db` |
| SSL | requerido (`sslmode=require`) |

**Conectarse desde servidor mediaDEV:**
```bash
PGPASSWORD='<PG_PASS>' psql \
  -h private-media-db-do-user-2116998-0.d.db.ondigitalocean.com \
  -p 25060 -U destroyer -d destroyer_db
```

**Conectarse desde local vía SSH tunnel:**
```bash
ssh -i ~/.ssh/keySED -L 5433:private-media-db-do-user-2116998-0.d.db.ondigitalocean.com:25060 root@159.223.104.91 -N &
psql -h localhost -p 5433 -U destroyer -d destroyer_db
```

### Storage — S3

| Campo | Valor |
|---|---|
| Bucket | `mediadev-recordings` |
| Región | `us-east-1` |
| Rutas clave | `{stream_id}/YYYY/MM/*.mp3` (MP3s horarios), `video_segments/` (TS TV, lifecycle 30 días), `clips/` (evidencia), `destroyer/releases/` (código Destroyer) |

> **Lifecycle de `video_segments/`** subido a 30 días (14 jun 2026): el clip de evidencia de
> VIDEO se arma bajando los `.ts` de S3; con 1 día de retención, reprocesar TV viejo daba
> `clip=None`. 30 días cubre demos y backfills (~1 mes). Ojo: video pesa, 30d ≈ 30× el storage.

### VPN y Gateways Honduras

```
mediaDEV (nyc1)
    │
    └── WireGuard VPN (wg0, 10.101.0.1/24)
         ├── hn01 (gateway Honduras 1)
         ├── hn02 (gateway Honduras 2 · PC-LCE)
         └── hn03 (gateway Honduras 3)
```

- **Fuente de verdad del gateway activo:** `/etc/mediadev/gateway.conf` (cambiar SOLO con `gateway_switch.sh <id>`; los scripts hacen `source`).
- **Failover:** automático por health score (`health_engine.py`). Ojo: el health check mide alcanzabilidad del m3u8, NO throughput de segmentos.
- Streams geo-restringidos usan SOCKS5; los de CDN global (streamtheworld, etc.) van directos.

---

## 2. Streams activos

El sistema procesa **13 streams** de radio y TV hondureños (10 radio + 3 TV). Los MP3/TS se
graban continuamente y se suben a S3.

| ID | Tipo | Descripción |
|---|---|---|
| `xy_hrn` | Radio | XY HRN |
| `xy_tgu` | Radio | XY TGU |
| `xy_sps` | Radio | XY SPS |
| `radio_satelite` | Radio | Radio Satelite |
| `fm_941` | Radio | 94.1 FM |
| `suave_fm` | Radio | Suave FM |
| `radio_america` | Radio | Radio America |
| `radio_globo` | Radio | Radio Globo |
| `radio_el_patio` | Radio | Radio El Patio |
| `radio_choluteca` | Radio | Radio Choluteca |
| `hch_tv` | TV | HCH TV |
| `teleceiba` | TV | Teleceiba |
| `canal_11` | TV | Canal 11 |

**Fuente de verdad operativa:** `/opt/media-ai/config/stations.json` (13 estaciones `enabled=true`).

**Query para ver catálogo activo en DB:**
```sql
SELECT id, name, type, status
FROM stream_catalog
WHERE status = 'active'
ORDER BY type, name;
```

Los streams de TV graban audio MP3 horario (para fingerprinting) y segmentos `.ts` de video
(para el clip de evidencia). El corte de clip de video se hace off-box uniendo los `.ts` de S3.

---

## 3. Base de Datos — Tablas y Schema

### Modelo de negocio (jerarquía multi-tenant)

PubliAudit se organiza en esta jerarquía. La llave de aislamiento es `tenant_id` — cada tenant ve solo su propio mundo:

```
tenants      (cliente que paga: agencia, central de medios, radio, TV, gobierno)
├── users    (personas del tenant — el JWT lleva tenant_id)
└── clients  (anunciante: Pepsi, Molineros, Sec. de Salud)   — 1:N con tenant
    └── campaigns
        └── advertisements (ads, con fingerprint de referencia)
            └── fingerprint_detections (emisiones detectadas)
                └── report_public_links (evidence portal)
```

> **Rename 13 jun 2026:** la tabla `clients` original (que era el tenant) se renombró a `tenants`, y `client_id` → `tenant_id` en 10 tablas. La palabra "client" ahora significa **anunciante** (tabla `clients` nueva, 1:N con tenant). Ver [Historial de decisiones](#9-historial-de-decisiones-técnicas).

### Tablas principales

| Tabla | Propósito |
|---|---|
| `tenants` | Cliente que paga la plataforma (antes `clients`) — llave de aislamiento |
| `users` | Usuarios del tenant (auth; el JWT lleva `tenant_id`) |
| `clients` | Anunciantes administrados por el tenant (Pepsi, etc.) — 1:N |
| `campaigns` | Campañas (`client_id` = anunciante + `tenant_id`) |
| `advertisements` | Anuncios/cuñas con su fingerprint de referencia |
| `fingerprint_detections` | Detecciones de audio fingerprinting |
| `report_public_links` | Links públicos del evidence portal (token + QR) |
| `stream_catalog` | Catálogo de estaciones (id = slug varchar, ej. `"hch_tv"`) |
| `destroyer_runs` | Historial de corridas del Destroyer |
| `s3_scan_log` | Registro de MP3s/segmentos subidos/escaneados en S3 |
| `gateways`, `stream_assignments` | Red de gateways + asignación stream→gateway |
| `mediadev_*` | Estado operativo de streams (espejo del stream-daemon) |

### Columnas clave

**`fingerprint_detections`:**
```sql
tenant_id        UUID          -- aislamiento (antes client_id)
campaign_id      UUID
ad_id            UUID          -- FK a advertisements
stream_id        VARCHAR       -- slug del stream, ej. "hch_tv" (NO integer)
air_time         TIMESTAMPTZ   -- siempre UTC desde 13 jun 2026
pipeline_version TEXT          -- 'legacy' (pre-cutover) | 'utc_v2' (post-cutover)
confidence_level VARCHAR       -- 'very_high' | 'high' | 'medium' | 'low'
clip_s3_key      VARCHAR       -- ruta del clip (.mp3 audio | .mp4 video TV)
deleted_at       TIMESTAMPTZ   -- soft-delete
```

**Guardas activas de integridad (14 jun 2026):**
```sql
CREATE UNIQUE INDEX ux_fingerprint_detections_source_match_active
ON fingerprint_detections (tenant_id, campaign_id, ad_id, stream_id, s3_key, ts_seconds)
WHERE deleted_at IS NULL;
```

Esta unicidad blinda re-scans del mismo MP3/offset. La limpieza inicial marcó como borradas `95` filas duplicadas activas agrupadas en `58` claves exactas.

**`clients` (anunciante):**
```sql
id         UUID
tenant_id  UUID NOT NULL       -- 1:N con tenants
name       TEXT                -- UNIQUE(tenant_id, name)
industry   TEXT
logo_url   TEXT
active     BOOLEAN
```

**`destroyer_runs`:**
```sql
id               SERIAL
status           TEXT          -- 'deploying'|'running'|'done'|'timeout'|'killed'|'destroyed'
files_done / total_files / total_detections  INTEGER
release_version  TEXT          -- ej: 'destroyer-v22'
last_activity    TIMESTAMPTZ   -- heartbeat por archivo (lo usa el watchdog AWS)
t1_deployed / t2_started / t3_completed / t4_destroyed  TIMESTAMPTZ
```

### Queries útiles

```sql
-- Jerarquía completa: tenant → anunciante → campaña → ads → detecciones
SELECT t.name AS tenant, cl.name AS anunciante, ca.name AS campaign,
       COUNT(DISTINCT a.id) AS ads, COUNT(DISTINCT fd.id) AS detections
FROM tenants t
JOIN campaigns ca ON ca.tenant_id = t.id
JOIN clients cl   ON cl.id = ca.client_id
LEFT JOIN advertisements a          ON a.campaign_id = ca.id AND a.deleted_at IS NULL
LEFT JOIN fingerprint_detections fd ON fd.campaign_id = ca.id AND fd.deleted_at IS NULL
GROUP BY t.name, cl.name, ca.name;

-- Detecciones recientes con nombre del anuncio (ad_id, NO advertisement_id)
SELECT fd.air_time, a.name AS ad, fd.stream_id
FROM fingerprint_detections fd
JOIN advertisements a ON a.id = fd.ad_id
WHERE fd.deleted_at IS NULL
ORDER BY fd.air_time DESC LIMIT 20;

-- Detecciones por pipeline_version
SELECT pipeline_version, COUNT(*), MIN(air_time), MAX(air_time)
FROM fingerprint_detections GROUP BY pipeline_version;
```

---

## 4. El Destroyer (AWS — migrado 14 jun 2026)

### Qué hace

El Destroyer es el motor de detección de audio fingerprinting. Compara los MP3s grabados de
los streams contra un catálogo de anuncios de referencia. Corre **100% serverless en AWS**,
independiente de mediaAPP/mediaCAP, en instancias **EC2 Spot c5.4xlarge** (16 vCPU) efímeras.

### Flujo completo (AWS)

```
EventBridge Rule `destroyer-hourly`  (cron 15 * * * ? *  → :15 UTC; +15min para que lleguen los archivos a S3)
    │
    └── Lambda `destroyer-launcher` (python3.12, 256MB, timeout 120s)
         ├── lista S3, registra archivos pending en PG `s3_scan_log`
         ├── inserta fila en `destroyer_runs` (status='deploying')
         └── lanza EC2 Spot c5.4xlarge desde AMI destroyer-v3
              user_data (cloud-init):
              1. exporta credenciales/env (keys IAM mediadev-s3 horneadas)
              2. aws s3 cp s3://mediadev-recordings/destroyer/releases/{RELEASE}.tar.gz
              3. tar -xzf → /opt/destroyer/
              4. python worker.py   (escanea, detecta, sube clips a S3, escribe detecciones a PG)
              5. al terminar: la instancia se auto-termina (aws ec2 terminate-instances)
```

Boot ~53-96s + scan ~30s. Cron horario ≈ **$6.6/mes** (spot c5.4xlarge ~$0.24/hr).

### Watchdog (AWS)

`Lambda destroyer-watchdog` + EventBridge `destroyer-watchdog-10min` (`rate(10 minutes)`).
Mata instancias `Role=destroyer` **por falta de progreso, no por edad fija** — observa
`destroyer_runs.last_activity` (heartbeat por archivo):
- stall 25 min (running/completing), boot grace 15 min (sin heartbeat aún), hard cap 90 min (240 en backfills grandes), + huérfanas sin run o `status=destroyed` pero vivas.
- Al matar: `terminate-instances` + run→`killed` + re-encola `scanning`→`pending`.

> Cerró el hueco de droplets huérfanos que tenía el watchdog viejo de DigitalOcean.

### Recursos AWS (us-east-1, cuenta `050871635829`)

| Recurso | Valor |
|---|---|
| AMI activa | `ami-065708bbb25ab56ad` (**destroyer-v3-ubuntu22-pydub**) — venv con pydub + ffmpeg 4.4.2 horneados |
| Lambda launcher | `destroyer-launcher` (role `destroyer-lambda-exec`) |
| Lambda watchdog | `destroyer-watchdog` |
| EventBridge | `destroyer-hourly`, `destroyer-watchdog-10min` |
| Security Group | `sg-042fecb04118e4ff9` (puerto 22 CERRADO; el worker sube su log a S3) |
| Key pair | `destroyer-worker` (`.pem` en mediaAPP `/opt/destroyer/` y local — NO en git) |
| IAM worker | user `mediadev-s3` (keys horneadas en user_data para S3/DB/terminate) |

> **Cuenta AWS compartida:** `050871635829` hospeda también Odoo, 3CX (telefonía), un proyecto
> "Shente" y una **"ECS Sample App" abandonada en us-west-2 (Oregon)** gastando ~$66/año por nada
> (candidata a borrar). Tagging adoptado: `Project=MediaAI` + `Component` (destroyer/capture);
> `tag_mediaai.py` aplica sobre lista curada (las 2 Lambdas quedaron sin tag por falta de permiso IAM).

### Sistema de releases S3

El **AMI base es estable** (Ubuntu 22, ffmpeg, Python 3.12, venv con pydub). El **código**
(`worker.py`, `fingerprint.py`) se versiona en S3 y se descarga al arrancar la instancia. Además
está espejado en GitHub `gchiham/destroyer` (`app/` = mediaAPP).

```
s3://mediadev-recordings/destroyer/releases/
├── destroyer-v20.tar.gz
├── destroyer-v21.tar.gz   ← costo real spot (describe_spot_price_history)
├── destroyer-v22.tar.gz   ← producción actual
└── latest.tar.gz
```

**Publicar / activar / rollback:**
```bash
cd /opt/destroyer && ./release.sh v22     # publica a S3 (correr en mediaAPP)
# Activar: editar /opt/destroyer/.env → DESTROYER_RELEASE=destroyer-v22
# Rollback: DESTROYER_RELEASE=destroyer-v21  (1 línea, sin recrear AMI)
```

**Cuándo recrear el AMI base:** solo cuando cambian dependencias del sistema (librería Python,
versión de ffmpeg). Los cambios de código (`worker.py`/`fingerprint.py`) solo requieren `./release.sh`.

### Variables de entorno del Destroyer

En `/opt/destroyer/.env` (NO en git):
```bash
DESTROYER_AMI_ID=ami-065708bbb25ab56ad   # AMI base AWS (reemplazó SNAPSHOT_ID de DO)
DESTROYER_RELEASE=destroyer-v22          # release activa del código
DESTROYER_WORKERS=32                      # workers paralelos
DESTROYER_SCAN_FILE_TIMEOUT=300           # cap por archivo "veneno"
DESTROYER_WTA_WINDOW_SEC=8                # ventana cross-ad para Winner Takes All
DESTROYER_HOURLY_USD=0.25                 # fallback costo spot
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY # IAM mediadev-s3
TG_TOKEN / TG_CHAT                        # Telegram bot
S3_BUCKET=mediadev-recordings
PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASS
MATCH_MIN=...                             # umbral de coincidencia
PIPELINE_VERSION=utc_v2                   # etiqueta grabada en cada detección
# DO_TOKEN ya es opcional/legacy (el worker en EC2 no usa la API de DigitalOcean)
```

### Releases publicadas

| Release | Fecha | Cambios |
|---|---|---|
| `destroyer-v10`–`v16` | 13–14 jun 2026 | Era DigitalOcean: releases S3, dedup + Winner Takes All, idempotencia `ON CONFLICT`, hotfixes de timeout. (Histórico.) |
| `destroyer-v20` | 14 jun 2026 | Primera sobre **EC2**: `DO_TOKEN` opcional, `get_droplet_id()` usa metadata EC2, `self_destruct()` salta API DO. |
| `destroyer-v21` | 14 jun 2026 | Costo real spot vía `describe_spot_price_history`; Telegram "💸 Costo de este deploy" con $/run y $/hr. |
| `destroyer-v22` | 14 jun 2026 | **Release activa.** Fix cosmético del nombre del clip (`name_stem`=filename real en vez del tmp). |

### Lanzamiento manual

```bash
# Disparar la Lambda launcher manualmente (AWS CLI con perfil de la cuenta):
aws lambda invoke --function-name destroyer-launcher /tmp/out.json --region us-east-1
# O editar/relanzar desde mediaAPP /opt/destroyer (launcher_ec2.py es backup manual del código).
```

> El `launcher.py` viejo de DigitalOcean en **mediaCAP** quedó **desmantelado**
> (`launcher.py.do-legacy.DISABLED`, sin exec). Ningún cron lo dispara.

---

## 5. Timezone — UTC Backend / GMT-6 Frontend

### Regla

**Todas las timestamps en la base de datos son UTC.** El frontend convierte a GMT-6 para mostrar al usuario.

Honduras no tiene horario de verano (DST). El offset es siempre fijo: `UTC - 6h`.

### Cutover

| Evento | Valor |
|---|---|
| Fecha/hora del cutover | 13 junio 2026, 16:07 UTC (10:07 AM Honduras) |
| `pipeline_version` pre-cutover | `'legacy'` |
| `pipeline_version` post-cutover | `'utc_v2'` |

Las filas `legacy` en la DB son históricas. Las nuevas detecciones siempre llevarán `utc_v2`.

### Conversión para el frontend

```javascript
// Honduras siempre UTC-6, sin DST
const airTimeUtc = new Date(row.air_time)  // viene de la DB en UTC
const airTimeHN  = new Date(airTimeUtc.getTime() - 6 * 60 * 60 * 1000)
```

---

## 6. Servicios systemd

### mediaCAP (captura) — `159.223.104.91`

| Servicio | Propósito |
|---|---|
| `stream-daemon` | Health, grabación MP3 horario, espejo de estado a PG |
| `supervisor` | 13× ffmpeg (un proceso por stream, `mediadev_streams.conf`) |
| `video-segment-uploader` | Sube `.ts` de TV (y MP3 horario) a S3 |
| `mediadev-gateway-api` | API de heartbeats de gateways |
| `mediadev-health-engine` | Health score de gateways + failover |
| `mediadev-monitor` | Vigila WireGuard, alertas Telegram |
| `nginx`, `privoxy`, `wg-quick@wg0` | serving HLS / proxy / VPN |
| `dashboard_mediadev` | **inactivo** (no corre dashboard) |

### mediaAPP (app/control) — `137.184.53.234`

| Servicio | Propósito |
|---|---|
| `media-app` | API REST del producto PubliAudit (FastAPI) + evidence portal |
| `chihambot` | Bot de Telegram (alertas) |
| `nginx` | reverse proxy (sites `default`, `media-app`) |

> La orquestación del Destroyer NO es un servicio systemd ni un cron local — corre en AWS (§4).

**Comandos útiles:**
```bash
systemctl status stream-daemon
journalctl -u stream-daemon -n 100 --no-pager
supervisorctl status      # 13 procesos ffmpeg (mediaCAP)
```

### Credenciales (fuera del repo, en `/etc/*.env`)

Cargadas por systemd con `EnvironmentFile=` (no `Environment=` inline, que se expone vía
`systemctl show`). Permisos `chmod 600`.

| Archivo | Nodo | Contenido |
|---|---|---|
| `/etc/mediadev-db.env` | ambos | credenciales PostgreSQL |
| `/etc/mediadev-s3.env` | mediaCAP | credenciales S3 |
| `/etc/media-app.env` | mediaAPP | `PG_*`, `JWT_SECRET`, `AWS_*`, `S3_*`, `PUBLIC_BASE_URL`, `CORS_ORIGINS` |
| `/opt/destroyer/.env` | ambos | config Destroyer (DB, AWS, Telegram) |

---

## 7. MCP Server (mediadev-mcp)

Permite a Claude Code / Codex consultar el estado del ecosistema en tiempo real. **Corre en
AMBOS nodos** (14 jun 2026): un MCP en mediaCAP (observa captura) y otro en mediaAPP (observa
media-app + Destroyer). Los tools de DB/cloud dan datos globales; los de host
(`get_service_logs`, `get_host_resources`, etc.) son **por-nodo**.

### En el servidor
```
mediaCAP: /opt/media-ai/mcp/server.py     (FastMCP, stdio) + venv     → repo MediaDEV-Honduras
mediaAPP: /opt/media-ai/mcp/server.py + tools/{system,workers,queue,
          health,errors,logs,cost,capacity}.py                        → repo mediadev-infra
```

### Herramientas mediaCAP (~13, solo lectura)
*Observabilidad:* `get_system_status` · `get_workers` · `get_queue_stats` · `get_service_health` · `get_recent_errors`
*Diagnóstico:* `get_service_logs(service,lines,contains)` · `get_error_digest(hours)`
*Escalado y costo:* `get_host_resources` · `get_stream_bandwidth` · `get_destroyer_analytics(limit)` · `get_droplets` · `get_disk_usage` · `get_uploader_status`

> **Por qué estos tools:** cada decisión de operación/escalado requería cavar datos a mano
> (journal, top/ps/df, bitrate de segmentos, `destroyer_runs`, API cloud). Estos tools los
> exponen para que la IA diagnostique sin escarbar — ej. `get_destroyer_analytics` marca el
> patrón de cuelgue del worker, `get_stream_bandwidth` muestra el cuello al escalar TV (red/disco/S3, no CPU).

### Desde Windows (Claude Code + Codex)

**Wrappers locales** (uno por nodo): `mediadev` → `C:\Users\Sedesol\.ssh\mediadev-mcp.py`
(mediaCAP), `mediadev-app` → `C:\Users\Sedesol\.ssh\mediadev-app-mcp.py` (mediaAPP). El wrapper
hace SSH al nodo y proxea stdin/stdout del protocolo MCP sin modificar.

```python
# Flags críticos para que el protocolo MCP no se corrompa:
"-T"                         # no pseudo-tty (stdio limpio)
"-o", "LogLevel=QUIET"       # silencia banners SSH
stderr=subprocess.DEVNULL    # SSH stderr no contamina JSON-RPC
```

**Test manual:**
```powershell
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python "C:\Users\Sedesol\.ssh\mediadev-mcp.py"
```

---

## 8. Archivos y rutas clave

### mediaCAP (159.223.104.91)
```
/opt/media-ai/                 ← repo git MediaDEV-Honduras (working tree real)
├── daemon/stream_daemon.py    ← health + grabación MP3 + espejo a PG
├── scripts/stream_*.sh        ← un ffmpeg por stream (SOCKS5 o directo)
├── scripts/video_segment_uploader.py
├── monitor/monitor.py         ← vigila WireGuard, alertas Telegram
├── mcp/server.py              ← MCP captura
├── dashboard/dashboard_v4.py  ← referencia histórica (NO corre)
└── config/stations.json       ← 13 estaciones
/opt/destroyer/gateway/engine/{gateway_api,health_engine}.py  ← gateway engine (repo destroyer/cap)
/opt/destroyer/launcher.py.do-legacy.DISABLED                 ← launcher DO viejo (desmantelado)
/etc/mediadev-db.env, /etc/mediadev-s3.env, /etc/mediadev/gateway.conf
/etc/wireguard/wg0.conf        ← llaves (NO en git)
```

### mediaAPP (137.184.53.234)
```
/opt/media-app/main.py         ← FastAPI PubliAudit (repo media-app) + cobertura_static/
/opt/destroyer/                ← repo destroyer/app/
├── launcher_ec2.py            ← backup manual del launcher EC2
├── worker.py                  ← procesa MP3s (corre en la instancia Spot)
├── fingerprint.py             ← audio fingerprinting
├── lambda_function.py         ← código de la Lambda destroyer-launcher
├── watchdog_function.py       ← código de la Lambda destroyer-watchdog
├── release.sh                 ← publica release a S3
├── .env                       ← config (NO en git)
└── destroyer-worker.pem       ← llave AWS (NO en git)
/opt/chihambot/bot.py          ← bot Telegram (repo mediadev-infra)
/opt/media-ai/mcp/             ← MCP app (repo mediadev-infra)
/etc/media-app.env, /etc/mediadev-db.env
```

### Windows (local)
```
C:\GusHD\CloudAWS\             ← working directory de auditoría (NO es repo git)
├── live_mediaDEV.md           ← este archivo (copia; el repo es gchiham/MediaDEV-Honduras)
├── lambda_function.py, launcher_ec2.py, worker_*.py  ← fuentes Destroyer editadas
C:\GusHD\destroyer_sync\       ← clon git de gchiham/destroyer
C:\GusHD\infra_sync\           ← clon git de gchiham/mediadev-infra
C:\Users\Sedesol\.ssh\keySED   ← SSH key de los nodos DO
C:\Users\Sedesol\.ssh\destroyer-worker.pem  ← SSH key del worker EC2
```

---

## 9. Historial de decisiones técnicas

### Destroyer migrado de DigitalOcean a AWS EC2 Spot (14 jun 2026)

**Decisión:** mover el Destroyer de droplets DigitalOcean c-16 efímeros a **AWS EC2 Spot
c5.4xlarge**, 100% serverless (EventBridge → Lambda launcher → Spot → auto-terminate), con un
Lambda watchdog que mata instancias estancadas por heartbeat (`last_activity`).

**Por qué:** spot c5.4xlarge cuesta ~$0.24/hr vs el c-16 de DO; el cron horario sale ≈$6.6/mes.
El watchdog por progreso (no por edad) cierra el hueco de instancias huérfanas. AMI base estable
+ releases S3 = deploy/rollback instantáneo. El launcher viejo de DO en mediaCAP quedó desmantelado.

**Gotchas:** el user IAM `mediadev-s3` no tiene `lambda:GetFunctionConfiguration` (los re-deploys
de Lambda evitan el waiter `function_updated`); el server no tiene `zip` (se empaqueta con `zipfile`
de Python). Bug pydub resuelto horneando pydub en el AMI v3.

---

### Versionado completo en GitHub (15 jun 2026)

**Decisión:** espejar todo el sistema en 4 repos GitHub (`MediaDEV-Honduras`, `media-app`,
`destroyer`, `mediadev-infra`) con la regla "la verdad es lo desplegado". Secretos nunca en git
(`.env`, `.pem`, llaves WireGuard redactadas). Ver §0 y `mediadev-infra/INVENTORY.md`.

**Por qué:** antes solo mediaCAP `/opt/media-ai` estaba versionado; media-app, destroyer y toda
la config operativa (systemd, supervisor, nginx, wireguard) vivían solo en disco — riesgo total
si se pierde un droplet. Ahora el developer tiene todo lo que opera.

---

### Deduplicación fuerte en DB + Winner Takes All (14 jun 2026)

**Decisión:** separar dos capas de control: (1) aplicación (worker/fingerprint) colapsa offsets
parciales, deduplica por ventana real y aplica Winner Takes All entre anuncios que compiten en el
mismo instante; (2) DB con índice único parcial sobre `(tenant_id, campaign_id, ad_id, stream_id,
s3_key, ts_seconds)` para idempotencia ante re-scans.

**Ejecución real:** 14 jun 2026 — `58` grupos duplicados corregidos, `95` filas extra desactivadas, `0` duplicados activos remanentes.

---

### Hardening de reconexión en mediaCAP (14 jun 2026)

**Decisión:** endurecer la captura con (1) flags de reconexión de ffmpeg en `stream_run.sh` —
diferenciados: para HLS de ventana corta (`*.m3u8`) se usa `-reconnect -reconnect_streamed
-reconnect_on_network_error` SIN `reconnect_at_eof`/`reconnect_on_http_error` (que causaban
crash-loop en Teleceiba), y para Icecast continuo el comportamiento viejo; (2) ventana de gracia
de `35s` en stream-daemon tras un restart antes de re-incrementar `cb_fails`.

**Por qué:** microcortes en `radio_el_patio`/`teleceiba` y un crash-loop de Teleceiba por gateway
sin throughput + flags HLS dañinos. El nodo no estaba saturado; era resiliencia y timing.

---

### Modelo tenant → client(anunciante) → campaign → ad (13 jun 2026)

**Decisión:** jerarquía multi-tenant con `tenant` = cliente que paga y `client` = anunciante,
relación 1:N. Se renombró `clients`→`tenants` y `client_id`→`tenant_id` en 10 tablas; nueva
`clients` = anunciante con FK `tenant_id`. Aislamiento por `tenant_id` (en el JWT), Auth intacto.
Sin módulo de "pauta esperada vs real" — solo se publica lo **detectado**.

---

### UTC en backend, GMT-6 solo en display (13 jun 2026)

**Decisión:** timestamps en UTC; el frontend aplica `-6h`. Honduras sin DST. Cutover 13 jun 16:07
UTC; filas previas `pipeline_version='legacy'`, nuevas `'utc_v2'`.

---

### EnvironmentFile en lugar de Environment= en systemd (13 jun 2026)

**Decisión:** credenciales en `/etc/*.env` (chmod 600) vía `EnvironmentFile=`, no inline, porque
`systemctl show` expone los `Environment=` en texto plano.

---

### libx264 ultrafast vs -c:v copy para clips TV (anterior a 13 jun 2026)

**Decisión:** los clips de video TV se re-encodean con `libx264 -preset ultrafast` (no `-c:v copy`)
para corte exacto. Con copy, el corte caía en un keyframe distinto al solicitado y el audio
desincronizaba. Nota: este re-encode ocurre **off-box** al armar el clip de evidencia, no en mediaCAP.

---

## 10. Cómo actualizar este documento

**Actualizar `live_mediaDEV.md` cuando cambie cualquiera de:** arquitectura de infraestructura ·
tablas/schema de la DB · flujo del Destroyer o releases · servicios systemd · variables de entorno
críticas · decisiones de diseño con impacto en el sistema.

**No actualizar por:** cambios de código internos sin impacto en arquitectura · fixes de bugs sin
cambio visible · config trivial.

**Proceso:**
1. Hacer el cambio en el sistema
2. Editar la sección correspondiente en este archivo
3. Actualizar la fecha del header
4. Commit y push al repo `gchiham/MediaDEV-Honduras` (y `git pull` en `/opt/media-ai`)

---

*MediaDEV · mediaCAP 159.223.104.91 + mediaAPP 137.184.53.234 · DigitalOcean nyc1 + Destroyer en AWS us-east-1 · Actualizado: 15 junio 2026*
