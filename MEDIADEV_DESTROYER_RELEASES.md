# Destroyer — Sistema de Releases Versionadas

**Fecha de implementación:** 13 junio 2026 (DigitalOcean) · **Migrado a AWS:** 14 junio 2026  
**Última actualización:** 15 junio 2026  
**Motivación:** Separar las dependencias del sistema (AMI base) del código de la aplicación (S3), eliminando la necesidad de recrear la imagen base por cada cambio de código.

> **Cambio de plataforma (14 jun 2026):** el Destroyer migró de droplets DigitalOcean c-16 a
> **AWS EC2 Spot c5.4xlarge**, orquestado por EventBridge + Lambda (100% serverless). El sistema
> de releases S3 sobrevive intacto; lo que cambió es la base (snapshot DO → **AMI AWS**) y el
> lanzador (cron/launcher en mediaAPP → **Lambda `destroyer-launcher`**). Ver `live_mediaDEV.md` §4.

---

## El problema que resuelve

La imagen base del Destroyer mezclaba dos responsabilidades:

| Qué contiene | Frecuencia de cambio |
|---|---|
| Ubuntu 22.04 + ffmpeg + Python 3.12 + venv (boto3, psycopg2, scipy, numpy, pydub…) | Raro (meses) |
| `worker.py` + `fingerprint.py` | Frecuente (cada fix) |

Antes (snapshot por código): cada fix requería recrear la imagen (~10 min), sin auditoría de qué
código corrió cada run. **Ahora:** la base es estable y el código se versiona en S3 (y en GitHub
`gchiham/destroyer`), se descarga al arrancar la instancia. Deploy instantáneo, rollback en 1 línea,
columna `release_version` en `destroyer_runs` para auditoría.

---

## Arquitectura (AWS)

```
AMI base (estable)               S3 releases (versionadas)
─────────────────────────        ────────────────────────────────
ami-065708bbb25ab56ad            s3://mediadev-recordings/destroyer/releases/
(destroyer-v3-ubuntu22-pydub)      destroyer-v20.tar.gz
Ubuntu 22.04                       destroyer-v21.tar.gz
ffmpeg 4.4.2                       destroyer-v22.tar.gz  ← worker.py + fingerprint.py (ACTIVA)
Python 3.12 + venv (con pydub)     latest.tar.gz         ← apunta a la más reciente
```

**Flujo al arrancar (cloud-init de la instancia Spot):**
```
EventBridge `destroyer-hourly` → Lambda `destroyer-launcher`
    ├─ lee DESTROYER_RELEASE (ej "destroyer-v22") + DESTROYER_AMI_ID
    ├─ lanza EC2 Spot c5.4xlarge desde el AMI
    └─ user_data:
         1. exporta credenciales/env (keys IAM mediadev-s3 horneadas)
         2. aws s3 cp s3://mediadev-recordings/destroyer/releases/destroyer-v22.tar.gz /tmp/release.tar.gz
         3. tar -xzf /tmp/release.tar.gz -C /opt/destroyer/
         4. python worker.py
         5. al terminar: aws ec2 terminate-instances (auto-destrucción)
```

Boot ~53-96s + scan ~30s. La descarga del tar.gz (~14KB) agrega <1s.

Contenido de cada tar.gz: `worker.py`, `fingerprint.py`.

---

## Publicar una nueva release

```bash
# En mediaAPP (137.184.53.234), /opt/destroyer:
cd /opt/destroyer
./release.sh v22
```
El script: `tar -czf destroyer-v22.tar.gz worker.py fingerprint.py` → sube a S3 → actualiza
`latest.tar.gz` → muestra instrucción para activar.

**Activar:** editar `/opt/destroyer/.env` → `DESTROYER_RELEASE=destroyer-v22`. La próxima corrida
(EventBridge horario o invocación manual de la Lambda) usa el nuevo código.

**Rollback:** `DESTROYER_RELEASE=destroyer-v21` en `.env`. Instantáneo, sin recrear AMI.

---

## Cuándo recrear el AMI base

Solo cuando cambien **dependencias del sistema**, no el código:

| Cambio | ¿Nuevo AMI? |
|---|---|
| Fix en `worker.py` / `fingerprint.py` | ❌ Solo `./release.sh vX` |
| Nueva librería Python en el venv | ✅ Sí |
| Actualización de ffmpeg / Python | ✅ Sí |
| Nuevo archivo `.py` en el Destroyer | ❌ Solo `./release.sh vX` (agregar al tar.gz) |

**Proceso para nuevo AMI** (así se horneó pydub en el v3): instancia plana desde el AMI previo →
instalar la dependencia por SSH → verificar import + ffmpeg → `aws ec2 create-image` →
`update_function_configuration` de la Lambda `destroyer-launcher` con el nuevo `DESTROYER_AMI_ID`.

