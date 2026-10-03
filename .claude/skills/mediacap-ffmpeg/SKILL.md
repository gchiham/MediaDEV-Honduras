---
name: mediacap-ffmpeg
description: Diagnóstico y cambios de la captura ffmpeg de mediaCAP (stream_daemon, HLS en vivo, radios Icecast, TV, gateways SOCKS5). Úsalo siempre que una estación "no graba", "no se escucha", graba de más/duplicado, haya load alto o ffmpeg duplicados, disco lleno, alerta de cobertura baja, al dar de alta o cambiar la URL de una estación, o antes de tocar banderas de ffmpeg (_RECONNECT, _hls_args, -c:a, hls_live_restart, ffmpeg_extra) — aunque el usuario no diga "ffmpeg".
---

# mediacap-ffmpeg — captura en vivo de mediaCAP

Nodo: **mediaCAP `159.223.104.91`** (2 vCPU / 4 GB). Código desplegado en `/opt/media-ai`.
Todo lo que corras ahí compite con la grabación: pruebas cortas (`-t 30`), nada de glob masivo,
nada de re-encodes largos en el nodo.

## 0. Verdades que no son obvias (leer siempre)

1. **Quien lanza ffmpeg es `stream_daemon.py`**, con `subprocess.Popen` + `os.setsid` en
   `spawn_stream`. **Supervisor NO maneja los streams** (sus conf son `.bak`; `supervisorctl status`
   sale vacío) y `scripts/stream_run.sh` ya no se usa. El CLAUDE.md raíz está desactualizado en eso.
2. **La config real sale de la DB**: `capture_config JOIN media_sources` (`load_config_from_db()`).
   `config/stations.json` y `stream_catalog.stream_url` están **congelados y mienten** (solo
   `stations.json` sigue valiendo para la definición de gateways). Una bandera que no esté en ese
   `SELECT` **no existe**, aunque el código haga `cfg.get(...)` → cae al default en silencio.
   Para saber qué corre de verdad: `tr '\0' ' ' < /proc/<pid>/cmdline`.
3. El daemon **relee `capture_config` cada 300 s** y hot-addea/quita streams sin restart.
   `is_enabled=false` es el interruptor maestro: el daemon mata el stream aunque el origen esté sano.
4. **La copia local `daemon/stream_daemon.py` de este repo está atrasada** respecto a prod (le
   falta el `killpg` de `spawn_stream`, el path resolver/streamlink, `ffmpeg_extra`, `-c:a copy`
   por estación). Antes de proponer un cambio, leé el archivo desplegado en `/opt/media-ai/daemon/`.
5. El **circuit breaker vive en memoria** del daemon (`CB_FAIL_OPEN=8`, backoff 5→10→20→60 min).
   PG es espejo: no se resetea por SQL. Al cambiar una URL con el CB abierto, contá ~10 min extra.
6. Antes/después de tocar prod: protocolo `CHANGES.log` (PLAN/DONE/FAILED) en `/opt/media-ai/CHANGES.log`.

## 1. Los caminos de captura (qué comando arma el daemon)

| Tipo de origen | Pipeline | Salida |
|---|---|---|
| Radio Icecast/Shoutcast (URL sin `.m3u8`) | `curl -s --retry 0 -L [--socks5-hostname] URL \| ffmpeg -i pipe:0` | AAC 64k mono 22050 → HLS |
| Radio HLS (`.m3u8`) | `ffmpeg [-http_proxy privoxy] _RECONNECT -i URL` | AAC 64k mono 22050 → HLS |
| TV (`media_type=tv`) | `ffmpeg [-http_proxy] _RECONNECT -i URL -c:v copy -c:a aac 128k` | HLS con video |
| Página web (Dailymotion, kick, mdstrm, tnh) | resolver `streamlink → pipe → ffmpeg` | HLS |

- HLS local: `/var/www/streams/<sid>/index.m3u8` + `seg_%05d.ts`, `-hls_time 4 -hls_list_size 10`.
- stderr por stream: `/var/log/streams/ffmpeg/<sid>.err`.
- `route=socks5` → `-http_proxy http://127.0.0.1:<privoxy>` (ffmpeg) o `--socks5-hostname` (curl).
  CDNs globales (streamtheworld, etc.) van `route=direct`.
- Radios: el MP3 horario lo hace `do_record` concatenando los `.ts` (viven 8 h, `KEEP_SEG_HOURS`),
  con auto-backfill de 3 h deduplicado **contra S3**. TV: lo sube `video_segment_uploader.py`.
- Audio de primera generación (`-c:a copy`) para estaciones donde el doble AAC dañaba el
  fingerprint (teleceiba, canal_5). El resto re-encoda.

## 2. Reglas de banderas (aprendidas rompiendo cosas)

