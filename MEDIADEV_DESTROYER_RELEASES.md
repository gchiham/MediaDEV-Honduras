# Destroyer — Sistema de Releases Versionadas

**Fecha de implementación:** 13 junio 2026  
**Última actualización:** 14 junio 2026  
**Motivación:** Separar las dependencias del sistema (snapshot) del código de la aplicación (S3), eliminando la necesidad de recrear el snapshot por cada cambio de código.

---

## El problema anterior

El snapshot del Destroyer mezclaba dos responsabilidades:

| Qué contenía | Frecuencia de cambio |
|---|---|
| Ubuntu 22.04 + ffmpeg + Python + venv + librerías | Raro (meses) |
| `worker.py` + `fingerprint.py` | Frecuente (cada fix) |

Consecuencias:
- Cada fix de código requería crear un nuevo snapshot (~10 minutos)
- No había auditoría de qué versión del código corrió cada run
- Rollback = cambiar `SNAPSHOT_ID` y esperar que el snapshot exista

---

## Arquitectura nueva

```
Snapshot base (estable)          S3 releases (versionadas)
─────────────────────────        ────────────────────────────────
Ubuntu 22.04                     destroyer/releases/
ffmpeg                             destroyer-v10.tar.gz
Python 3.11                        destroyer-v11.tar.gz
venv con:                          destroyer-v14.tar.gz
  boto3, psycopg2, scipy,          destroyer-v15.tar.gz
  numpy, requests, etc.            destroyer-v16.tar.gz  ← worker.py + fingerprint.py
                                   latest.tar.gz         ← apunta a la más reciente
```

**Flujo al arrancar el droplet:**

```
launcher.py
    │
    ├─ Lee DESTROYER_RELEASE del .env  (ej: "destroyer-v16")
    │
    ├─ Crea droplet desde snapshot base
    │
    └─ cloud-init ejecuta:
           1. Exporta credenciales y env vars
           2. aws s3 cp s3://mediadev-recordings/destroyer/releases/destroyer-v16.tar.gz /tmp/release.tar.gz
           3. tar -xzf /tmp/release.tar.gz -C /opt/destroyer/
           4. python worker.py
```

**Velocidad de arranque:** idéntica. El tiempo dominante es la provisión del droplet por DigitalOcean (~70-90s). La descarga del tar.gz desde S3 agrega <1 segundo (nyc1 → us-east-1, archivo ~14KB).

---

## Estructura en S3

```
s3://mediadev-recordings/
└── destroyer/
    └── releases/
        ├── destroyer-v10.tar.gz
        ├── destroyer-v11.tar.gz
        ├── destroyer-v14.tar.gz
        ├── destroyer-v15.tar.gz
        ├── destroyer-v16.tar.gz    ← código de producción actual
        └── latest.tar.gz           ← siempre = la última publicada
```

Contenido de cada tar.gz:
```
worker.py
fingerprint.py
```

---

## Publicar una nueva release

```bash
# En el servidor mediaAPP (137.184.53.234):
cd /opt/destroyer
./release.sh v16
```

El script hace:
1. `tar -czf /tmp/destroyer-v16.tar.gz worker.py fingerprint.py`
2. Sube `destroyer-v16.tar.gz` a S3
3. Actualiza `latest.tar.gz` en S3
4. Muestra instrucción para activarla

**Activar la release:**
```bash
# Editar /opt/destroyer/.env
DESTROYER_RELEASE=destroyer-v16
```

El próximo cron del Destroyer (o lanzamiento manual) usará la nueva release.

---

## Rollback

```bash
# Volver a la versión anterior:
# Editar /opt/destroyer/.env
DESTROYER_RELEASE=destroyer-v15
```

Instantáneo. No requiere recrear snapshots ni reiniciar servicios.

---

## Auditoría por run

La tabla `destroyer_runs` tiene la columna `release_version`:

```sql
SELECT id, status, files_done, total_detections, release_version, t2_started
FROM destroyer_runs
ORDER BY id DESC
LIMIT 10;
```

Ejemplo de resultado:
```
id | status | files_done | total_detections | release_version  | t2_started
27 | destroyed |   199    |      24          | destroyer-v13    | 2026-06-14 01:32
24 | timeout   |    93    |       0          | destroyer-v10    | 2026-06-13 18:34
```

Los runs anteriores a esta migración tienen `release_version = NULL` (usaban código baked en snapshot).

---

## Cuándo recrear el snapshot

Solo cuando cambien **dependencias del sistema**, no el código:

