# Scripts — CLAUDE.md

> Verificado contra `/opt/media-ai/scripts/` desplegado (9-oct-2026).

**La captura no vive aquí.** Los ffmpeg los lanza `daemon/stream_daemon.py` (ver
`daemon/CLAUDE.md`) con la config de `capture_config` en PG. Supervisor sigue activo pero sin
programas.

## Qué corre y cómo
| Script | Cómo corre | Qué hace |
|---|---|---|
| `video_segment_uploader.py` | servicio `video-segment-uploader` | TV: sube `seg_*.ts` a `video_segments/<sid>/YYYY/MM/DD/<epoch_ini>_<epoch_fin>.ts` y los borra del disco. Además arma el audio horario de TV |
| `ffmpeg_reaper.py` | `mediadev-ffmpeg-reaper.timer` (cada 5 min) | Si hay más de 1 ffmpeg escribiendo a `/var/www/streams/<sid>/`, deja el más nuevo |
| `mediadev_logs.py` | servicio `mediadev-logs` | Journal y logs → CloudWatch Logs (claves en `/etc/mediadev-cw.env`) |
| `mediadev_metrics.py` | servicio `mediadev-metrics` | Métricas de host/procesos → CloudWatch (`MediaDEV`) |
| `tvprem_watch.py` | cron 21:00 HN (`/etc/cron.d/tvprem-watch`) | Consulta `player_api.php` de tvprem (no usa cupo de conexión). Avisa por Telegram si la cuenta vence o cambian los IDs. Nunca toca `capture_config` |
| `gateway_switch.sh <id>` | manual o desde `health_engine` (failover) | Cambia el gateway activo (ver abajo) |
| `sync_streams.py [--apply]` | manual | Regenera `stations.json` y `stream_catalog` desde `capture_config` (sin `--apply` es dry-run) |
| `analyze_reencoding.py` | manual | Detecta fuentes re-encodeadas por corte espectral |
| `stream_run.sh` | **no corre** | Runner legado de supervisor. Se conserva solo porque lo referencian las tools de acción de `mcp/` (rotas, ver `mcp/README.md`) |

Con AWS suspendida (desde el 6-oct), `mediadev-logs` y `mediadev-metrics` fallan con `InvalidClientTokenId`.

## video_segment_uploader.py — detalles que importan
- **La lista de canales TV la lee de `config/stations.json`** (`type=tv` y `enabled`), no de
  `capture_config`. Al dar de alta o baja un canal TV hay que correr `sync_streams.py --apply`; el uploader
  relee `stations.json` en cada ciclo, así que no hace falta reiniciarlo. Si no se corre el sync, el daemon captura el canal y nadie sube el video.
- Cada 15 s (`SCAN_INTERVAL`) sube todos los segmentos salvo los últimos `HLS_KEEP = 12`. El
  epoch sale de `mtime`. Los segmentos inválidos van a `/var/www/streams/_invalid/<sid>/`.
  En prod `VIDEO_VALIDATE_FFPROBE=0`.
- Audio TV: por cada segmento extrae el audio (`-vn -c:a copy`) a
  `/var/www/streams/_tv_audio/<sid>/<hora>/<epoch>.ts`. Al cambiar de hora, `flush_audio_hour`
  concatena y sube `<sid>/YYYY/MM/<hora>.ts` más `<hora>.manifest.json`, que mapea la posición
  acumulada al epoch real y corrige el drift audio/video.
- Guardas: solo concatena archivos `{epoch}.ts` y aborta si la salida pesa más de
  `CONCAT_MAX_RATIO = 3` veces la entrada. Así se cortó el self-concat que llenó el disco el 26-ago.
- Registra en `recording_coverage` (audio y video) y en `s3_scan_log`.

## gateway_switch.sh
```bash
sudo /opt/media-ai/scripts/gateway_switch.sh <gateway_id>   # hn01 | hn02 | hn03
```
1. Reescribe `/etc/mediadev/gateway.conf` (fuente de verdad: `GW_ACTIVE_ID`, `GW_SOCKS5`).
2. Reconfigura y recarga Privoxy.
3. **No** toca `stations.json`: el estado de gateways vive en la DB y en `gateway.conf`.
4. `systemctl restart stream-daemon` para que los streams socks5 tomen el nuevo gateway.

El failover normal lo decide `health_engine` (repo `destroyer`, `cap/gateway/engine/`). No
editar `gateway.conf` a mano. Gateway activo hoy: `hn03` (RPi-Levi, `10.101.0.6`).

## Cron de root (no está en este repo)
```
*/30 * * * * find /var/www/streams/ -maxdepth 2 -name "seg_*.ts" -not -path "*/hch_tv/*" \
             -not -path "*/teleceiba/*" -not -path "*/canal_11/*" -mmin +120 -delete
```
Este cron manda más que `KEEP_SEG_HOURS = 8` del daemon: los segmentos de radio viven unas 2 h.

## Agregar o cambiar una estación
1. Probar el origen y decidir `route` (`socks5` si es geo-restringido, `direct` si es CDN).
2. Insertar o editar en `media_sources` + `capture_config`. El daemon la toma en ≤ 300 s.
3. Si es **TV**: `python3 scripts/sync_streams.py --apply` (el uploader saca de `stations.json` la lista de canales TV).
4. Validar: 1 ffmpeg nuevo, `index.m3u8` fresco y `.err` limpio. Ver el skill `mediacap-ffmpeg`.
