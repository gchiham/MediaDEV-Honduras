# MediaDEV — Migración a UTC (Backend)

**Objetivo:** Backend 100% UTC como fuente de verdad para timestamps absolutos. Frontend/presentación sigue en GMT-6.  
**Downtime estimado:** 5-15 minutos para el cutover de escritura nueva.  
**Preparación previa:** 1-2 horas de trabajo sin tocar producción.  
**Honduras:** no tiene horario de verano — GMT-6 siempre, offset fijo `+6 hours`.

> **Principio operativo:** no hacer un backfill masivo ciego durante el cutover. Primero se normaliza **todo lo nuevo** a UTC; el histórico solo se migra después si su semántica quedó demostrada.

---

## Tabla de Contenidos

1. [Diagnóstico previo](#1-diagnóstico-previo)
2. [Preparación sin downtime](#2-preparación-sin-downtime)
3. [Cutover — 5 minutos](#3-cutover--5-minutos)
4. [Cambios de código](#4-cambios-de-código)
5. [Validación post-cutover](#5-validación-post-cutover)
6. [Regla de oro para frontend](#6-regla-de-oro-para-frontend)
7. [Rollback](#7-rollback)

---

## 1. Diagnóstico Previo

Antes de tocar nada, verificar el estado actual. Ejecutar en la DB:

```sql
-- 1. Ver tipo de columna air_time
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'fingerprint_detections'
  AND column_name IN ('air_time', 'ts_seconds');

-- 2. Ver si hay timezone info en los valores actuales
SELECT 
    air_time,
    air_time AT TIME ZONE 'America/Tegucigalpa' AS hn_time,
    air_time AT TIME ZONE 'UTC' AS utc_time
FROM fingerprint_detections
ORDER BY air_time DESC
LIMIT 5;

-- 3. Contar registros históricos a migrar
SELECT COUNT(*) AS total_rows,
       MIN(air_time) AS oldest,
       MAX(air_time) AS newest
FROM fingerprint_detections;

-- 4. Ver tipo de columnas en otras tablas con timestamps
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND data_type IN ('timestamp without time zone', 'timestamp with time zone', 'bigint')
  AND (
    column_name ILIKE '%time%' OR
    column_name ILIKE '%ts%' OR
    column_name ILIKE '%at'
  )
ORDER BY table_name, column_name;
```

### Interpretación del resultado

| Caso | Qué significa | Acción necesaria |
|---|---|---|
| `data_type = 'timestamp with time zone'` y los valores muestran offset `-06` | Guardado con timezone HN — PostgreSQL ya lo convierte a UTC internamente | Solo verificar que las inserciones futuras usen `now()` o UTC explícito |
| `data_type = 'timestamp without time zone'` y los valores son la hora HN sin zone | Naive timestamp en HN — candidato a backfill `+6h`, pero solo después de validar muestra real | No ejecutar UPDATE ciego |
| `data_type = 'bigint'` | Unix epoch — ya es UTC por definición | Sin acción necesaria |

---

## 2. Preparación Sin Downtime

Todo esto se hace **antes** del cutover, sin detener servicios.

### 2.1 Snapshot de seguridad de la DB

```bash
# Desde la máquina MediaDEV o desde tu local con acceso a la DB
set -a
source /etc/mediadev-db.env
set +a

pg_dump \
  -h "$PG_HOST" \
  -p "${PG_PORT:-25060}" \
  -U "${PG_USER:-destroyer}" \
  -d "${PG_DB:-destroyer_db}" \
  --table=fingerprint_detections \
  --table=mediadev_stream_status \
  --table=mediadev_events \
  --table=destroyer_runs \
  -f /root/backup_pre_utc_$(date +%Y%m%d_%H%M%S).sql

echo "Backup listo"
```

### 2.2 Definir baseline de migración (recomendado)

Antes del cutover, dejar documentado el punto exacto a partir del cual el pipeline ya será UTC:

```sql
ALTER TABLE fingerprint_detections
ADD COLUMN IF NOT EXISTS pipeline_version VARCHAR(16) DEFAULT 'legacy';

ALTER TABLE s3_scan_log
ADD COLUMN IF NOT EXISTS pipeline_version VARCHAR(16) DEFAULT 'legacy';
```

Durante el cutover se marcarán los procesos nuevos como `utc_v2`.

### 2.3 Preparar backfill de histórico (solo si aplica, no para el cutover)

Guardar este script como `/root/migrate_utc.sql`:

```sql
-- /root/migrate_utc.sql
-- NO ejecutar durante el cutover inicial.
-- Honduras = UTC-6 siempre (sin DST)
-- Si air_time era naive timestamp en HN → sumar 6 horas para obtener UTC real
-- Ejecutar solo después de validar una muestra real contra S3/logs/Telegram.

BEGIN;

-- Verificar una muestra antes de cambiar
SELECT 'ANTES' as check_point, air_time, 
       air_time + interval '6 hours' as air_time_utc_preview
FROM fingerprint_detections 
ORDER BY air_time DESC LIMIT 3;

-- El UPDATE real: AJUSTAR el WHERE a un rango legacy verificado
UPDATE fingerprint_detections
SET air_time = air_time + interval '6 hours'
WHERE pipeline_version = 'legacy'
  AND air_time >= TIMESTAMP '2026-01-01 00:00:00'
  AND air_time < TIMESTAMP '2026-06-13 09:00:00';

-- Verificar después
SELECT 'DESPUÉS' as check_point, air_time
FROM fingerprint_detections
ORDER BY air_time DESC LIMIT 3;

-- Si todo se ve bien: COMMIT
-- Si algo está mal: ROLLBACK
-- COMMIT;
-- ROLLBACK;
```

> **IMPORTANTE:** El `COMMIT` está comentado. Verificar los resultados antes de ejecutarlo.

### 2.4 Preparar código nuevo del Destroyer

Ver sección [4. Cambios de Código](#4-cambios-de-código) — hacer los cambios en el repo y tener el snapshot listo **antes** del cutover.

---

## 3. Cutover — 5 a 15 Minutos

Ejecutar en orden, sin saltarse pasos.

```bash
# ── PASO 1: Deshabilitar cron del Destroyer ──────────────────────────────────
crontab -l | grep -v launcher.py | crontab -
echo "Cron deshabilitado. Verificando:"
crontab -l

# ── PASO 2: Detener escritores del pipeline horario ──────────────────────────
systemctl stop stream-daemon
systemctl stop video-segment-uploader
echo "stream-daemon: $(systemctl is-active stream-daemon)"
echo "video-segment-uploader: $(systemctl is-active video-segment-uploader)"

# Los 12 streams siguen corriendo (supervisord no se toca)
echo "Streams ffmpeg:"
supervisorctl status | grep -E "RUNNING|FATAL"

# ── PASO 3: Tomar baseline operacional ───────────────────────────────────────
date -u +"%Y-%m-%dT%H:%M:%SZ"
# Guardar ese valor como MIGRATION_BASELINE_UTC en el runbook/canal del equipo.

# ── PASO 4: Deployar nuevo código ────────────────────────────────────────────
cd /opt/media-ai
git pull origin main   # o el branch con los cambios UTC

cd /opt/destroyer
git pull origin main

# ── PASO 5: Reiniciar escritores ─────────────────────────────────────────────
systemctl start stream-daemon
systemctl start video-segment-uploader
sleep 5
systemctl is-active stream-daemon
systemctl is-active video-segment-uploader

# ── PASO 6: Re-habilitar cron ────────────────────────────────────────────────
(crontab -l; echo "0 0,6,12,18 * * * /opt/destroyer/venv/bin/python /opt/destroyer/launcher.py") | crontab -
echo "Cron re-habilitado:"
crontab -l

# ── PASO 7: Validar ──────────────────────────────────────────────────────────
# Ver sección 5
```

> **Importante:** el backfill histórico queda fuera de esta ventana. El objetivo del cutover es que desde `MIGRATION_BASELINE_UTC` todo lo nuevo ya quede consistente.

---

## 4. Cambios de Código

### 4.1 Destroyer — `worker.py`

**Regla:** `air_time` debe seguir representando el instante absoluto real de emisión. En el código actual eso **no** sale del reloj del servidor; sale de combinar:

- nombre del MP3 horario
- `ts_sec` detectado dentro del archivo
- `created_at` / metadata auxiliar en `s3_scan_log`

El cambio correcto es **normalizar la inferencia temporal**, no reemplazarla por `datetime.now()`.

```python
# ANTES
air = air_time_from_item(s3_key, ts_sec, stream, s3_created_at)

# DESPUÉS
air = air_time_from_item(s3_key, ts_sec, stream, s3_created_at)
air_time_utc = air.astimezone(timezone.utc) if air else None
```

**En la inserción a la DB:** guardar el datetime aware en UTC o usar una columna explícita `air_time_utc`. No usar `AT TIME ZONE` incrustado en el placeholder de `psycopg2`; es innecesario si el valor ya viene timezone-aware.

```python
cursor.execute("""
    INSERT INTO fingerprint_detections (air_time, pipeline_version, ...)
    VALUES (%s, %s, ...)
""", (air_time_utc, 'utc_v2', ...))
```

**Para logs y notificaciones Telegram (solo aquí usamos HN):**

```python
HN_TZ = pytz.timezone('America/Tegucigalpa')

def to_hn(dt_utc: datetime) -> str:
    """Convierte UTC → HN para mostrar al usuario."""
    return dt_utc.astimezone(HN_TZ).strftime('%Y-%m-%d %H:%M:%S HN')

# Ejemplo:
print(f"Anuncio detectado: {to_hn(air_time_utc)}")
```

**Para MP3 horarios:** no mezclar dos cambios grandes en el mismo cutover.

### Fase 1 recomendada

- Mantener temporalmente el nombre legacy `YYYY-MM-DD_HHh.mp3`
- Agregar metadata/columnas UTC en `s3_scan_log` si hacen falta
- Hacer que Destroyer priorice ese dato UTC cuando exista

### Fase 2 opcional

- Cambiar naming de los MP3 a un formato UTC explícito
- Mantener compatibilidad de lectura con el formato legacy

Ejemplo de fase 2:

```python
now_utc = datetime.now(timezone.utc)
filename = f"{stream_id}_{now_utc.strftime('%Y-%m-%dT%HZ')}.mp3"
# Resultado: teleceiba_2026-06-13T05Z.mp3  (inequívocamente UTC)
```

> **Nota sobre S3 histórico:** los archivos `.mp3` viejos con nombre HN NO se renombran. Solo los nuevos usan el formato UTC. El Destroyer debe detectar ambos formatos.

```python
import re
from datetime import datetime, timezone

def parse_mp3_timestamp(filename: str) -> datetime:
    """
    Parsea timestamp de MP3 horario.
    Formato legacy:  stream_id_YYYY-MM-DD_HHh.mp3     (HN, GMT-6)
    Formato nuevo:   stream_id_YYYY-MM-DDTHHZ.mp3     (UTC)
    """
    HN_TZ = pytz.timezone('America/Tegucigalpa')
    
    # Formato nuevo UTC
    m = re.search(r'(\d{4}-\d{2}-\d{2})T(\d{2})Z', filename)
    if m:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}:00:00", '%Y-%m-%d %H:%M:%S')
        return dt.replace(tzinfo=timezone.utc)
    
    # Formato legacy HN
    m = re.search(r'(\d{4}-\d{2}-\d{2})_(\d{2})h', filename)
    if m:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}:00:00", '%Y-%m-%d %H:%M:%S')
        return HN_TZ.localize(dt).astimezone(timezone.utc)
    
    raise ValueError(f"No se puede parsear timestamp de: {filename}")
```

### 4.2 Stream Daemon — `stream_daemon.py`

```python
# ANTES
from datetime import datetime
ts = int(datetime.now().timestamp())          # puede ser local
updated_at = datetime.now().isoformat()       # naive, ambiguo

# DESPUÉS
from datetime import datetime, timezone
ts = int(datetime.now(timezone.utc).timestamp())    # unix epoch = UTC por definición
updated_at = datetime.now(timezone.utc).isoformat() # '2026-06-13T15:22:00+00:00'
```

**Inserción de eventos DOWN/UP:**

```python
# DESPUÉS - siempre timezone-aware
cursor.execute("""
    INSERT INTO mediadev_events (stream_id, ts, etype, detail)
    VALUES (%s, %s, %s, %s)
""", (
    stream_id,
    int(datetime.now(timezone.utc).timestamp()),  # unix epoch UTC
    event_type,
    detail
))
```

### 4.3 Video Segment Uploader — `video_segment_uploader.py`

Los segmentos `.ts` ya usan `epoch_start_epoch_end` (unix epoch = UTC). Sin cambios en el nombre.

Además, este servicio forma parte del cutover porque hoy:

- genera MP3 horarios TV con label HN
- inserta filas en `s3_scan_log`

Por eso debe detenerse y reiniciarse junto con `stream-daemon`.

Solo asegurar que cualquier metadata nueva que se guarde en DB use UTC:

```python
# DESPUÉS
from datetime import datetime, timezone

segment_start_utc = datetime.fromtimestamp(epoch_start, tz=timezone.utc)
segment_end_utc   = datetime.fromtimestamp(epoch_end,   tz=timezone.utc)

# Si se guarda en DB:
cursor.execute("""
    INSERT INTO video_segments_log (stream_id, start_utc, end_utc, s3_key)
    VALUES (%s, %s, %s, %s)
""", (stream_id, segment_start_utc, segment_end_utc, s3_key))
```

### 4.4 PubliAudit API — `main.py`

**Recibir del cliente:** si el cliente manda un rango de fechas en HN, convertir a UTC en el API antes de consultar:

```python
import pytz
from datetime import datetime, timezone

HN_TZ = pytz.timezone('America/Tegucigalpa')

def hn_to_utc(date_str: str, fmt: str = '%Y-%m-%d') -> datetime:
    """Convierte fecha HN a UTC para queries en DB."""
    dt_hn = HN_TZ.localize(datetime.strptime(date_str, fmt))
    return dt_hn.astimezone(timezone.utc)

# En el endpoint de detecciones:
@router.get("/api/detections")
async def get_detections(date_from: str, date_to: str):
    utc_from = hn_to_utc(date_from)           # '2026-06-13' → 2026-06-13 06:00:00 UTC
    utc_to   = hn_to_utc(date_to) + timedelta(days=1)  # hasta fin del día HN
    
    rows = db.query("""
        SELECT *, air_time AT TIME ZONE 'America/Tegucigalpa' AS air_time_hn
        FROM fingerprint_detections
        WHERE air_time >= %s AND air_time < %s
        ORDER BY air_time DESC
    """, (utc_from, utc_to))
    return rows
```

**Responder al frontend:** siempre devolver ambos campos:

```python
# En el schema de respuesta (Pydantic)
class DetectionResponse(BaseModel):
    air_time_utc: datetime    # para procesamiento interno / otros sistemas
    air_time_hn:  str         # para mostrar al usuario: "2026-06-13 23:15:05"
    
# En el serializer:
def serialize_detection(row):
    air_time_utc = row['air_time']  # ya en UTC en DB
    air_time_hn  = air_time_utc.astimezone(HN_TZ).strftime('%Y-%m-%d %H:%M:%S')
    return {
        'air_time_utc': air_time_utc.isoformat(),
        'air_time_hn':  air_time_hn,
        # ... otros campos
    }
```

### 4.5 Dashboard Flask — `dashboard_v4.py`

```python
# En cualquier lugar donde se formateen fechas para la API pública:
HN_TZ = pytz.timezone('America/Tegucigalpa')

def fmt_hn(ts_utc):
    """Para mostrar en el dashboard y API pública."""
    if isinstance(ts_utc, (int, float)):
        ts_utc = datetime.fromtimestamp(ts_utc, tz=timezone.utc)
    return ts_utc.astimezone(HN_TZ).strftime('%Y-%m-%d %H:%M:%S')
```

### 4.6 MCP Server — `tools/`

Las tools del MCP ya convierten a HN para mostrar. Solo asegurar que las queries usen UTC:

```python
# En tools/errors.py y tools/queue.py
# Cambiar cualquier:
datetime.now()
# Por:
datetime.now(timezone.utc)

# Cambiar cualquier:
ts > EXTRACT(EPOCH FROM NOW() - INTERVAL '6 hours')
# Por:
ts > EXTRACT(EPOCH FROM NOW() - INTERVAL '%s hours')  # con el parámetro correcto en UTC
```

---

## 5. Validación Post-Cutover

Ejecutar estas queries inmediatamente después del cutover:

```sql
-- 1. ¿Las nuevas detecciones entran en UTC?
SELECT air_time, 
       air_time AT TIME ZONE 'America/Tegucigalpa' AS hn,
       NOW() - air_time AS age
FROM fingerprint_detections
ORDER BY air_time DESC
LIMIT 5;
-- Esperado: air_time cerca de NOW() en UTC, hn = NOW() - 6h

-- 2. ¿Las nuevas filas ya salen marcadas como UTC v2?
SELECT air_time,
       pipeline_version,
       air_time AT TIME ZONE 'America/Tegucigalpa' AS hn
FROM fingerprint_detections
ORDER BY air_time DESC
LIMIT 10;
-- Esperado: filas nuevas con pipeline_version='utc_v2'

-- 3. ¿El stream-daemon sigue insertando?
SELECT stream_id, status, updated_at
FROM mediadev_stream_status
ORDER BY updated_at DESC
LIMIT 3;
-- updated_at debe ser reciente (< 30 segundos)

-- 4. Verificar events
SELECT stream_id, ts, etype,
       to_timestamp(ts) AT TIME ZONE 'America/Tegucigalpa' AS ts_hn
FROM mediadev_events
ORDER BY ts DESC
LIMIT 5;
```

**Validación desde el API:**

```bash
# La API pública debe seguir mostrando hora HN al usuario
curl http://159.223.104.91/api/detections?limit=3 | jq '.[0].air_time'
# Debe mostrar hora en HN (lo que ve el usuario)

# El campo interno debe ser UTC
curl http://159.223.104.91:8080/api/detections | jq '.[0].air_time_utc'
# Debe mostrar hora en UTC (para sistemas internos)
```

### Validación adicional para TV

- Tomar 1 detección nueva de `hch_tv` o `teleceiba`
- Verificar que `air_time_utc` convierta correctamente a HN
- Confirmar que el clip MP4 salga del día/rango correcto en `video_segments/`
- Confirmar que no aparezca un offset fijo de 6 horas entre el clip y la hora mostrada

---

## 6. Regla de Oro para Frontend

**Una sola regla:** el backend devuelve UTC, el frontend muestra HN. Nunca al revés.

```
Backend / DB / S3 / MCP / API interna  →  UTC siempre
API pública / Dashboard / PubliAudit   →  UTC en campo raw + HN en campo display
Frontend / PDF / comprobantes / usuario →  GMT-6 siempre
```

### Conversión en frontend (JavaScript)

```javascript
// Dado un campo air_time_utc en ISO 8601 desde la API:
const airTimeUtc = new Date(detection.air_time_utc);

// Mostrar en HN:
const hnFormatter = new Intl.DateTimeFormat('es-HN', {
    timeZone: 'America/Tegucigalpa',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false
});
const displayTime = hnFormatter.format(airTimeUtc);
// '13/06/2026, 23:15:05'
```

### Conversión en Python (para reportes PDF, Telegram, etc.)

```python
import pytz
from datetime import datetime, timezone

HN_TZ = pytz.timezone('America/Tegucigalpa')

def display_hn(dt: datetime) -> str:
    """Siempre usar esta función para mostrar al usuario."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # asumir UTC si no tiene zone
    return dt.astimezone(HN_TZ).strftime('%d/%m/%Y %H:%M:%S HN')
```

---

## 7. Rollback

Si algo sale mal, revertir en < 2 minutos:

```bash
# ROLLBACK COMPLETO

# 1. Detener escritores
systemctl stop stream-daemon
systemctl stop video-segment-uploader

# 2. Revertir código al release anterior conocido
cd /opt/media-ai && git checkout <release_anterior_conocido>
cd /opt/destroyer && git checkout <release_anterior_conocido>

# 3. Si el problema fue solo de código, NO tocar histórico.
#    Si hubo backfill posterior y validado como incorrecto, restaurar desde backup
#    o ejecutar un script de reversión delimitado por pipeline_version='legacy'
#    y rango exacto ya auditado. Nunca usar:
#    "restar 6 horas a todo lo posterior al baseline".

# 4. Reiniciar servicios
systemctl start stream-daemon
systemctl start video-segment-uploader

# 5. Restaurar cron
(crontab -l; echo "0 0,6,12,18 * * * /opt/destroyer/venv/bin/python /opt/destroyer/launcher.py") | crontab -
```

> **Regla de rollback:** revertir código y reabrir escritura nueva es rápido; revertir histórico requiere procedimiento aparte y nunca debe hacerse con un `UPDATE -6h` genérico.

---

## Checklist Final

### Preparación (antes del cutover)
- [ ] Backup de DB ejecutado y verificado
- [ ] Tipo de columna `air_time` verificado (con o sin timezone)
- [ ] Script `migrate_utc.sql` preparado y revisado
- [ ] Código nuevo Destroyer listo en el repo
- [ ] Código nuevo stream-daemon listo
- [ ] Snapshot nuevo del Destroyer preparado (si aplica)
- [ ] Ventana de mantenimiento comunicada al equipo

### Durante el cutover
- [ ] Cron del Destroyer deshabilitado
- [ ] stream-daemon detenido
- [ ] video-segment-uploader detenido
- [ ] Baseline UTC del cutover documentado
- [ ] Código nuevo desplegado
- [ ] stream-daemon reiniciado y activo
- [ ] video-segment-uploader reiniciado y activo
- [ ] Cron re-habilitado

### Post-cutover
- [ ] Query de validación ejecutada y correcta
- [ ] API pública responde con hora HN correcta
- [ ] PubliAudit API responde con UTC en campo raw
- [ ] Dashboard muestra horas HN correctas
- [ ] Primera corrida del Destroyer (próximo ciclo) validada
- [ ] Detección nueva de TV validada extremo a extremo
- [ ] Snapshot nuevo del Destroyer tomado

---

*Documento preparado por Claude Code (claude-sonnet-4-6) — 2026-06-13*  
*Sistema: MediaDEV · Servidor: 159.223.104.91*