| Cambio | ¿Nuevo snapshot? |
|---|---|
| Fix en `worker.py` o `fingerprint.py` | ❌ Solo `./release.sh vX` |
| Nueva librería Python en el venv | ✅ Sí |
| Actualización de ffmpeg | ✅ Sí |
| Cambio de versión de Python | ✅ Sí |
| Nuevo archivo `.py` en el Destroyer | ❌ Solo `./release.sh vX` (agregar al tar.gz) |

### Proceso para nuevo snapshot base

```bash
# 1. Hacer los cambios de sistema en el droplet base o en mediaDEV
# 2. Crear snapshot desde DO dashboard o API
# 3. Actualizar /opt/destroyer/.env:
SNAPSHOT_ID=232XXXXXX
```

---

## Variables de entorno relevantes

En `/opt/destroyer/.env`:

```bash
SNAPSHOT_ID=232701378            # ID del snapshot base en DigitalOcean
DESTROYER_RELEASE=destroyer-v16  # Release del código a descargar desde S3
DESTROYER_WORKERS=32             # Número de workers paralelos (default: 32)
DESTROYER_SCAN_FILE_TIMEOUT=300  # Cap por archivo "veneno"
DESTROYER_WTA_WINDOW_SEC=8       # Ventana cross-ad para WTA
```

---

## Releases publicadas

| Release | Fecha | Cambios |
|---|---|---|
| `destroyer-v10` | 13 jun 2026 | Primera release S3. Incluye fix libx264 ultrafast para clips TV + migración UTC (pipeline_version=utc_v2) |
| `destroyer-v11` | 13 jun 2026 | Launcher con releases S3 en producción. |
| `destroyer-v14` | 14 jun 2026 | `fingerprint.py` colapsa offsets vecinos, `worker.py` aplica dedup por ventana real y Winner Takes All, sube debug logs a S3 y usa timeout por archivo configurable. |
| `destroyer-v15` | 14 jun 2026 | Inserción idempotente en DB con `ON CONFLICT DO NOTHING`, evita generar clips/Telegram en duplicados exactos y acompaña la migración de deduplicación en `fingerprint_detections`. Introdujo una regresión en el loop del pool: el timeout empezó a contarse desde `submitted_at`, incluyendo espera en cola. |
| `destroyer-v16` | 14 jun 2026 | Hotfix del incidente `Destroyer0000`/run `28`: vuelve el timeout efectivo por `ar.get(timeout=...)` y restaura el default a `300s`, evitando que archivos queued expiren antes de ejecutarse. Release activa actual. |

---

## Incidente Destroyer0000 / run 28 (14 jun 2026)

**Síntoma:** run `28` (`Destroyer0000`) quedó en `timeout` con `18/48` archivos procesados, `18` en `error` (`scan timeout 240s`) y `30` atascados en `scanning`.

**Causa raíz:** `destroyer-v15` cambió el pool para medir timeout desde `submitted_at` en vez de desde la espera real del resultado. Con `48` archivos y `28` workers, varios archivos acumulaban tiempo mientras solo esperaban turno en cola. Eso produjo timeouts falsos. Tras varios recreados del pool, la corrida quedó viva pero sin más heartbeats.

**Corrección aplicada:** se publicó `destroyer-v16`, se activó en `mediaAPP`, se dejó explícito `DESTROYER_SCAN_FILE_TIMEOUT=300` y se resetearon `30` rows de `s3_scan_log` de `scanning` a `pending`. El `run 28` quedó corregido en DB con `files_error=18`.

---

## Migración de deduplicación en DB (14 jun 2026)

Se aplicó la migración `fingerprint_detection_dedup_migration.sql` desde `mediaDEV` contra la DB privada de DO Managed PostgreSQL.

Resultado verificado:
- `58` grupos duplicados activos detectados antes de migrar
- `95` filas extra desactivadas por soft-delete
- `0` grupos duplicados activos después de migrar
- Índice creado:

```sql
CREATE UNIQUE INDEX ux_fingerprint_detections_source_match_active
ON fingerprint_detections (tenant_id, campaign_id, ad_id, stream_id, s3_key, ts_seconds)
WHERE deleted_at IS NULL;
```

---

## Verificar que una release existe en S3

```bash
cd /opt/destroyer
set -a && source .env && set +a
/opt/destroyer/venv/bin/python3 -c "
import boto3, os
s3 = boto3.client('s3', region_name='us-east-1')
r = s3.list_objects_v2(Bucket=os.environ['S3_BUCKET'], Prefix='destroyer/releases/')
for o in r.get('Contents', []):
    print(o['Key'], f\"{o['Size']/1024:.1f}KB\")
"
```

---

*Sistema: Destroyer / mediaAPP · Servidor: 137.184.53.234 · Actualizado: 14 junio 2026*
