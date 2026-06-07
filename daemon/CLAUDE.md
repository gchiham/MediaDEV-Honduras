# Daemon — CLAUDE.md

## Propósito
Proceso Python único que reemplaza 5 cron jobs independientes.
Corre como servicio systemd (`stream-daemon.service`), loop infinito con intervalos conservadores para 1 vCPU.

## Responsabilidades
- **Health check** (cada 15s): verifica m3u8 age + seg count, maneja Circuit Breaker, reinicia streams caídos
- **Indexer** (cada 60s): registra segmentos .ts nuevos en SQLite (solo archivos con mtime >= último run)
- **Metrics** (cada 60s): graba snapshot por minuto en tabla `metrics` (status, segs, bytes)
- **Recordings** (cada 120s): genera MP3 horario concatenando segmentos de la hora anterior
- **Cleanup** (cada 30min): elimina segmentos .ts > 8h y purga métricas > 7 días
- **Daily reset** (cada 1h, trigger por fecha GMT-6): resetea `restart_today` a medianoche local

## Archivos clave
- `stream_daemon.py` — archivo activo
- `stream_daemon.py.bak_utc` — backup pre-cambio de timezone
- `migrate.py` — script de migración ya ejecutado (no re-ejecutar)

## Intervalos críticos — NO reducir sin justificación
```
INTERVAL_HEALTH = 15s   # era 3s — reducirlo causa 97% CPU en 1 vCPU
INTERVAL_INDEX  = 60s   # era 10s — escaneaba 37K archivos por ciclo
INTERVAL_CLEAN  = 1800s # cada 30 minutos
LOOP_SLEEP      = 2s    # era 0.5s
```

## Circuit Breaker
- **CB_FAIL_OPEN = 5** fallos consecutivos → estado OPEN (stream DISABLED)
- **CB_RESET_SECS = 1800** (30 min) → vuelve a CLOSED automáticamente
- Eventos en tabla `events`: CB_OPEN, CB_CLOSE, DOWN, UP, RESTART

## Optimizaciones de performance
1. `m3u8_seg_count()` lee el archivo m3u8 como texto — NO hace glob en disco
2. `sup_statuses()` hace UNA sola llamada supervisorctl para los 12 streams
3. `do_index()` filtra `mtime >= last_run` — NO re-indexa todos los segmentos

## Zona horaria
```python
TGU = timezone(timedelta(hours=-6))  # America/Tegucigalpa GMT-6
# Usado en: nombres de grabaciones MP3, reset diario, NO en almacenamiento
```

## Comandos útiles
```bash
journalctl -u stream-daemon -f                          # Logs en vivo
systemctl restart stream-daemon                          # Reiniciar
# Reset manual de circuit breakers:
python3 -c "import sqlite3; db=sqlite3.connect('/var/www/streams/mediadev.db'); db.execute(\"UPDATE stream_status SET cb_state='CLOSED',cb_fails=0\"); db.commit(); print('OK')"
```

## Pitfalls conocidos
- NO usar glob() en do_health() — causa crash de CPU al 97%
- migrate.py ya corrió — no re-ejecutar
- MP3 parciales: si ffmpeg falla, out.unlink(missing_ok=True) limpia el archivo
