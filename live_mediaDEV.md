# live_mediaDEV.md — Ecosistema MediaDEV: Referencia Viva

**Última actualización:** 14 junio 2026  
**Versión del documento:** 1.2  
**Servidor:** 159.223.104.91  
**Mantenido por:** equipo MediaDEV — actualizar cada vez que cambie arquitectura, schema, servicios, o decisiones de diseño

> Este documento es la fuente de verdad del ecosistema MediaDEV. Cualquier dev o AI que lo lea debe poder entender el sistema completo sin necesidad de preguntas adicionales. Vive en el repo de GitHub.

---

## 1. Infraestructura

### Servidor principal — mediaDEV

| Campo | Valor |
|---|---|
| IP pública | `159.223.104.91` |
| Proveedor | DigitalOcean, región `nyc1` |
| Tamaño | 2 vCPU / 4 GB RAM |
| OS | Ubuntu 24.04.4 LTS |
| Acceso | `ssh -i ~/.ssh/keySED root@159.223.104.91` |

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
| Rutas clave | `{stream_id}/YYYY/MM/*.mp3` (MP3s horarios), `video_segments/` (TS TV), `clips/` (evidencia), `destroyer/releases/` (código Destroyer) |

### VPN y Gateways Honduras

```
mediaDEV (nyc1)
    │
    └── WireGuard VPN
         ├── hn01 (gateway Honduras 1)
         ├── hn02 (gateway Honduras 2) ← primary (PC-LCE)
         └── hn03 (gateway Honduras 3)
```

- **Gateway primario:** PC-LCE vía `hn02`
- **Failover:** automático si el gateway primario cae
- **Propósito:** captura de streams de radio/TV hondureños via gateways locales

### Droplet Destroyer (efímero)

| Campo | Valor |
|---|---|
| Tipo | DigitalOcean c-16 |
| CPU | 16 vCPU (CPU-optimized) |
| Vida útil | efímero — se destruye al terminar la corrida |
| Snapshot base | ID `232701378` (`destroyer-base-v9`) |
| Código | descargado desde S3 al arrancar (ver sección 4) |

---

## 2. Streams activos

El sistema procesa 12 streams de radio y TV hondureños. Los MP3/TS se graban continuamente y se suben a S3.

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

**Fuente de verdad operativa:** `/opt/media-ai/config/stations.json` (12 estaciones `enabled=true`).

**Query para ver catálogo activo en DB:**
```sql
SELECT id, name, type, status
FROM stream_catalog
WHERE status = 'active'
ORDER BY type, name;
```

Los streams de TV generan clips de video (libx264 ultrafast) además del audio.

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
| `stream_catalog` | Catálogo de ~193 estaciones (id = slug varchar, ej. `"hch_tv"`) |
| `destroyer_runs` | Historial de corridas del Destroyer |
| `s3_scan_log` | Registro de MP3s/segmentos subidos a S3 |
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
status           TEXT          -- 'deploying'|'running'|'done'|'timeout'|'destroyed'
files_done / total_files / total_detections  INTEGER
release_version  TEXT          -- ej: 'destroyer-v11'
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

## 4. El Destroyer

### Qué hace

El Destroyer es el motor de detección de audio fingerprinting. Compara los MP3s grabados de los streams contra un catálogo de anuncios de referencia. Corre en un droplet c-16 (16 vCPU) para procesar en paralelo rápido.

### Flujo completo

```
Cron (0 0,6,12,18 * * *)
    │
    └── launcher.py (en mediaDEV)
         │  Lee .env: SNAPSHOT_ID, DESTROYER_RELEASE, DESTROYER_WORKERS
         │
         ├── Crea droplet c-16 desde snapshot base (ID: 232701378)
         │
         ├── Inserta fila en destroyer_runs (status='deploying')
         │
         └── cloud-init ejecuta en el droplet:
              1. Exporta todas las credenciales/env vars
              2. aws s3 cp s3://mediadev-recordings/destroyer/releases/{DESTROYER_RELEASE}.tar.gz /tmp/release.tar.gz
              3. tar -xzf /tmp/release.tar.gz -C /opt/destroyer/
              4. python worker.py
              5. Al terminar: droplet se auto-destruye
```