| Regla | Por qué |
|---|---|
| **HLS: nada de `-reconnect_at_eof 1` ni `-reconnect_on_http_error`** | En playlists de ventana corta (3 segs/9 s) el fin normal del playlist se trata como error → bucle de reconexión → el segmento expira → 502 → crash-loop. `on_http_error` reintenta para siempre un segmento muerto. |
| **No `-live_start_index -1`** | Lo sienta en el borde y los segmentos expiran antes de bajarlos. |
| **`--hls-live-restart` solo sin DVR largo** | Con DVR de horas (Dailymotion/canal_10) re-baja todo el buffer en cada reconexión: archivos de "1 h" con 4 h adentro. Bandera por estación `capture_config.hls_live_restart` (canal_10 = `false`). |
| **Flag nueva = columna en `capture_config` + en el `SELECT`** | Si no, queda inerte (pasó 3 días con canal_10). |
| **No subir `curl --retry` global** | Toca todas las radios Icecast; medir por estación primero. |
| **tvprem: usar `.ts`, nunca `.m3u8`** | `.m3u8` → "Failed to reload playlist 0" en crash-loop. canal_5 = `http://tvprem.pro:8080/live/<user>/<pass>/2700.ts`, `route=direct`. |
| **Origen RPi/`-listen 1`: un solo cliente** | Cualquier `curl`/`nc` de prueba roba la conexión y tumba la captura. `http=000` ahí puede ser "sano y ocupado". |
| **Nunca re-encodear+sobrescribir un archivo en S3 con uno más chico** | El backfill solo-local truncó horas buenas. Subir solo si el nuevo es mayor. |

Valores actuales en el daemon (referencia, verificá en prod): `_RECONNECT = -reconnect 1
-reconnect_at_eof 0 -reconnect_streamed 1 -reconnect_delay_max 8 -rw_timeout 20000000 -timeout 15000000`,
`STALE_SECS=45`. No bajar intervalos del daemon (`INTERVAL_HEALTH=15`, etc.) sin justificar: 2 vCPU.

## 3. Playbook "X no graba" (en este orden)

```bash
df -h /                                                    # 1. disco PRIMERO: todo "UP" y aun así no graba
systemctl show stream-daemon -p ActiveState -p ExecMainStartTimestamp -p NRestarts
systemctl show video-segment-uploader -p NRestarts         # TV: contador alto = crash-loop
pgrep -af "ffmpeg.*<sid>"                                   # 2. ¿hay 1 ffmpeg? ¿0? ¿varios?
ls -la --time-style=+%T /var/www/streams/<sid>/index.m3u8   # 3. frescura del HLS (> 45 s = stale)
tail -50 /var/log/streams/ffmpeg/<sid>.err                  # 4. 502/404 vs h264 corrupto vs timeout vs 407
cat /etc/mediadev/gateway.conf                              # 5. si es socks5: gateway activo
```

Después aislar el lado culpable:
- **Comparar con otra estación del mismo tipo** en el mismo nodo/gateway. Si la otra graba bien,
  el problema es el origen o su ruta, no mediaCAP.
- **Probar el origen** (solo si no es de un cliente único y no rompe un cupo de conexiones):
  `curl -sv -o /dev/null --max-time 10 URL` → mirar código y certificado TLS. Si es socks5,
  probar a través del proxy y medir **throughput de un segmento**, no solo el m3u8: el
  `health_engine` solo mide alcanzabilidad del m3u8 y no hace failover por gateway lento.
- **Abrir el player oficial de la estación.** Si dice "fuera de línea", no hay nada que arreglar
  de nuestro lado (caso radio_globo: migró de proveedor; el fix es actualizar `capture_config`).

Leer el `.err`:

| Síntoma | Lectura |
|---|---|
| 502 / segmento expirado en HLS | banderas de reconnect o gateway lento |
| `non-existing PPS 0 referenced`, h264 corrupto | descargas truncadas: origen lento (o ffmpeg duplicados pisándose) |
| `Failed to reload playlist` en tvprem | están usando `.m3u8`; pasar a `.ts` |
| **HTTP 407 desde tvprem** | **exceso de conexiones** (cupo efectivo 2), no credenciales |
| HTTP 451 | fuente bloqueada legalmente, está muerta |
| CB ciclando OPEN/CLOSE durante horas | origen caído (canal_6: apagones de 60 h de `video.dataserv.cc`) |
| DTS no monotónico en mpegts de RPi/IPTV | normal, ignorar |

## 4. "Graba, pero no se escucha" — la cobertura es ciega al silencio

`recording_coverage` cuenta segundos decodificables: un origen mudo figura al **100 %**.
Tres estados, no dos: caído (4xx/5xx), **mudo (200 + bytes + −91 dB)**, sano.

```bash
ffmpeg -hide_banner -i <archivo_o_url> -t 30 -af volumedetect -f null - 2>&1 | grep -E "mean_volume|max_volume"
```

