# Scripts de Stream — CLAUDE.md

## Propósito
La captura de los 13 streams la hace un **runner unificado** `stream_run.sh <stream_id>`,
gestionado por supervisord (un `[program:stream_{id}]` por estación, todos invocan el runner).
Captura el stream origen y lo recodifica a HLS: audio para radios, video preservado para TV.

## stream_run.sh — cómo decide
Lee `url`, `type` y `route` de `config/stations.json` y resuelve tres ejes:

1. **Captura**: si la URL es `ice42.securenetsystems.net` (Icecast) → `curl` pipe con headers
   `Icy-MetaData` (ffmpeg no puede mandar esos headers vía proxy). El resto → `ffmpeg -i` directo.
2. **Transporte** (campo `route`):
   - `gateway` → siempre por el gateway (curl `--socks5-hostname $GW_SOCKS5`, o ffmpeg `-http_proxy`).
   - `direct` → siempre conexión directa.
   - `auto` → `probe_direct()` prueba la URL; si responde 2xx va directo, si no usa gateway.
     **Re-evalúa en cada arranque**, así que si bloquean una fuente directa, el reinicio de
     supervisord cae solo al gateway. Este es el mecanismo de fallback.
3. **Salida**: `type=radio` → `-vn -c:a aac -b:a 64k -ac 1 -ar 22050`;
   `type=tv` → `-c:v copy -c:a aac -b:a 128k`.

> `probe_direct()` usa el **código HTTP** (`curl -w %{http_code}`), NO range requests:
> muchos Icecast ignoran `-r` y mandan stream continuo → el range daba falsos negativos.

## route por estación (config/stations.json)
- `gateway` (8): geo-bloqueadas (`ice42.securenetsystems.net`) o sin throughput directo —
  xy_hrn, xy_tgu, xy_sps, radio_satelite, fm_941, suave_fm, radio_choluteca, **teleceiba**.
- `auto` (5): radio_america, radio_globo, radio_el_patio, hch_tv, canal_11 — van directo
  mientras puedan, con fallback automático a gateway.

> `teleceiba` pasó a `gateway` fijo (su origen no entregaba throughput de segmentos por la
> ruta directa). Los 3 TV son hch_tv, teleceiba y canal_11.

## Parámetros ffmpeg
```bash
# Comunes:
-hls_time 4 -hls_list_size 10 -hls_flags append_list   # SIN delete_segments (auditoría 8h)
-hls_segment_filename "$OUT_DIR/seg_%05d.ts"
# Radio: -vn -c:a aac -b:a 64k -ac 1 -ar 22050  (codec antes de -ac)
# TV:    -c:v copy -c:a aac -b:a 128k           (NO -vn — Destroyer necesita el video)
```

## Cambiar gateway — usar gateway_switch.sh (NO editar a mano)
```bash
sudo /opt/media-ai/scripts/gateway_switch.sh <gateway_id>   # ej: hn02
```
Actualiza `/etc/mediadev/gateway.conf` (que leen los scripts SOCKS5), Privoxy y stations.json,
y reinicia los streams. El failover normalmente es automático (health_engine).

## Gateways (config/stations.json)
- `hn01` 10.101.0.2 — RPi Honduras 01 (failover-1)
- `hn02` 10.101.0.5 — PC-LCE (primary, activo)
- `hn03` 10.101.0.6 — RPi-Levi (failover-2)

## Otros scripts
- `video_segment_uploader.py` — sube .ts de TV a S3 (servicio `video-segment-uploader`).
- `gateway_switch.sh` — cambio de gateway (gateway_watchdog.py retirado 27 jun 2026).
- `deploy_peer_b.sh` — configura un peer de respaldo.
- `backup_healthcheck.py` — failover active-active de grabaciones en S3.

## Agregar un stream
1. Añadir entrada a `config/stations.json` (con `route`, normalmente `auto`).
2. Añadir `[program:stream_{id}]` con `command=stream_run.sh {id}` a supervisor.
3. `supervisorctl reread && supervisorctl update`.
No se crea ningún script nuevo — el runner es compartido.

## Supervisord
```bash
supervisorctl status                      # 13 streams
supervisorctl restart all
supervisorctl tail stream_fm_941 stdout   # ver decisión de routing (use_gateway=...)
# Config: /etc/supervisor/conf.d/mediadev_streams.conf
```

## Pitfalls
- NO poner `-vn` en los TV (hch_tv/teleceiba/canal_11) — perdería el video que necesita Destroyer.
- `route=auto` agrega ~5s al arranque (el probe espera respuesta). Es aceptable.
- Si la fuente está caída, el Circuit Breaker (stream-daemon) la deshabilita tras 5 fallos.