El Destroyer toma ~70-90s para provisionar (DigitalOcean) + <1s para descargar el código de S3.

### Watchdog

`/opt/destroyer/watchdog.py` corre en mediaDEV en paralelo al droplet. Monitorea la fila activa en `destroyer_runs`. Si detecta que `files_done` no avanza por más de 20 minutos:
1. Envía alerta a Telegram
2. Destruye el droplet vía API de DigitalOcean
3. Actualiza `status = 'timeout'` en la DB

### Sistema de releases S3 (implementado 13 jun 2026)

**El snapshot base es estable.** Contiene: Ubuntu 24.04, ffmpeg, Python 3.12, venv con todas las librerías del Destroyer. Raramente cambia.

**El código está en S3, versionado:**
```
s3://mediadev-recordings/
└── destroyer/releases/
    ├── destroyer-v10.tar.gz
    ├── destroyer-v11.tar.gz
    ├── destroyer-v14.tar.gz
    ├── destroyer-v15.tar.gz    ← producción actual (worker.py + fingerprint.py)
    └── latest.tar.gz           ← siempre = la última publicada
```

**Publicar nueva release:**
```bash
# En el servidor mediaDEV:
cd /opt/destroyer
./release.sh v11
```

**Activar la nueva release:**
```bash
# Editar /opt/destroyer/.env:
DESTROYER_RELEASE=destroyer-v15
```
El próximo cron (o lanzamiento manual) usará el nuevo código.

**Rollback instantáneo:**
```bash
# Sin recrear snapshots:
DESTROYER_RELEASE=destroyer-v14   # en .env
```

**Cuándo recrear el snapshot base:**

| Cambio | ¿Nuevo snapshot? |
|---|---|
| Fix en `worker.py` o `fingerprint.py` | No — solo `./release.sh vX` |
| Nueva librería Python en el venv | Sí |
| Actualización de ffmpeg | Sí |
| Nuevo archivo `.py` en el Destroyer | No — agregar al tar.gz |

### Variables de entorno del Destroyer

En `/opt/destroyer/.env`:
```bash
SNAPSHOT_ID=232701378              # ID snapshot base en DigitalOcean
DESTROYER_RELEASE=destroyer-v15    # Release activa del código
DESTROYER_WORKERS=32               # Workers paralelos (default: 32)
DESTROYER_SCAN_FILE_TIMEOUT=240    # Cap por archivo "veneno" antes de marcar error
DESTROYER_WTA_WINDOW_SEC=8         # Ventana cross-ad para Winner Takes All
DO_TOKEN=...                    # DigitalOcean API token
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
TG_TOKEN=...                    # Telegram bot
TG_CHAT=...
S3_BUCKET=mediadev-recordings
PG_HOST=private-media-db-...
PG_PORT=25060
PG_DB=destroyer_db
PG_USER=destroyer
PG_PASS=...
MATCH_MIN=...                   # umbral mínimo de coincidencia para detección
PIPELINE_VERSION=utc_v2         # etiqueta que se graba en cada detección
```

### Releases publicadas

| Release | Fecha | Cambios |
|---|---|---|
| `destroyer-v10` | 13 jun 2026 | Primera release S3. Fix libx264 ultrafast para clips TV. Migración UTC (pipeline_version=utc_v2). |
| `destroyer-v11` | 13 jun 2026 | Launcher con releases S3 en producción. |
| `destroyer-v14` | 14 jun 2026 | Colapso de offsets vecinos en `fingerprint.py`, dedup por ventana real, Winner Takes All por instante, logs de debug a S3 y timeout por archivo configurable. |
| `destroyer-v15` | 14 jun 2026 | Inserción idempotente en DB con `ON CONFLICT DO NOTHING`, no genera clip/Telegram si la detección ya existe, y queda alineada con la migración de deduplicación en `fingerprint_detections`. Release activa actual. |

### Lanzamiento manual

```bash
# Desde mediaDEV:
cd /opt/destroyer
source .env
python3 launcher.py
```

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

## 6. Servicios systemd en mediaDEV

