# Dashboard — CLAUDE.md

> ⚠️ **REFERENCIA HISTÓRICA — este dashboard NO corre.** `dashboard_v4.py` fue eliminado del
> servicio el 14 jun 2026; `dashboard_mediadev.service` está **inactivo** y sus endpoints
> `/api/*` ya no responden. El código sigue en `dashboard/` solo como referencia. El producto
> y su API viven en **mediaAPP** (`media-app`, FastAPI, repo `gchiham/media-app`). Lo de abajo
> describe cómo funcionaba el dashboard de captura.

## Propósito (histórico)
Aplicación Flask que servía el dashboard de salud y la API JSON. Lee de **PostgreSQL
(media-db)** vía psycopg2. Gunicorn con 1 worker (constraint 2 vCPU) detrás de nginx en :80.

## Archivos clave
- `dashboard_v4.py` — única versión activa.
- `templates/dashboard_main.html` — KPIs globales + grid de 12 stream cards.
- `templates/stream_detail.html` — detalle por stream: player HLS, KPIs, gráfico, auditoría, eventos.

## Rutas
Vistas web:
- `GET /` — Dashboard principal.
- `GET /stream/<sid>` — Detalle de stream individual.

API JSON (read-only, para consumo externo / Claude Design):
- `GET /api/status` y alias `GET /api/streams` — estado en vivo de los 12 streams.
- `GET /api/stations` — catálogo (filtros `?status=`, `?type=`).
- `GET /api/detections` — detecciones recientes con JOIN a advertisements (`?limit=`).
- `GET /api/summary` — KPIs globales.

HLS (`/streams/<sid>/index.m3u8`) lo sirve nginx directo, no Flask.

## Conexión a PostgreSQL
```python
db()  # psycopg2 con RealDictCursor (filas como dict) y sslmode=require
qall(con, sql, params)  # fetchall
qone(con, sql, params)  # fetchone
# Placeholders %s (NO ? como SQLite). Credenciales de /etc/mediadev-db.env.
```

## Reglas de queries
- SIEMPRE batch GROUP BY (1 query para todos los streams), nunca loops por stream.
- **`SUM(bytes)` en PostgreSQL devuelve `Decimal`** → castear a `::bigint` (o `::float8` para
  ROUND) cuando el valor va a `json.dumps`, o convertir con `_ser()`. SQLite devolvía int/float;
  PG no — este fue el bug que rompía `/stream/<sid>`.
- La auditoría de segmentos se deriva del filesystem (`disk_segments()`), no de la DB.

## Serialización JSON
`_ser()` convierte datetime→ISO, Decimal→float, UUID→str. Usar `rows_json()` para listas.

## Zona horaria
Backend almacena UTC. El display convierte a GMT-6 (Honduras, sin DST, offset fijo).
```python
TGU = timezone(timedelta(hours=-6))  # solo para presentación — DB guarda UTC
```

## Pitfalls
- Llamar `last_event_str(con, sid, etype)` ANTES de `con.close()`.
- Variables de loop del chart: usar `hr`, no `row` (no pisar la query externa).
- `template_folder="templates"`.
