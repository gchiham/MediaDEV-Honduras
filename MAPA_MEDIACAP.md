# Mapa de mediaCAP — empezar aquí

Guía para revisar el nodo de captura de MediaDEV. Describe **lo que corre hoy en producción**
(octubre 2026) y dónde está cada pieza en GitHub. Si algo de este repo contradice este mapa,
manda el mapa: varios documentos viejos describen la arquitectura de junio.

## Qué hace mediaCAP

Graba 24/7 unas 20 estaciones de Honduras (radios y canales de TV), las sirve como HLS local y
sube la grabación horaria a un bucket de objetos. De ahí la toma el Destroyer (detección de
anuncios), que **no** corre en este nodo.

- Droplet DigitalOcean `159.223.104.91`, **2 vCPU / 4 GB**, CPU típica ~70 %. Es el límite
  principal de todo el diseño: no hay margen para procesos pesados.
- Base de datos: PostgreSQL gestionado de DO (`media-db`). El nodo lee la configuración de
  captura y espeja estado y cobertura.
- Streams geo-restringidos: salen por gateways residenciales en Honduras (Raspberry Pi / PC)
  conectados por WireGuard, vía SOCKS5.

## El código está en 3 repos

| Repo | Visibilidad | Qué tiene de mediaCAP |
|---|---|---|
| `gchiham/MediaDEV-Honduras` (este) | público | captura: `stream_daemon`, uploader de video, monitor, scripts operativos |
| `gchiham/destroyer` | privado | carpeta `cap/gateway/`: motor de gateways (`health_engine.py`, `gateway_api.py`) y el agente que corre en cada gateway |
| `gchiham/mediadev-infra` | privado | carpeta `mediacap/`: units de systemd, cron, nginx, privoxy, WireGuard (llaves redactadas), `gateway.conf`, e `INVENTORY.md` |

Los tres son espejo de lo desplegado y fueron verificados byte a byte contra el servidor el
9-oct-2026.

## Qué corre (y desde dónde)

| Proceso | Archivo | Repo | Qué hace |
|---|---|---|---|
| `stream-daemon` | `daemon/stream_daemon.py` | este | Lanza 1 ffmpeg por estación, health check, circuit breaker, arma la hora y la sube al bucket |
| `video-segment-uploader` | `scripts/video_segment_uploader.py` | este | TV: sube segmentos de video y arma el audio horario de TV |
| `mediadev-ffmpeg-reaper.timer` | `scripts/ffmpeg_reaper.py` | este | Cada 5 min mata ffmpeg duplicados/huérfanos (red de seguridad) |
| `mediadev-monitor` | `monitor/monitor.py` | este | Vigila WireGuard y avisa por Telegram |
| `mediadev-logs`, `mediadev-metrics` | `scripts/mediadev_logs.py`, `scripts/mediadev_metrics.py` | este | Envían logs y métricas a CloudWatch |
| cron 21:00 HN | `scripts/tvprem_watch.py` | este | Vigila la cuenta IPTV de tvprem |
| `mediadev-health-engine` | `cap/gateway/engine/health_engine.py` | destroyer | Sondea gateways, elige el activo (failover), alertas de captura |
| `mediadev-gateway-api` | `cap/gateway/engine/gateway_api.py` | destroyer | API de heartbeats de los gateways |
| `wg-quick@wg0`, `privoxy`, `nginx` | config | mediadev-infra | Túnel a gateways, proxy HTTP→SOCKS5, servir HLS |

Definición de cada servicio (usuario, `ExecStart`, variables de entorno): `mediadev-infra/mediacap/systemd/`.

## Flujo de datos

```
capture_config JOIN media_sources (PG)        <- única fuente de verdad de URLs y banderas
        │  releído cada 300 s
        ▼
stream_daemon ── ffmpeg (1 por estación) ──► /var/www/streams/<sid>/seg_N.ts  (HLS, 4 s, se guardan ~8 h)
   │   route=socks5 → curl --socks5 / privoxy → WireGuard → gateway activo (/etc/mediadev/gateway.conf)
   │   route=direct → origen directo (CDNs globales)
   ▼
cada hora: build_hour_ts() une los segmentos → <sid>/YYYY/MM/<hora>Z.ts → bucket
TV: video_segment_uploader sube video + audio horario
```

## Fuente de verdad (importante)