| Servicio | Propósito |
|---|---|
| `stream-daemon` | Graba streams de radio/TV y sube MP3s/segmentos a S3 |
| `nginx` | Reverse proxy para publiaudit-api y gateway-api |
| `publiaudit-api` | API REST para el producto PubliAudit (FastAPI) |
| `gateway-api` | API para gestión de gateways WireGuard |
| `health-engine` | Monitoreo de salud de streams y alertas Telegram |
| `video-segment-uploader` | Sube segmentos de video TV a S3 |
| `medio-orchestrator` | Orquestador de tareas periódicas del ecosistema |
| `privoxy` | Proxy HTTP (usado por gateways) |
| `wireguard` | VPN hacia gateways Honduras |

**Comandos útiles:**
```bash
systemctl status publiaudit-api
journalctl -u stream-daemon -n 100 --no-pager
systemctl restart publiaudit-api
```

### Credenciales de publiaudit-api

Las credenciales están en `/etc/publiaudit-api.env` (chmod 600, root:root). NO están en el `.service` para evitar exposición vía `systemctl show`.

```ini
# /etc/systemd/system/publiaudit-api.service
[Service]
EnvironmentFile=/etc/publiaudit-api.env   ← así, no como Environment= inline
```

Variables en `/etc/publiaudit-api.env`:
```bash
PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASS
JWT_SECRET, JWT_EXP_HOURS
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
S3_BUCKET, S3_REGION, PUBLIC_BASE_URL
CORS_ORIGINS    # opcional: ej. https://app.publiaudit.com,https://admin.publiaudit.com
                # si no se define, el default es '*'
```

---

## 7. MCP Server (mediadev-mcp)

Permite a Claude Code consultar el estado del ecosistema MediaDEV en tiempo real.

### En el servidor

```
/opt/media-ai/mcp/
├── server.py       ← FastMCP, transport="stdio"
└── venv/           ← Python venv con mcp[server]
```

**Herramientas disponibles (11, solo lectura):**

*Observabilidad:*
- `get_system_status` — estado de los 12 streams
- `get_workers` — procesos ffmpeg + servicios systemd
- `get_queue_stats` — motor Destroyer: corridas, detecciones, costos
- `get_service_health` — gateways, VPN, DB, proxies
- `get_recent_errors` — eventos de stream (DOWN/UP/CB) + failovers de gateway + runs con error

*Diagnóstico de errores (v1.1):*
- `get_service_logs(service, lines, contains)` — tail/grep del journal de un servicio (incluye tracebacks de Python). Allowlist de 9 servicios
- `get_error_digest(hours)` — escaneo consolidado de errores/tracebacks. Una llamada para "¿qué se está rompiendo?"

*Decisiones de escalado y costo (v1.2):*
- `get_host_resources` — CPU/RAM/disco/load + agregado ffmpeg + veredicto. ¿Hay headroom? ¿cuándo desacoplar captura?
- `get_stream_bandwidth` — bitrate (Mbps) por stream + GB/día. El cuello al escalar TV es red/disco/S3, no CPU
- `get_destroyer_analytics(limit)` — boot/work/costo por corrida + **detección automática de cuelgues** (timeout con 0 detecciones)
- `get_droplets` — inventario DO + **caza de droplets Destroyer huérfanos** (money-leaks)

> **Por qué estos tools (v1.1–v1.2, 13 jun 2026):** cada decisión de operación/escalado requería cavar datos a mano (journal, top/ps/df, bitrate de segmentos, `destroyer_runs`, API de DO). Estos tools los exponen para que la IA diagnostique y decida sin escarbar — ej. `get_droplets` caza droplets c-16 huérfanos que cuestan ~$0.95/h, y `get_destroyer_analytics` marca solo el patrón de cuelgue del worker.

### Desde Windows (Claude Code desktop)

**Wrapper local:** `C:\Users\Sedesol\.ssh\mediadev-mcp.py`

El wrapper hace SSH al servidor y proxea stdin/stdout del protocolo MCP sin modificaciones.

```python
# Flags críticos para que el protocolo MCP no se corrompa:
"-T"                    # no pseudo-tty (stdio limpio)
"-o", "LogLevel=QUIET"  # silencia banners SSH
stderr=subprocess.DEVNULL  # SSH stderr no contamina JSON-RPC
```

