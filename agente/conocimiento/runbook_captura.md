# Captura en vivo de mediaCAP — guía de diagnóstico

mediaCAP es un nodo chico (2 vCPU / 4 GB) que solo graba. Todo lo que corre ahí compite con la
grabación. (Versión pública: los proveedores, cupos y credenciales concretos van en las notas
operativas locales, fuera del repo.)

## 0. Verdades que no son obvias

1. **Quien lanza ffmpeg es `stream_daemon.py`** (`spawn_stream`, un proceso por estación).
   Supervisor no maneja los streams.
2. **La config real sale de la DB**: `capture_config JOIN media_sources`. `config/stations.json` y
   `stream_catalog.stream_url` **no se usan para capturar** (stations.json solo da la lista TV al
   uploader). Una bandera que no esté en el `SELECT` del daemon **no existe** aunque el código haga
   `cfg.get(...)`: cae al default en silencio.
3. El daemon **relee `capture_config` cada 300 s** y agrega/quita streams sin restart.
   `is_enabled=false` apaga la estación aunque el origen esté sano.
4. El **circuit breaker vive en memoria** del daemon (8 fallos → OPEN, reset fijo a los 600 s).
   PG es un espejo. Tras cambiar una URL con el CB abierto, cuenta ~10 min extra.
5. Todo cambio en producción queda en `CHANGES.log` (PLAN/DONE/FAILED): mirarlo antes de culpar
   al código.

## 1. Caminos de captura

| Tipo de origen | Pipeline | Salida |
|---|---|---|
| Radio Icecast/Shoutcast | `curl … \| ffmpeg -i pipe:0` | AAC 64k mono → HLS |
| Radio HLS (`.m3u8`) | `ffmpeg [-http_proxy] -i URL` | AAC 64k mono → HLS |
| TV | `ffmpeg [-http_proxy] -i URL -c:v copy -c:a aac 128k` (o `-c:a copy` en algunas) | HLS con video |
| Página web (Dailymotion, kick, etc.) | resolver `streamlink → pipe → ffmpeg` | HLS |

- HLS local: `/var/www/streams/<sid>/index.m3u8` + `seg_NNNNN.ts` (4 s, lista de 10).
- stderr por estación: `/var/log/streams/ffmpeg/<sid>.err`.
- `route=socks5` = sale por el gateway residencial hondureño activo (geo-restricción);
  `route=direct` = CDN global.
- Radios: la hora la arma el daemon concatenando segmentos. TV: el uploader sube video y arma el
  audio horario. Un cron borra los segmentos de radio locales a las 2 h.

## 2. Reglas de banderas (aprendidas rompiendo cosas)

| Regla | Por qué |
|---|---|
| **HLS: nada de `-reconnect_at_eof 1` ni `-reconnect_on_http_error`** | En playlists de ventana corta el fin normal del playlist se trata como error → bucle de reconexión → el segmento expira → 502 → crash-loop. |
| **No `-live_start_index -1`** | Arranca en el borde y los segmentos expiran antes de bajarlos. |
| **`--hls-live-restart` solo sin DVR largo** | Con DVR de horas re-baja todo el buffer en cada reconexión: archivos de "1 h" con 4 h adentro. Bandera por estación (`capture_config.hls_live_restart`). |
| **Flag nueva = columna en `capture_config` + en el `SELECT`** | Si no, queda inerte. |
| **No subir `curl --retry` global** | Toca todas las radios Icecast; medir por estación primero. |
| **Orígenes de cupo limitado o de cliente único: no probarlos con el daemon corriendo** | Una prueba ocupa la conexión del daemon y tumba la captura (en un IPTV de cupo: HTTP 407). `probar_origen` ya los omite. |
| **Nunca sobrescribir un archivo en S3 con uno más chico** | Un backfill local truncó horas buenas. |

## 3. "X no graba" (en este orden)

1. **Disco** primero: con el disco lleno todo figura "UP" y nada graba.
2. ¿Hay **1 ffmpeg** para la estación? ¿0? ¿varios?
3. **Frescura** del `index.m3u8` (> 90 s = stale).
4. **`.err`**: 502/404 vs h264 corrupto vs timeout vs 407.
5. Si es socks5: **gateway activo** y su estado.

