# Destroyer — Sistema de Releases Versionadas

**Fecha de implementación:** 13 junio 2026  
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
ffmpeg                             destroyer-v10.tar.gz  ← worker.py + fingerprint.py
Python 3.11                        destroyer-v11.tar.gz
venv con:                          latest.tar.gz         ← apunta a la más reciente
  boto3, psycopg2, scipy,
  numpy, requests, etc.
```

**Flujo al arrancar el droplet:**

```
launcher.py
    │
    ├─ Lee DESTROYER_RELEASE del .env  (ej: "destroyer-v10")
    │
    ├─ Crea droplet desde snapshot base
    │
    └─ cloud-init ejecuta:
           1. Exporta credenciales y env vars
           2. aws s3 cp s3://mediadev-recordings/destroyer/releases/destroyer-v10.tar.gz /tmp/release.tar.gz
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
        ├── destroyer-v10.tar.gz    ← código de producción actual
        ├── destroyer-v11.tar.gz    ← próxima versión
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
# En el servidor mediaDEV (159.223.104.91):
cd /opt/destroyer
./release.sh v11
```

El script hace:
1. `tar -czf /tmp/destroyer-v11.tar.gz worker.py fingerprint.py`
2. Sube `destroyer-v11.tar.gz` a S3
3. Actualiza `latest.tar.gz` en S3
4. Muestra instrucción para activarla

**Activar la release:**
```bash
# Editar /opt/destroyer/.env
DESTROYER_RELEASE=destroyer-v11
```

El próximo cron del Destroyer (o lanzamiento manual) usará la nueva release.

---

## Rollback

```bash
# Volver a la versión anterior:
# Editar /opt/destroyer/.env
DESTROYER_RELEASE=destroyer-v10
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
24 | timeout|    93      |       0          | destroyer-v10    | 2026-06-13 18:34
23 | timeout|    59      |       0          | (null)           | 2026-06-13 12:01
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
SNAPSHOT_ID=232701378         # ID del snapshot base en DigitalOcean
DESTROYER_RELEASE=destroyer-v10  # Release del código a descargar desde S3
DESTROYER_WORKERS=32          # Número de workers paralelos (default: 32)
```

---

## Releases publicadas

| Release | Fecha | Cambios |
|---|---|---|
| `destroyer-v10` | 13 jun 2026 | Primera release S3. Incluye fix libx264 ultrafast para clips TV + migración UTC (pipeline_version=utc_v2) |

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

*Sistema: MediaDEV · Servidor: 159.223.104.91 · 13 junio 2026*