**Config en Claude Code desktop:**
`C:\Users\Sedesol\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "mediadev": {
      "command": "C:\\Users\\Sedesol\\AppData\\Local\\Programs\\Python\\Python314\\python.exe",
      "args": ["C:\\Users\\Sedesol\\.ssh\\mediadev-mcp.py"]
    }
  }
}
```

**Test manual (verifica que el MCP funciona):**
```powershell
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python "C:\Users\Sedesol\.ssh\mediadev-mcp.py"
# Respuesta esperada: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"mediadev",...}}}
```

---

## 8. Archivos y rutas clave

### En el servidor (159.223.104.91)

```
/opt/destroyer/
├── launcher.py         ← orquestador principal: crea droplet, inyecta cloud-init
├── worker.py           ← procesamiento de MP3s (corre en el droplet c-16)
├── fingerprint.py      ← lógica de audio fingerprinting
├── watchdog.py         ← mata el Destroyer si se cuelga >20 min
├── release.sh          ← publica nueva release a S3 (./release.sh vX)
├── .env                ← todas las variables de entorno del Destroyer
└── logs/
    ├── cloud-init.log  ← log del script que corre en el droplet al arrancar
    └── worker.log      ← stdout/stderr del worker.py en el droplet

/opt/media-ai/mcp/
├── server.py           ← servidor MCP (FastMCP, stdio)
└── venv/

/opt/publiaudit-api/
├── main.py             ← FastAPI app de PubliAudit
└── ...

/etc/publiaudit-api.env     ← credenciales publiaudit-api (chmod 600)
/etc/systemd/system/        ← definiciones de servicios
```

### En Windows (local)

```
C:\GusHD\CloudAWS\              ← working directory principal
├── live_mediaDEV.md            ← este archivo
├── MEDIADEV_MEJORAS_RESULTADOS.md
├── MEDIADEV_DESTROYER_RELEASES.md
└── patch_launcher_release.py   ← script de utilidad (referencia histórica)

C:\Users\Sedesol\.ssh\
├── keySED                      ← SSH key para mediaDEV
└── mediadev-mcp.py             ← wrapper MCP para Claude Code

C:\Users\Sedesol\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\
└── LocalCache\Roaming\Claude\claude_desktop_config.json  ← config MCP
```

---

## 9. Historial de decisiones técnicas

### S3 releases en lugar de snapshot por código (13 jun 2026)

**Decisión:** El snapshot del Destroyer solo tiene dependencias del sistema (Ubuntu, ffmpeg, Python, venv). El código (`worker.py`, `fingerprint.py`) se versiona en S3 y se descarga al arrancar el droplet.

**Por qué:** Crear un nuevo snapshot tardaba ~10 minutos y no había auditoría de qué código corrió en cada run. Con S3 releases: deploy instantáneo, rollback en 1 línea, columna `release_version` en DB para auditoría completa.

**Cuándo usar snapshot nuevo:** solo cuando cambian dependencias del sistema (librería Python, versión de ffmpeg). Los cambios de código no requieren snapshot.

---

### Deduplicación fuerte en DB + Winner Takes All (14 jun 2026)

**Decisión:** separar dos capas de control:

1. **Aplicación (worker/fingerprint):** colapsa offsets parciales del mismo anuncio, deduplica por ventana real según duración y aplica Winner Takes All entre anuncios que compiten en el mismo instante.
2. **Base de datos:** índice único parcial sobre `(tenant_id, campaign_id, ad_id, stream_id, s3_key, ts_seconds)` para que un re-scan del mismo archivo no duplique filas aunque el worker reintente.

**Por qué:** el ruido observado venía de dos fuentes distintas: detecciones múltiples dentro de una sola emisión y copias exactas causadas por re-scans/reintentos. La capa de aplicación reduce ruido semántico; la unicidad en DB blinda la idempotencia.

**Ejecución real:** el 14 jun 2026 se aplicó la migración de dedup en la DB administrada. Resultado: `58` grupos duplicados activos corregidos, `95` filas extra desactivadas, y `0` duplicados activos remanentes tras la verificación.