---

## Variables de entorno relevantes

En `/opt/destroyer/.env` (NO en git):
```bash
DESTROYER_AMI_ID=ami-065708bbb25ab56ad   # AMI base AWS (reemplazó SNAPSHOT_ID de DO)
DESTROYER_RELEASE=destroyer-v22          # release del código a descargar de S3
DESTROYER_WORKERS=32                       # workers paralelos
DESTROYER_SCAN_FILE_TIMEOUT=300            # cap por archivo "veneno"
DESTROYER_WTA_WINDOW_SEC=8                 # ventana cross-ad para Winner Takes All
DESTROYER_HOURLY_USD=0.25                  # fallback costo spot
# AWS_*, TG_*, S3_BUCKET, PG_*  → credenciales (ver INVENTORY.md)
```

---

## Auditoría por run

`destroyer_runs.release_version` registra qué código corrió:
```sql
SELECT id, status, files_done, total_detections, release_version, t2_started
FROM destroyer_runs ORDER BY id DESC LIMIT 10;
```
Runs pre-migración tienen `release_version = NULL` (código baked en snapshot).

---

## Releases publicadas

| Release | Fecha | Cambios |
|---|---|---|
| `destroyer-v10` | 13 jun 2026 | Primera release S3 (DO). Fix libx264 ultrafast para clips TV + migración UTC (`utc_v2`). |
| `destroyer-v11` | 13 jun 2026 | Launcher con releases S3 en producción (DO). |
| `destroyer-v14` | 14 jun 2026 | `fingerprint.py` colapsa offsets vecinos; `worker.py` dedup por ventana real + Winner Takes All; debug logs a S3; timeout por archivo configurable. |
| `destroyer-v15` | 14 jun 2026 | Idempotencia DB (`ON CONFLICT DO NOTHING`); no genera clip/Telegram en duplicados. Regresión: timeout desde `submitted_at` (incluía espera en cola). |
| `destroyer-v16` | 14 jun 2026 | Hotfix run 28: timeout efectivo por `ar.get(timeout=...)`, default `300s`. Última release en DigitalOcean. |
| `destroyer-v20` | 14 jun 2026 | Primera sobre **EC2/AWS**: `DO_TOKEN` opcional, `get_droplet_id()` usa metadata EC2, `self_destruct()` salta API DO. |
| `destroyer-v21` | 14 jun 2026 | Costo real spot (`describe_spot_price_history`); Telegram "💸 Costo de este deploy" ($/run, $/hr). |
| `destroyer-v22` | 14 jun 2026 | **Release activa.** Fix cosmético del nombre del clip (`name_stem`=filename real en vez del tmp). |

---

## Incidente Destroyer0000 / run 28 (14 jun 2026)

**Síntoma:** run `28` quedó en `timeout` con `18/48` archivos procesados, `18` en `error` (`scan timeout 240s`) y `30` atascados en `scanning`.

**Causa raíz:** `destroyer-v15` midió el timeout desde `submitted_at` (entrada al pool) en vez de desde la espera real. Con `48` archivos y `28` workers, archivos en cola acumulaban tiempo sin ejecutarse → timeouts falsos.

**Corrección:** `destroyer-v16` (vuelve `ar.get(timeout=...)`, default `300s`); se resetearon `30` rows de `s3_scan_log` de `scanning` a `pending`; `run 28` corregido con `files_error=18`.

---

## Migración de deduplicación en DB (14 jun 2026)

Aplicada contra la DB privada de DO Managed PostgreSQL. Resultado verificado: `58` grupos
duplicados detectados, `95` filas desactivadas por soft-delete, `0` duplicados activos después.
Índice creado:
```sql
CREATE UNIQUE INDEX ux_fingerprint_detections_source_match_active
ON fingerprint_detections (tenant_id, campaign_id, ad_id, stream_id, s3_key, ts_seconds)
WHERE deleted_at IS NULL;
```

---

## Verificar que una release existe en S3

```bash
cd /opt/destroyer && set -a && source .env && set +a
/opt/destroyer/venv/bin/python3 -c "
import boto3, os
s3 = boto3.client('s3', region_name='us-east-1')
for o in s3.list_objects_v2(Bucket=os.environ['S3_BUCKET'], Prefix='destroyer/releases/').get('Contents', []):
    print(o['Key'], f\"{o['Size']/1024:.1f}KB\")
"
```

---

*Sistema: Destroyer / AWS EC2 Spot (us-east-1) · Lanzado desde Lambda en cuenta 050871635829 · Actualizado: 15 junio 2026*
