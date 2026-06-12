# Daemon — CLAUDE.md

## Propósito
Proceso Python único (`stream-daemon.service`) con un loop de mantenimiento. El estado
operativo vive **en memoria** (se recalcula desde el filesystem con `mtime`) y se **espeja a
PostgreSQL (media-db)** para el dashboard. Si PG no está disponible, el daemon sigue operando.

## Responsabilidades
- **Health check** (15s): verifica m3u8 age + seg count, maneja Circuit Breaker, reinicia
  streams caídos, hace UPSERT del estado a `mediadev_stream_status` y registra eventos.
- **Metrics** (60s): snapshot (status, segs, bytes del último minuto) → `mediadev_metrics`.
- **Recordings** (120s): genera MP3 horario concatenando segmentos de la hora anterior + sube a S3.
- **Cleanup** (30min): elimina .ts > 8h, purga `mediadev_metrics` (>7d) y `mediadev_events` (>30d).
- **Daily reset** (1h, por fecha GMT-6): resetea `restart_today` a medianoche local.

## Persistencia
NO usa SQLite. Espeja a PostgreSQL vía `pg_write()` (tolerante a fallos, nunca lanza).
Credenciales en `/etc/mediadev-db.env` (`PG_*`), cargadas por systemd. Las escrituras a PG
son un espejo; la fuente de verdad operativa es el estado en memoria + el filesystem.

## Intervalos críticos — NO reducir sin justificación (2 vCPU)
```
INTERVAL_HEALTH  = 15s   # era 3s — reducirlo causó 97% CPU
INTERVAL_METRICS = 60s
INTERVAL_CLEAN   = 1800s
LOOP_SLEEP       = 2s
```

## Circuit Breaker
- **CB_FAIL_OPEN = 5** fallos consecutivos → OPEN (stream DISABLED).
- **CB_RESET_SECS = 1800** (30 min) → vuelve a CLOSED automáticamente.
- Eventos en `mediadev_events`: DOWN, UP, CB_OPEN, CB_CLOSE.
- Reset manual:
  ```sql
  UPDATE mediadev_stream_status SET cb_state='CLOSED', cb_fails=0;
  ```
  (el daemon recalcula el estado real en el siguiente health check).

## Optimizaciones
1. `m3u8_seg_count()` lee el m3u8 como texto — NO hace glob en disco.
2. `sup_statuses()` hace UNA sola llamada a supervisorctl para los 12 streams.
3. `do_metrics()` es la única tarea que hace glob (1×/min) para medir bytes.

## Pitfalls
- NO usar glob() en do_health() — causa CPU al 97%.
- Las escrituras PG van envueltas en `pg_write()`; no lanzar excepciones que maten el loop.
- MP3 parciales: si ffmpeg falla, `out.unlink(missing_ok=True)` limpia el archivo.
