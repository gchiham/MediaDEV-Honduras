Contexto de mediaCAP para interpretar hallazgos (versión 1, 11-oct-2026).

mediaCAP es el nodo de captura 24/7 de MediaDEV (DigitalOcean, 2 vCPU / 4 GB). Graba ~20 radios y
canales de TV de Honduras. Perder captura = perder evidencia de pauta que no se puede recuperar.
Prioridad absoluta: que la captura no se detenga. Recursos escasos: nada pesado en ese nodo.

Componentes (todo en /opt/media-ai salvo indicación):
- stream_daemon.py (stream-daemon.service): lanza 1 ffmpeg por estación; lee la config de la tabla
  capture_config (PostgreSQL), no de stations.json. Circuit breaker de 8 fallos.
- video_segment_uploader.py (video-segment-uploader): sube segmentos de TV y el audio horario a S3.
  El cambio de hora (:00-:05) es el momento de más carga.
- segment_janitor.py (timer): limpieza de segmentos. ffmpeg_reaper.py (timer): mata ffmpeg huérfanos.
- /opt/destroyer en mediaCAP: solo gateway/engine (gateway_api.py, health_engine.py: failover de
  gateways SOCKS5 por WireGuard) está en uso por systemd. El worker del Destroyer (detección de
  anuncios) corre en AWS, no en mediaCAP: un worker.py en mediaCAP es una copia sin uso.
- monitor/monitor.py (mediadev-monitor): alertas de WireGuard por Telegram.
- Vigía (mediacap-vigia-vista) y este auditor (mediacap-auditor-inventario): herramientas de solo lectura.

Repos: gchiham/MediaDEV-Honduras (código de /opt/media-ai), gchiham/destroyer (cap/ = /opt/destroyer
de mediaCAP), gchiham/mediadev-infra (units, cron, nginx, wireguard). Regla del equipo: GitHub es la
fuente de verdad; el deploy es manual por SSH y queda registrado en CHANGES.log.

Incidentes que originaron reglas: ffmpeg huérfanos (load 96); reconnect_at_eof en HLS (Teleceiba no
grababa); --hls-live-restart con DVR (canal_10 a 4x); disco lleno por auto-concatenación del uploader;
token de Telegram en el repo público (ya revocado); banderas de capture_config fuera del SELECT.

Criterio de prioridad: P0 = captura detenida o en riesgo inmediato; P1 = riesgo real para captura,
evidencia o secretos; P2 = deuda que confunde a quien opere o despliegue; P3 = limpieza.
