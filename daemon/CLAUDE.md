# Daemon — CLAUDE.md

> Verificado contra `/opt/media-ai/daemon/stream_daemon.py` desplegado (9-oct-2026).

## Propósito
`stream_daemon.py` (servicio `stream-daemon`) es **el dueño de la captura**: lee la config de
la DB, lanza un ffmpeg por estación (`subprocess.Popen` + `os.setsid`), vigila su salud, arma la
hora de audio de las radios y la sube al bucket. Supervisor ya no lanza streams y
`scripts/stream_run.sh` no se usa.
El estado operativo vive **en memoria**. PostgreSQL (media-db) es solo un espejo, y si se cae
el daemon sigue grabando.

## Config de streams
- `load_config_from_db()`: `capture_config JOIN media_sources` con `is_enabled = true` y
  `lifecycle_status = 'active'`. Columnas leídas: `stream_url, route, mp3_s3_prefix,
  ts_s3_prefix, ffmpeg_extra, hls_live_restart, fallback_url`. **Una bandera nueva tiene que
  estar en ese `SELECT`**; si no, `cfg.get()` cae al default sin avisar.
- Se relee cada `INTERVAL_CONFIG = 300 s` (`refresh_catalog_state`). Las estaciones nuevas se
  lanzan y las quitadas se detienen, sin reiniciar el daemon.
- Si la DB falla, se usa el cache `/etc/mediadev/stream_config.cache.json`. Si tampoco hay
  cache, la lista sale de `config/stations.json` y, en último caso, de `FALLBACK_STREAMS`.
- El gateway se toma de `/etc/mediadev/gateway.conf` (`GW_SOCKS5`, `GW_PRIVOXY_PORT`) al arrancar
  y en cada refresh de config. Un cambio de gateway igual requiere reiniciar el daemon (lo hace `gateway_switch.sh`).

## Cómo lanza ffmpeg (`spawn_stream`)
| Caso | Pipeline |
|---|---|
| TV con URL de página (`kick.com`, `mdstrm.com`, `dailymotion.com`) | `streamlink [--hls-live-restart] URL <calidad> \| ffmpeg -c:v copy -c:a aac 128k` |
| Radio sin `.m3u8` (Icecast/Shoutcast) | `curl [--socks5-hostname GW] URL \| ffmpeg -vn` + audio radio |
| TV HLS | `ffmpeg [-http_proxy privoxy] _RECONNECT [ffmpeg_extra] -i URL -c:v copy` + audio TV |
| Radio HLS | `ffmpeg [-http_proxy privoxy] _RECONNECT [ffmpeg_extra] -i URL -vn` + audio radio |

- Audio radio: AAC 64k mono 22050 (`AUDIO_COPY_SIDS` permite `-c:a copy`; hoy está vacío, se
  revirtió el 8-oct). Audio TV: AAC 128k estéreo, salvo `teleceiba` y `canal_5`, que van con `-c:a copy`.
- `route = socks5` → privoxy (`-http_proxy`) o `curl --socks5-hostname`; `direct` → sin proxy.
- `hls_live_restart` (default true) solo aplica al camino streamlink. Con DVR largo
  (canal_10) debe ir en `false`: si no, captura a 4x.
- HLS: `-hls_time 4 -hls_list_size 10 -hls_flags append_list+omit_endlist`, con `-start_number`
  = último `seg_N` + 1. Un respawn continúa la numeración y no pisa segmentos de la hora.
- Antes de respawnear se mata **siempre** el grupo de procesos previo (SIGTERM → 3 s → SIGKILL).
  Así se arregló el leak de ffmpeg huérfanos. Red de seguridad extra: `mediadev-ffmpeg-reaper.timer`.
- stderr de cada ffmpeg: `/var/log/streams/ffmpeg/<sid>.err` (se trunca al pasar 20 MB).

## Loop principal (`LOOP_SLEEP = 2 s`)
| Tarea | Intervalo | Qué hace |
|---|---|---|
| `do_health` | 15 s | Refresca la config si toca. Si el proceso murió, lo respawnea. Mira edad y cantidad de segmentos del m3u8, maneja el CB y el failover de URL, y hace UPSERT a `mediadev_stream_status` |
| `do_metrics` | 60 s | Bytes del último minuto por stream → `mediadev_metrics` (único glob periódico) |
| `do_record` | 120 s | Radios: arma las últimas `AUTO_BACKFILL_HOURS = 3` horas, valida, sube y registra |
| `do_cleanup` | 1800 s | Borra `seg_*.ts` > 8 h (radio) / 24 h (TV) y purga métricas (> 7 d) y eventos (> 30 d) |
| `do_daily_reset` | 1 h | Pone `restart_today = 0` al cambiar de día en hora Honduras |