- **Estaciones**: tabla `capture_config` en PostgreSQL (hoy 19 habilitadas: 12 radios y 7 TV).
  Las URLs de `config/stations.json` no se usan para capturar, pero el archivo sigue vivo: el
  uploader de video saca de ahí **la lista de canales TV**. Lo regenera `scripts/sync_streams.py --apply`;
  si se da de alta un TV sin correrlo, se captura y nadie sube el video.
- **Gateway activo**: `/etc/mediadev/gateway.conf` (hoy `hn03`, RPi-Levi). Se cambia solo con
  `scripts/gateway_switch.sh`, que también invoca `health_engine`, y reinicia `stream-daemon`.
- **Bandera nueva de captura**: tiene que existir como columna en `capture_config` **y** en el
  `SELECT` de `load_config_from_db()`; si no, el daemon usa el default sin avisar.

## Secretos (no están en git)

| Archivo | Contenido |
|---|---|
| `/etc/mediadev-db.env` | credenciales PostgreSQL |
| `/etc/mediadev-s3.env` | credenciales del bucket de grabaciones |
| `/etc/mediadev-cw.env` | claves de CloudWatch (logs y métricas) |
| `/etc/mediadev-monitor.env` | token y chat de Telegram del monitor |
| `/opt/destroyer/.env` | config del motor de gateways y alertas |
| `/etc/wireguard/wg0.conf` | llave privada WireGuard (en `mediadev-infra` va redactada) |

## Orden de revisión sugerido

1. `daemon/stream_daemon.py`: `load_config_from_db`, `spawn_stream`, el circuit breaker, `do_record` y `build_hour_ts`.
2. `cap/gateway/engine/health_engine.py` (repo destroyer): sondas, scoring y failover.
3. `scripts/video_segment_uploader.py`.
4. `mediadev-infra/mediacap/systemd/` y `cron/`.
5. Scripts auxiliares (`ffmpeg_reaper`, `tvprem_watch`, `sync_streams`, `gateway_switch.sh`).

## Estado conocido y zonas de riesgo (honesto)

- **Deploy manual**: no hay CI ni pipeline. Los cambios se hacen por SSH directo en `/opt/media-ai`
  y se registran en `/opt/media-ai/CHANGES.log` (fuera de git). Por eso el repo estuvo atrasado
  hasta el 9-oct.
- **CPU al límite** (~70 % en 2 vCPU). Probamos pasar 9 radios a `-c:a copy` (bajó a ~47 %) y se
  revirtió para validarlo junto con el Destroyer; el mecanismo quedó en `AUDIO_COPY_SIDS` (vacío).
- **Reinicios del daemon dejan segmentos de 0 bytes y reinician el PTS**. Antes eso truncaba la
  hora; `build_hour_ts` (v2, 8-oct) lo corrige y ya funcionó en vivo, pero es código nuevo.
- **Circuit breaker en memoria** (8 fallos → OPEN, reset fijo a los 10 min): un reinicio lo resetea; PG solo es espejo.
- **Retención local real ~2 h para radios**: un cron de root borra `seg_*.ts` de más de 120 min
  (excepto hch_tv/teleceiba/canal_11), por encima de los 8 h que dice el daemon.
- **AWS suspendida desde el 6-oct**: CloudWatch (logs y métricas) falla con
  `InvalidClientTokenId`; el bucket usa DO Spaces como interino; el Destroyer no está corriendo.
- **Historial de git**: contiene dos tokens de Telegram y una contraseña IPTV antiguos. Los tokens
  están revocados y la contraseña ya no es la vigente; no se reescribió el historial.
- **Restos en el servidor**: un PostgreSQL 16 local con una `destroyer_db` de 8 MB (no se usa) y
  `supervisor` activo sin programas.
- **Código con deuda conocida**: `monitor/telegram_bot.py` tiene un `SyntaxError` y no corre.
  Las tools de acción del MCP (`restart_stream`, `add_stream`, `update_stream`) siguen usando
  supervisor, `stations.json` y `stream_run.sh`, y **no funcionan** (ver `mcp/README.md`).

## Qué no es parte de mediaCAP

- `archive/`: código y documentos que ya no corren (dashboard eliminado el 14-jun, scripts de
  migración de junio, deploy y release del Destroyer viejo, guías de junio). Se conservan como
  referencia histórica.
- mediaAPP (`137.184.53.234`), el producto SaaS (`gchiham/media-app`) y el Destroyer en AWS
  (`gchiham/destroyer`, carpeta `app/`) son otro nodo y otra revisión.