Después, aislar el lado culpable:
- **Comparar con otra estación del mismo tipo y la misma ruta.** Si la otra graba bien, el
  problema es el origen o su ruta, no mediaCAP.
- **Probar el origen** midiendo código HTTP, TLS y **throughput de un segmento** (no solo el
  m3u8: el health engine solo mide alcanzabilidad y no hace failover por gateway lento).
- Si el origen responde HTML de error de un nginx/proxy (502) o 404 en la URL de siempre, el
  problema es de la emisora; a veces la emisora **migró de proveedor** y el arreglo es una URL nueva
  en `capture_config` (requiere aprobación).

| Síntoma en `.err` | Lectura |
|---|---|
| 502 / segmento expirado en HLS | origen caído, banderas de reconnect o gateway lento |
| `non-existing PPS 0 referenced`, h264 corrupto | descargas truncadas: origen lento o ffmpeg duplicados pisándose |
| HTTP 407 desde un IPTV | exceso de conexiones sobre su cupo, no credenciales |
| HTTP 451 | fuente bloqueada legalmente: muerta |
| CB ciclando OPEN/CLOSE durante horas | origen caído (hubo apagones de origen de 19 h y 60 h) |
| DTS no monotónico en mpegts de IPTV/capturadoras | normal, ignorar |

## 4. "Graba, pero no se escucha": la cobertura es ciega al silencio

`recording_coverage` cuenta segundos decodificables: un origen mudo figura al **100 %**.
Tres estados, no dos: caído (4xx/5xx), **mudo (200 + bytes + −91 dB)**, sano.

| | mean_volume |
|---|---|
| radios sanas | −14 a −16 dB |
| TV con audio bajo pero audible | ~−37 dB |
| silencio digital | **−91.0 dB** |

- Umbral: > −60 dB hay audio. **Siempre medir un control** (una estación sana) en la misma corrida.
- Si el **origen** también mide −91 dB, la emisora está mandando silencio: no es nuestra captura.
- Para declarar mudo un archivo completo hace falta medir la hora entera con varias pruebas;
  muestrear solo el inicio dio falsos mudos.
- No clasificar por tamaño de archivo: otras fuentes/bitrates pesan poco y tienen audio.
- Un Shoutcast con el encoder conectado puede mandar silencio con "200 OK": medir siempre.

## 5. Grabó de más / disco que crece

- Una hora normal dura **~3500–3550 s**; `> 4000 s` = runaway (DVR largo + live-restart).
- Una hora de audio TV pesa ~50–130 MB. Si una hora (o un archivo) pesa **GB**, es el uploader
  re-concatenándose a sí mismo (pasó tras un restart a media hora: 126 MB → 41.8 GB y disco lleno).

## 6. Load alto / ffmpeg duplicados

- Normal = **1 ffmpeg por estación activa**.
- Varios ffmpeg sobre el mismo HLS = el viejo no murió antes del respawn: se pisan y corrompen
  segmentos. ffmpeg con PPID=1 = huérfanos de un restart. Hay un reaper cada 5 min; si tiene que
  matar seguido, el daemon está apilando procesos.

## 7. Alertas de cobertura o "no graba" en TODAS las estaciones a la vez

- Todas en la misma hora / el mismo minuto → **restart del daemon** (deploy, cambio de gateway o el
  restart diario de actualizaciones automáticas ~06:20 HN, que deja segmentos de 0 bytes y un
  minuto sin grabar). Se ve como "Stopping/Started stream-daemon" en el journal. No requiere acción.
- Una estación baja de forma recurrente → problema real de ese stream (§3).

## 8. Alta o cambio de URL de una estación (lo hace el operador)

1. Probar el origen fuera del daemon: código HTTP, TLS, codecs y `volumedetect`.
2. Decidir `route` y si el origen tiene DVR largo.
3. Cambiar **`capture_config`** (nunca `stations.json` a mano); el daemon lo toma en ≤ 300 s.
4. Validar: 1 ffmpeg nuevo, `index.m3u8` fresco, `.err` limpio, nivel de audio.
