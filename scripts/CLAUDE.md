# Scripts de Stream — CLAUDE.md

## Propósito
Un script bash por stream, gestionado por supervisord. Captura el stream origen y lo
recodifica a HLS. Audio para radios; **video preservado para TV**.

## Tres patrones de conexión
Determinados por geo-restricción de la fuente:

### SOCKS5 directo (7): fm_941, radio_choluteca, radio_satelite, suave_fm, xy_hrn, xy_sps, xy_tgu
```bash
source /etc/mediadev/gateway.conf        # fuente de verdad del gateway (GW_SOCKS5)
curl -s --socks5-hostname $GW_SOCKS5 URL | ffmpeg -i pipe:0 ...
```

### Privoxy/HTTP (3): radio_america, radio_el_patio, radio_globo
```bash
ffmpeg -http_proxy http://127.0.0.1:3128 -i URL ...   # Privoxy reenvía a SOCKS5
```

### Directo sin proxy (2): hch_tv, teleceiba
Fuentes NO geo-restringidas (streamhch.com, teleceiba.com) — ffmpeg conecta directo.

## Parámetros ffmpeg
Radios (audio):
```bash
-vn -c:a aac -b:a 64k -ac 1 -ar 22050    # mono 22kHz, codec antes de -ac
```
TV (video preservado, para reconstruir clips de anuncios en S3):
```bash
-c:v copy -c:a aac -b:a 128k             # NO usar -vn en hch_tv / teleceiba
```
Comunes:
```bash
-hls_time 4 -hls_list_size 10 -hls_flags append_list   # sin delete_segments (auditoría 8h)
-hls_segment_filename "$OUT_DIR/seg_%05d.ts"
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
- `gateway_switch.sh` / `gateway_watchdog.py` — cambio y vigilancia de gateway.
- `deploy_peer_b.sh` — configura un peer de respaldo.
- `backup_healthcheck.py` — failover active-active de grabaciones en S3.

## Supervisord
```bash
supervisorctl status                      # 12 streams
supervisorctl restart all
supervisorctl tail stream_fm_941 stderr
# Config: /etc/supervisor/conf.d/
```

## Pitfalls
- Orden de flags ffmpeg de audio: `-c:a aac -b:a 64k -ac 1` (codec antes del canal).
- NO poner `-vn` en hch_tv/teleceiba — perdería el video que necesita Destroyer.
- Si la fuente está caída (404), el Circuit Breaker la deshabilita tras 5 fallos.
