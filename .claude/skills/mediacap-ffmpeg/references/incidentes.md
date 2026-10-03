# Incidentes de captura que originaron las reglas

Una línea de causa y una de lección por caso. Fechas en 2026.

| Fecha | Estación | Síntoma | Causa | Lección |
|---|---|---|---|---|
| 14 jun | teleceiba | no grababa | gateway hn02 sin throughput + `-reconnect_at_eof 1` en HLS de ventana de 9 s + origen a 43 KB/s | medir throughput de segmento; sin `reconnect_at_eof`/`on_http_error` en HLS |
| 14 jun | radios de cola | ~50 % de horas perdidas en silencio | `do_record` hacía `out.stat()` sin guardia → crash del daemon entero | `try/except` por stream; alerta si una hora queda sin MP3 |
| 14 jun | 6 archivos de radio | horas buenas en S3 truncadas | auto-backfill con dedupe solo local sobrescribió S3 | dedupe contra S3; nunca subir un archivo más chico |
| 16 jun | todas | load 96, CPU 100 % | 39 ffmpeg huérfanos (PPID=1) tras un restart masivo | matar huérfanos de `/var/www/streams`; KillMode=control-group |
| 29 jun | teleceiba | detección débil (score 357) | 16 ffmpeg vivos pisando el mismo HLS: `spawn_stream` no mataba el viejo colgado | `killpg` del viejo antes de spawnear + reaper cada 5 min |
| 29 jun | teleceiba | fingerprint degradado | doble compresión AAC (`-c:a aac 128k` sobre AAC) | `-c:a copy` por estación |
| 29 jun | hch_tv (Destroyer) | "negative dimensions" | mpegts con contenedor dañado y audio sano; libsndfile falla, ffmpeg no | decodificar con ffmpeg como fallback; duración con ffprobe |
| 30 jun 03Z | 13 radios | alerta cobertura baja | deploy que paró el daemon a media hora | todas en la misma hora = restart, no caída |
| 23 ago | canal_10 | archivos de 1 h con 4 h, duplicados | `--hls-live-restart` + DVR de horas de Dailymotion | bandera por estación |
| 23→26 ago | canal_10 | el fix no surtía efecto | la bandera estaba en `stations.json`, no en el `SELECT` de `capture_config` | flag nueva = columna + SELECT |
| 26 ago | TV | "no graba" con todo UP | disco 100 %: `flush_audio_hour` se auto-concatenaba (126 MB → 41.8 GB) tras restart de unattended-upgrades | `df -h` primero; glob solo `{epoch}.ts`; `CONCAT_MAX_RATIO` |
| 15–21 ago | canal_6 | 26 % de horas perdidas | apagones de 60 h y 19 h del origen `video.dataserv.cc` | CB ciclando horas = origen caído; comparar con otra TV |
| 30 ago | congolon, galaxia_21 | "100 % cobertura" sin audio | Shoutcast con encoder conectado mandando silencio | `volumedetect` en toda alta |
| 23 ago–16 sep | canal_5 (RPi) | audio mudo intermitente | captura HDMI del RPi; 42 falsos mudos al muestrear solo el inicio | medir la hora completa con 4 pruebas antes de borrar |
| 21 sep | radio_globo | CB OPEN/CLOSE 37 h | la radio migró de proveedor (radiosmundiales → Condori Stream) | `curl -sv` + player oficial; actualizar `capture_config` |
| 1 oct | canal_5 (tvprem) | HTTP 407 y crash-loop | pruebas encimadas sobre el cupo de 2 conexiones; endpoint `.m3u8` | 407 = cupo; usar `.ts`; no probar con el daemon corriendo |

Pendientes conocidos (no resueltos): canal_10 graba solo audio pese a `type=tv`; el health
marca DOWN ~2 min tras cada respawn (m3u8 nuevo sin segmentos); unattended-upgrades sigue
reiniciando todo a diario; asimetría de retención de segmentos (canal_5/6/10/tnh solo 2 h).