**No reducir intervalos** (2 vCPU). Con health a 3 s la CPU llegó al 97 %.

## Salud, circuit breaker y failover
- Stream OK si `index.m3u8` tiene menos de `STALE_SECS = 90 s` y al menos 1 segmento.
- Fallos: a partir de `RESTART_AFTER_FAILS = 3` reinicia ffmpeg (o hace failover de URL). Con
  `CB_FAIL_OPEN = 8` el CB pasa a OPEN y el stream queda `DISABLED`.
- **CB reset fijo**: `CB_RESET_SECS = 600` (10 min) → CLOSED y reinicio. No hay backoff
  exponencial. El CB vive en memoria y un restart del daemon lo resetea; un `UPDATE` en PG no sirve.
- Tras cada restart, `RESTART_GRACE_SECS = 45` de gracia. El evento `DOWN` se registra solo si la
  caída dura ≥ `DOWN_EVENT_AFTER_SECS = 180`.
- **Failover de URL** (solo con `capture_config.fallback_url`, hoy canal_5/tvprem): alterna entre
  primaria y fallback tras 4 muertes en 300 s o fallos de salud. Hay un hold de 180 s entre
  cambios y después de 4 cambios seguidos sin estabilizar se rinde y alerta. Eventos
  `FAILOVER` / `FAILOVER_EXHAUSTED` y aviso por Telegram.

## Grabación horaria (solo radios; TV la hace `video_segment_uploader.py`)
- `RAW_AUDIO_OFFLOAD = 1` (default): no se recodifica a MP3. `build_hour_ts()` concatena los
  segmentos con `-c copy` y sube `<sid>/YYYY/MM/YYYY-MM-DDTHHZ.ts` (nombre en UTC). El Destroyer
  hace la conversión a MP3. Kill-switch: `RAW_AUDIO_OFFLOAD=0` vuelve al MP3 local con libmp3lame.
- `build_hour_ts` descarta segmentos de 0 bytes y detecta tramos por reinicios (saltos de PTS o
  codec, buscando por bisección). Cada tramo se lleva a PTS 0 y se normaliza a AAC mono 22050.
- Deduplica contra S3: si la hora ya existe (o existe como `.mp3` legacy), no la rehace.
- Si una hora tiene menos de 10 segmentos queda `skipped`. Cada estado se registra en
  `recording_coverage` (`pending/validated/uploaded/upload_failed/invalid/skipped`) y en `s3_scan_log`.
- Se conservan las últimas `KEEP_MP3_COUNT = 8` horas locales. `recover_pending_audio_uploads()`
  reintenta las que quedaron sin subir.
- **Circuit breaker S3**: ante `InvalidAccessKeyId`/`InvalidClientTokenId`/`AccountProblem` omite
  subidas durante `S3_AUTH_BACKOFF = 600 s`, y los archivos quedan en disco. Se agregó el 8-oct
  por la suspensión de AWS.
- Alerta Telegram agregada si la hora anterior quedó con menos de `RECORDING_ALERT_MIN_SECONDS = 900` s.
  Las credenciales de Telegram salen de `TG_ENV_FILE = /opt/destroyer/.env`.

## Persistencia (PG, vía `pg_write()` que nunca lanza)
`mediadev_stream_status`, `mediadev_metrics`, `mediadev_events` (DOWN/UP/CB_OPEN/CB_CLOSE/FAILOVER…),
`recording_coverage`, `s3_scan_log`. Credenciales en `/etc/mediadev-db.env`. Las de S3 están en
`/etc/mediadev-s3.env` (ambos como `EnvironmentFile` del unit).

## Pitfalls
- Nada de glob en `do_health`: solo lee el m3u8 como texto.
- **El cron de root borra `seg_*.ts` de más de 120 min** cada 30 min, salvo `hch_tv`, `teleceiba`
  y `canal_11`. En la práctica las radios guardan unas 2 h, no 8. El backfill de 3 h de
  `do_record` no encuentra segmentos para horas de más de 2 h atrás.
- Reiniciar el daemon a mitad de hora deja tramos. `build_hour_ts` lo maneja, pero la alerta
  de cobertura puede disparar para todas las estaciones a la vez: es el restart, no una caída.
- Cambios: protocolo `CHANGES.log` (PLAN/DONE/FAILED) en `/opt/media-ai/CHANGES.log`.
