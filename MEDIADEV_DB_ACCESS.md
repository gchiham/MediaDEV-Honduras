# MediaDEV — Acceso a la Base de Datos

**DB:** PostgreSQL managed (DigitalOcean)  
**Instancia:** `destroyer_db`  
**Nota:** el hostname `private-...` solo es accesible desde dentro de la red de DigitalOcean. Para conectarse desde fuera se requiere SSH tunnel a través del servidor MediaDEV.

---

## Credenciales

| Campo | Valor |
|---|---|
| Host | `private-media-db-do-user-2116998-0.d.db.ondigitalocean.com` |
| Puerto | `25060` |
| Base de datos | `destroyer_db` |
| Usuario | `destroyer` |
| Contraseña | `SgUWgtzmiWUAfZ91vInmUxiVWl4XC` |
| SSL | requerido (`sslmode=require`) |

---

## Opción 1 — psql directo desde el servidor

La forma más simple. Entrar al servidor por SSH y correr psql ahí mismo.

```bash
# 1. Conectar al servidor
ssh -i ~/.ssh/keySED root@159.223.104.91

# 2. Ya adentro del servidor, abrir psql:
PGPASSWORD='SgUWgtzmiWUAfZ91vInmUxiVWl4XC' psql \
  -h private-media-db-do-user-2116998-0.d.db.ondigitalocean.com \
  -p 25060 \
  -U destroyer \
  -d destroyer_db
```

También puede leer las credenciales directo del archivo de configuración:

```bash
set -a && source /etc/mediadev-db.env && set +a
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB"
```

---

## Opción 2 — SSH Tunnel + cliente local

Permite usar cualquier cliente gráfico (TablePlus, DBeaver, pgAdmin) desde la máquina local.

### Paso 1 — Abrir el tunnel

```bash
ssh -i ~/.ssh/keySED \
  -L 5433:private-media-db-do-user-2116998-0.d.db.ondigitalocean.com:25060 \
  root@159.223.104.91 \
  -N
```

Dejar esta terminal abierta mientras se usa la DB. El flag `-N` mantiene el tunnel sin abrir shell.

### Paso 2 — Conectar el cliente local

Con el tunnel activo, usar estos datos en el cliente:

| Campo | Valor |
|---|---|
| Host | `127.0.0.1` |
| Puerto | `5433` |
| Base de datos | `destroyer_db` |
| Usuario | `destroyer` |
| Contraseña | `SgUWgtzmiWUAfZ91vInmUxiVWl4XC` |
| SSL | requerido |

**psql local:**
```bash
PGPASSWORD='SgUWgtzmiWUAfZ91vInmUxiVWl4XC' psql \
  -h 127.0.0.1 -p 5433 -U destroyer -d destroyer_db
```

**TablePlus / DBeaver / pgAdmin:**  
Crear nueva conexión PostgreSQL con los datos de la tabla de arriba apuntando a `127.0.0.1:5433`.

---

## Queries útiles de arranque

```sql
-- Ver tablas disponibles
\dt

-- Estado de los 12 streams
SELECT stream_id, status, sup, segs, age, updated_at
FROM mediadev_stream_status
ORDER BY stream_id;

-- Últimas detecciones de anuncios
SELECT stream_id, air_time AT TIME ZONE 'America/Tegucigalpa' AS air_time_hn,
       pipeline_version, score
FROM fingerprint_detections
ORDER BY air_time DESC
LIMIT 10;

-- Verificar migración UTC (primeras filas utc_v2)
SELECT pipeline_version, COUNT(*), MIN(air_time) as desde
FROM fingerprint_detections
GROUP BY pipeline_version;

-- Historial de corridas del Destroyer
SELECT id, status, files_done, total_files, total_detections,
       t2_started AT TIME ZONE 'America/Tegucigalpa' AS started_hn,
       cost_usd
FROM destroyer_runs
ORDER BY id DESC
LIMIT 5;

-- Últimos errores de streams (últimas 6 horas)
SELECT stream_id, etype, detail,
       to_timestamp(ts) AT TIME ZONE 'America/Tegucigalpa' AS ts_hn
FROM mediadev_events
WHERE ts > EXTRACT(EPOCH FROM NOW() - INTERVAL '6 hours')
ORDER BY ts DESC;
```

---

## Notas de seguridad

- La contraseña está en `/etc/mediadev-db.env` (chmod 600) en el servidor
- La DB no acepta conexiones directas desde internet — siempre via tunnel o desde el servidor
- La conexión del sistema usa `default_transaction_read_only=on` para operaciones de solo lectura — para writes usar la conexión normal
- No compartir estas credenciales fuera del equipo de desarrollo

---

*Sistema: MediaDEV · Servidor: 159.223.104.91*
