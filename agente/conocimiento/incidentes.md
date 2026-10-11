# Incidentes de captura que originaron las reglas

Una línea de causa y una de lección por caso. Fechas en 2026.

| Fecha | Estación | Síntoma | Causa | Lección |
|---|---|---|---|---|
| 14 jun | teleceiba | no grababa | gateway sin throughput + `-reconnect_at_eof 1` en HLS de ventana de 9 s + origen lento | medir throughput de segmento; sin `reconnect_at_eof`/`on_http_error` en HLS |
| 14 jun | varias radios | ~50 % de horas perdidas | `out.stat()` sin guardia → crash del daemon entero | `try/except` por stream |
| 14 jun | 6 archivos de radio | horas buenas en S3 truncadas | backfill con dedupe solo local sobrescribió S3 | dedupe contra S3; nunca subir un archivo más chico |
| 16 jun | todas | load 96, CPU 100 % | 39 ffmpeg huérfanos (PPID=1) tras un restart masivo | matar huérfanos; KillMode=control-group |
| 29 jun | teleceiba | detección débil | 16 ffmpeg vivos pisando el mismo HLS | matar el grupo del viejo antes de spawnear + reaper |
| 29 jun | teleceiba | fingerprint degradado | doble compresión AAC | `-c:a copy` por estación |
| 30 jun | 13 radios | alerta de cobertura baja | deploy que paró el daemon a media hora | todas en la misma hora = restart, no caída |
| 23 ago | canal_10 | archivos de 1 h con 4 h | `--hls-live-restart` + DVR de horas | bandera por estación |
| 23→26 ago | canal_10 | el fix no surtía efecto | la bandera estaba en `stations.json`, no en el `SELECT` | flag nueva = columna + SELECT |
| 26 ago | TV | "no graba" con todo UP | disco 100 %: el uploader se auto-concatenaba (126 MB → 41.8 GB) tras un restart | mirar el disco primero |
| 15–21 ago | canal_6 | 26 % de horas perdidas | apagones de 60 h y 19 h del origen | CB ciclando horas = origen caído; comparar con otra TV |
| 30 ago | 2 radios | "100 % cobertura" sin audio | Shoutcast con encoder conectado mandando silencio | `volumedetect` en toda alta |
| ago–sep | canal_5 | audio mudo intermitente | capturadora HDMI; falsos mudos al muestrear solo el inicio | medir la hora completa antes de declarar mudo |
| 21 sep | radio_globo | CB OPEN/CLOSE 37 h | la radio migró de proveedor de streaming | probar el origen + player oficial; URL nueva en `capture_config` |
| 1 oct | canal_5 | HTTP 407 y crash-loop | pruebas encimadas sobre el cupo de conexiones del IPTV | 407 = cupo; no probar con el daemon corriendo |

Pendientes conocidos: el health marca DOWN ~2 min tras cada respawn (m3u8 nuevo sin segmentos);
el restart diario de actualizaciones automáticas sigue reiniciando todo cerca de las 06:20 HN.
