# MediaDEV — Resultados de Mejoras de Seguridad y Estabilidad

**Ejecutado:** 13 junio 2026  
**Servidor:** 159.223.104.91 (DigitalOcean, 2 vCPU / 4 GB)

---

## 1. Credenciales publiaudit-api → EnvironmentFile ✅

**Problema:** Las credenciales de la API (JWT secret, AWS keys, DB password) estaban como `Environment=` inline en el `.service`. Cualquier usuario con acceso a `systemctl show` las veía en texto plano.

**Solución aplicada:**

Creado `/etc/publiaudit-api.env` (chmod 600, chown root:root) con todas las variables:
```
PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASS
JWT_SECRET, JWT_EXP_HOURS
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
S3_BUCKET, S3_REGION, PUBLIC_BASE_URL
```

Cambiado `/etc/systemd/system/publiaudit-api.service`:
```ini
# ANTES (visible en systemctl show):
Environment=JWT_SECRET=publiaudit_s3cr3t_2026_hn
Environment=AWS_SECRET_ACCESS_KEY=...

# DESPUÉS (seguro):
EnvironmentFile=/etc/publiaudit-api.env
```

**Verificación:**
```bash
systemctl show publiaudit-api --property=Environment
# → Environment=    (vacío — las vars no son visibles)

systemctl show publiaudit-api --property=EnvironmentFiles
# → EnvironmentFiles=/etc/publiaudit-api.env (not encrypted)

systemctl is-active publiaudit-api
# → active
```

---

## 2. CORS restringido a env var ✅

**Problema:** `main.py` tenía `allow_origins=['*']` hardcodeado, permitiendo requests desde cualquier origen.

**Solución aplicada** en `/opt/publiaudit-api/main.py`:
```python
_cors_origins_env = os.environ.get('CORS_ORIGINS', '*')
_cors_origins = (
    [o.strip() for o in _cors_origins_env.split(',') if o.strip()]
    if _cors_origins_env != '*' else ['*']
)
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, ...)
```

Para restringir orígenes, agregar al `/etc/publiaudit-api.env`:
```
CORS_ORIGINS=https://app.publiaudit.com,https://admin.publiaudit.com
```

Sin esa variable sigue funcionando con `*` (comportamiento por defecto preservado).

---

## 3. Migración UTC — Cutover ejecutado ✅

**Fecha/hora del cutover:** 13 junio 2026, 16:07 UTC (10:07 AM Honduras)

**Estado post-cutover:**

| pipeline_version | Filas | Primer registro |
|---|---|---|
| `legacy` | 398 | 2026-06-08 00:58:35 UTC |
| `utc_v2` | 0 | (pendiente — próxima corrida) |

Las filas `legacy` son esperadas: son detecciones anteriores al cutover. Las nuevas detecciones generadas después de las 16:07 UTC del 13 junio llevarán `pipeline_version = 'utc_v2'`.

**Cuándo aparecerán las primeras filas utc_v2:**
- Próximo cierre de MP3 horario: ~17:00 UTC
- Próxima corrida del Destroyer: 18:00 HN (00:00 UTC del 14 junio)

**Nota sobre corridas recientes del Destroyer:**

| Run ID | Status | Archivos | Hora inicio (UTC) |
|---|---|---|---|
| 23 | timeout | 59/97 | 13 jun 12:01 |
| 22 | timeout | 32/65 | 13 jun 06:01 |
| 21 | destroyed | 2/2 | 13 jun 03:30 |

Los timeouts en runs 22 y 23 son anteriores al cutover UTC y no están relacionados con la migración. El Destroyer es capturado por el watchdog y genera alerta Telegram cuando se cuelga >20 minutos.

---

## 4. Alertas Telegram para timeout del Destroyer ✅ (ya existía)

**Verificación:** `/opt/destroyer/watchdog.py` ya implementa detección de Destroyer colgado:

```python
# watchdog.py — ya implementado
if elapsed > 20 * 60:  # >20 minutos sin avance
    send_telegram("🚨 Destroyer colgado — terminado")
    kill(pid)
```

Las corridas con `status = 'timeout'` en la DB confirman que el watchdog está activo (runs 22 y 23 fueron terminados por él). No requirió cambios.

---

## 5. WORKERS hardcodeado → variable de entorno ✅

**Problema:** `/opt/destroyer/launcher.py` tenía `WORKERS='32'` hardcodeado en el script cloud-init que se inyecta al droplet c-16 del Destroyer. No se podía cambiar sin editar el código.

**Solución aplicada:**

```python
# ANTES (hardcodeado):
f"export WORKERS='32'\n"

# DESPUÉS (configurable via env):
_workers = os.environ.get("DESTROYER_WORKERS", "32")
...
f"export WORKERS='{_workers}'\n"
```

El default sigue siendo 32 (sin cambios en producción). Para cambiar el número de workers sin tocar el código:
```bash
# En el servidor mediaDEV:
echo 'DESTROYER_WORKERS=48' >> /etc/mediadev.env
# y recargar el servicio que ejecuta launcher.py
```

**Verificación:**
```bash
python3 -m py_compile /opt/destroyer/launcher.py
# → sin errores (exit 0)
```

---

## Resumen de cambios

| # | Mejora | Archivo(s) modificado(s) | Estado |
|---|---|---|---|
| 1 | Credenciales → EnvironmentFile | `/etc/publiaudit-api.env`, `/etc/systemd/system/publiaudit-api.service` | ✅ |
| 2 | CORS → env var | `/opt/publiaudit-api/main.py` | ✅ |
| 3 | Cutover UTC | Backend completo (stream-daemon, Destroyer, API) | ✅ |
| 4 | Alertas Telegram Destroyer | `/opt/destroyer/watchdog.py` | ✅ (ya existía) |
| 5 | WORKERS → env var | `/opt/destroyer/launcher.py` | ✅ |

### Pendiente

- **Snapshot nuevo del Destroyer** — crear snapshot v9 del droplet c-16 con el fix de `libx264 ultrafast` para clips de TV (bug en `worker.py` donde `-c:v copy` causaba offset de timing). El snapshot actual (v8 / ID 232634281) aún tiene el bug.
- **Primera fila utc_v2** — confirmar que la próxima corrida del Destroyer genera detecciones con `pipeline_version = 'utc_v2'`.

---

*MediaDEV · Servidor: 159.223.104.91 · 13 junio 2026*