---

### Modelo tenant → client(anunciante) → campaign → ad (13 jun 2026)

**Decisión:** Jerarquía multi-tenant con `tenant` como cliente que paga (agencia/central/radio/TV/gobierno) y `client` como anunciante (Pepsi, Molineros). Relación tenant↔anunciante **1:N** (cada tenant maneja su propia cartera). Se renombró la tabla `clients` original → `tenants` y `client_id` → `tenant_id` en 10 tablas; se creó `clients` nueva = anunciante con FK `tenant_id`.

**Por qué 1:N y no M2M:** aunque un anunciante fuera compartido entre agencias, las campañas/ads/detecciones quedan aisladas por tenant de todas formas — el M2M solo agregaría complejidad para compartir lo cosmético. La llave de aislamiento sigue siendo `tenant_id` a nivel aplicativo (el JWT lo lleva), por eso no se tocó Auth.

**Sin "pauta esperada vs real":** el sistema solo publica lo **detectado**. Se descartó el módulo de `placements`.

**API:** `publiaudit-api` expone CRUD de anunciantes (`/api/clients`), campañas con `client_name` + filtro `?client_id=`, `PATCH /api/campaigns/{id}` para reasignar anunciante. El evidence portal muestra el anunciante (`advertiser_name`) con el tenant como `provider_name`.

---

### UTC en backend, GMT-6 solo en display (13 jun 2026)

**Decisión:** Todas las timestamps se almacenan y calculan en UTC. El frontend aplica `-6h` para mostrar hora Honduras.

**Por qué:** Honduras no tiene DST (offset siempre fijo), pero almacenar en UTC es la práctica correcta para evitar ambigüedades y facilitar queries cross-timezone en el futuro. Cutover ejecutado el 13 jun 2026 a las 16:07 UTC. Filas anteriores tienen `pipeline_version='legacy'`; las nuevas tienen `'utc_v2'`.

---

### EnvironmentFile en lugar de Environment= en systemd (13 jun 2026)

**Decisión:** Las credenciales de `publiaudit-api` están en `/etc/publiaudit-api.env` (chmod 600) en lugar de inline como `Environment=` en el `.service`.

**Por qué:** `systemctl show publiaudit-api` expone en texto plano todas las variables `Environment=`. Con `EnvironmentFile=` el archivo tiene permisos restringidos y `systemctl show` solo muestra la ruta del archivo, no el contenido.

---

### libx264 ultrafast en lugar de -c:v copy para clips TV (fecha: anterior a 13 jun 2026)

**Decisión:** Los clips de video de streams TV se re-encodean con `libx264 -preset ultrafast` en lugar de hacer `-c:v copy` (copia directa del stream).

**Por qué:** Con `-c:v copy`, los clips de TV tenían un offset de timing (el audio no sincronizaba con el video) porque el punto de corte caía en un keyframe diferente al solicitado. Re-encodear con ultrafast fuerza el corte exacto sin sacrificar velocidad perceptiblemente.

---

## 10. Cómo actualizar este documento

**Actualizar `live_mediaDEV.md` cuando cambie cualquiera de:**

- Arquitectura de infraestructura (nuevo servidor, cambio de proveedor, nuevo servicio)
- Tablas o schema de la DB (nueva tabla, columna nueva, cambio de tipo)
- Flujo del Destroyer o sistema de releases (nueva release publicada, cambio en el proceso)
- Servicios systemd (nuevo servicio, renombrado, cambio de configuración relevante)
- Variables de entorno críticas (nueva variable, cambio de propósito)
- Decisiones de diseño con impacto en el sistema

**No actualizar por:**
- Cambios de código internos sin impacto en arquitectura
- Fixes de bugs sin cambio de comportamiento visible externamente
- Cambios de configuración triviales

**Proceso:**
1. Hacer el cambio en el sistema
2. Editar la sección correspondiente en este archivo
3. Actualizar la fecha en el header: `Última actualización: DD mes AAAA`
4. Commit y push al repo de GitHub

---

*MediaDEV · 159.223.104.91 · DigitalOcean nyc1 · Actualizado: 14 junio 2026*