| | mean_volume |
|---|---|
| radios sanas | −14 a −16 dB |
| canal_11 (TV, bajo pero audible) | −37 dB |
| canal_5 vía RPi sano | −26 dB |
| silencio digital | **−91.0 dB** |

- Umbral: > −60 dB hay audio. **Correr siempre un control** contra una estación sana en la misma corrida.
- `volumedetect` escribe en nivel *info*: con `-v error` no imprime nada (parece "sin audio").
- **Para borrar o declarar mudo un archivo, medí la hora COMPLETA y con 4 pruebas** que coincidan:
  `volumedetect` (mean y max < −80), `astats` (Peak < −80), `silencedetect noise=-80dB:d=2`
  (≥ 99 % silencio), `ffprobe` bitrate de audio (< 20 kbps). Muestrear el inicio dio 42 falsos
  mudos de 196 (arrancaban en silencio y tenían picos de −0.6 dB después).
- **No clasificar por tamaño de archivo**: otras fuentes/bitrates pesan poco y tienen audio.

## 5. Grabó de más / duplicado

- Duración real con `ffprobe -v error -show_entries format=duration -of csv=p=0 f.ts`: una hora
  normal da **~3500–3550 s**; `> 4000 s` = runaway (DVR, ver canal_10).
- Epochs de segmentos que avanzan más lento que su duración, volumen diario ~2× sus pares,
  `ts_seconds > 3700` en detecciones → misma clase de bug.
- Disco: `du -sh /var/www/streams/_tv_audio/*` — una hora de audio TV pesa ~50–130 MB; en GB es
  el self-concat del uploader (ya mitigado con `CONCAT_MAX_RATIO`; si reaparece, revisar el glob
  de `flush_audio_hour`, que solo debe tomar `{epoch}.ts`).

## 6. Load alto / ffmpeg duplicados

Normal = **1 ffmpeg por stream activo** (contar con `grep -oE` da 2 por stream porque la cmdline
nombra el path dos veces: no es duplicado).

```bash
pgrep -c ffmpeg
ps -eo pid,ppid,etimes,args | grep "[f]fmpeg" | grep -oE "/var/www/streams/[^/]+" | sort | uniq -c   # dividir entre 2
ps -eo pid,ppid,args | awk '$2==1 && /ffmpeg/'          # huérfanos reparentados a init
```

- Matar solo duplicados y huérfanos que escriben a `/var/www/streams` (nunca el grabador MP3).
  Huérfanos: `ps -eo pid,ppid,args | awk '$2==1 && /ffmpeg/ && /\/var\/www\/streams/{print $1}' | xargs -r kill -TERM`.
  Por stream, usar un for-loop con PIDs explícitos (xargs en heredoc anidado falló).
- Nunca `pkill -f` con un patrón que también matchee tu propia sesión SSH.
- Red de seguridad: `mediadev-ffmpeg-reaper.timer` (cada 5 min deja 1 por stream). **Si el reaper
  empieza a matar seguido, el daemon está apilando procesos de nuevo** → revisar que `spawn_stream`
  haga `killpg` del viejo (SIGTERM → 3 s → SIGKILL) antes de spawnear.
- Firma en métricas de DO: `private inbound` cae y `public inbound` sube = CPU interna, no red.

## 7. Alerta "COBERTURA BAJA DE GRABACIÓN"

- **Todas las estaciones bajas en una sola hora** → restart del daemon a media hora (deploy,
  `gateway_switch`, unattended-upgrades ~06:25 CST). Verificar `ExecMainStartTimestamp`
  (`NRestarts=0` = manual) y `CHANGES.log`. No requiere acción.
- **Una estación baja, recurrente** → problema real de ese stream: ir al playbook §3.

## 8. Alta o cambio de URL de una estación

1. Probar el origen fuera del daemon: código HTTP, cert, `ffprobe` de codecs, y **`volumedetect`**
   (que no sea un encoder conectado inyectando silencio, como los Shoutcast de Lempira).
2. Decidir `route` (socks5 si es geo-restringido, direct si es CDN global) y si el origen tiene
   DVR largo (`hls_live_restart=false`).
3. Cambiar **solo `capture_config`** (nunca `stations.json`). El daemon lo toma en ≤ 300 s.
4. Validar: 1 ffmpeg nuevo, `index.m3u8` fresco, `.err` limpio y nivel de audio en el HLS local.
5. Con fuentes de cupo limitado (tvprem: 2 conexiones, el daemon ya ocupa 1) o de cliente único
   (RPi `-listen 1`), **no probar con el daemon corriendo** sin permiso del usuario.
   `player_api.php` de tvprem no consume conexión y muestra `active_cons`.

Detalle de cada incidente que originó estas reglas: [references/incidentes.md](references/incidentes.md).
