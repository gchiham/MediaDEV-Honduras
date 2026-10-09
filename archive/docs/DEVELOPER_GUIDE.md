# MediaDEV — Stream Relay Stack
## Guía de Operación para Desarrolladores

**Versión:** 3.0 | **Actualizado:** 2026-06-15

> **Topología — 2 nodos (split 14 jun 2026):** esta guía cubre **mediaCAP** (`159.223.104.91`),
> el nodo de **captura**. El producto SaaS + evidence portal (`media-app`) y la orquestación del
> **Destroyer** (ahora en **AWS**: EventBridge + Lambda + EC2 Spot) viven en **mediaAPP**
> (`137.184.53.234`). Ver `live_mediaDEV.md` para el ecosistema completo.

---

## Tabla de Contenidos

1. [Arquitectura del sistema](#1-arquitectura-del-sistema)
2. [Acceso a los servidores](#2-acceso-a-los-servidores)
3. [Cómo escuchar / reproducir un stream](#3-cómo-escuchar--reproducir-un-stream)
4. [API y dashboards](#4-api-y-dashboards)
5. [Estructura de archivos](#5-estructura-de-archivos)
6. [Cómo agregar una nueva radio o TV](#6-cómo-agregar-una-nueva-radio-o-tv)
7. [Cómo eliminar o deshabilitar una estación](#7-cómo-eliminar-o-deshabilitar-una-estación)
8. [Operación diaria — comandos esenciales](#8-operación-diaria--comandos-esenciales)
9. [Gateways y failover](#9-gateways-y-failover)
10. [Diagnóstico de problemas](#10-diagnóstico-de-problemas)
11. [Reinicio completo del stack](#11-reinicio-completo-del-stack)
12. [Referencia rápida](#12-referencia-rápida)

---

## 1. Arquitectura del sistema

```
[Internet Honduras — ice42, streamtheworld, streamhch, etc.]
              |
              | HTTP/HTTPS (geobloqueado para radios)
              v
[Gateways Honduras — RPi / PC con IP residencial]
  hn01 10.101.0.2 · hn02 10.101.0.5 · hn03 10.101.0.6
  microsocks : <ip>:1080 (SOCKS5) + agente heartbeat
              |
              | WireGuard VPN wg0 (cifrado)
              v
[mediaCAP — DigitalOcean — 159.223.104.91 — 2 vCPU / 4 GB]
  privoxy        127.0.0.1:3128   HTTP proxy → SOCKS5
  13x ffmpeg     /var/www/streams/{id}/  (HLS; radio audio, TV video)
  supervisord    auto-restart de los 13 procesos ffmpeg
  stream-daemon  health + grabación MP3 + espejo de estado a PostgreSQL
  gateway-api +  recibe heartbeats, calcula health score, failover automático
   health-engine
  video-uploader segmentos .ts de TV → S3 (insumo de Destroyer)
  mediadev-monitor  vigila WireGuard, alertas Telegram
  nginx :80      HLS /streams/, catálogo /estaciones/
  MCP            observabilidad para Claude Code (vía SSH)
              |
              | VPC privada nyc1 + DB privada
              v
[mediaAPP — 137.184.53.234 — 2 vCPU / 2 GB]   media-app (SaaS + evidence portal) · chihambot · MCP
[AWS us-east-1]                                Destroyer: EventBridge → Lambda → EC2 Spot c5.4xlarge
[PostgreSQL media-db — DO Managed]            estado, métricas, catálogo, detecciones (compartida)
```

### Por qué esta arquitectura

| Problema | Solución |
|---|---|
| Streams geobloqueados en Honduras | Gateways residenciales hondureños (SOCKS5) |
| ffmpeg no soporta SOCKS5+HTTPS con cabeceras Icecast | curl pipe (ice42) o privoxy (HTTP→SOCKS5) |
| Un gateway puede caer | 3 gateways + failover automático (health-engine) |
| Streams que se cortan | supervisord reinicia ffmpeg; Circuit Breaker evita storms |
| Auditoría y detección de anuncios | segmentos persisten ~8h; video de TV se archiva en S3 |
| mediaCAP debe quedar liviano (2 vCPU) | el producto y el Destroyer (cómputo pesado) corren fuera del nodo de captura |

---

## 2. Acceso a los servidores

```bash
# mediaCAP (captura)
ssh -i ~/.ssh/keySED root@159.223.104.91
# mediaAPP (app/control)
ssh -i ~/.ssh/keySED root@137.184.53.234
# Windows: C:\Users\Sedesol\.ssh\keySED
```

Gateways Honduras: definidos en `config/stations.json`. El gateway **activo** está en
`/etc/mediadev/gateway.conf` y se cambia con `gateway_switch.sh`.

---

## 3. Cómo escuchar / reproducir un stream

Todos los streams están disponibles como **HLS en vivo** en mediaCAP:
```
http://159.223.104.91/streams/{id}/index.m3u8
```

### Streams disponibles (13: 10 radio + 3 TV)

| ID | Nombre | Tipo |
|---|---|---|
| `fm_941` | 94 Su FM | Radio |
| `radio_america` | Radio América | Radio |
| `radio_choluteca` | Radio Choluteca | Radio |
| `radio_el_patio` | Radio El Patio | Radio |
| `radio_globo` | Radio Globo | Radio |
| `radio_satelite` | Radio Satélite | Radio |
| `suave_fm` | Suave FM | Radio |
| `xy_hrn` | XY HRN | Radio |
| `xy_sps` | XY SPS | Radio |
| `xy_tgu` | XY TGU | Radio |
| `hch_tv` | HCH TV | TV (video) |
| `teleceiba` | Teleceiba | TV (video) |
| `canal_11` | Canal 11 | TV (video) |

### Reproducir
```bash
vlc    http://159.223.104.91/streams/xy_hrn/index.m3u8
ffplay http://159.223.104.91/streams/hch_tv/index.m3u8
ffmpeg -i http://159.223.104.91/streams/fm_941/index.m3u8 -t 60 -c copy salida.mp3   # grabar 60s
```

---

## 4. API y dashboards

> **El dashboard viejo (`dashboard_v4.py`) fue eliminado** (14 jun 2026). El servicio
> `dashboard_mediadev` existe pero está **inactivo**; sus endpoints `/api/*` read-only en
> mediaCAP **ya no corren**. El código sigue en `dashboard/` solo como referencia histórica.

- **Producto / API del cliente:** corre en **mediaAPP** (`media-app`, FastAPI) — repo `gchiham/media-app`. Auth JWT, CRUD de anunciantes/campañas/ads, evidence portal.
- **HLS en vivo:** `http://159.223.104.91/streams/{id}/index.m3u8` (nginx, sigue activo).
- **Observabilidad de captura:** vía MCP (`mcp/server.py`, 17 tools) desde Claude Code, no por HTTP.

Para estado de los streams sin HTTP: `supervisorctl status`, `journalctl -u stream-daemon`, o el
MCP (`get_system_status`, `get_workers`, `get_service_health`).

---

## 5. Estructura de archivos

```
/opt/media-ai/                    ← repo git gchiham/MediaDEV-Honduras (working tree real)
├── config/stations.json          ← Estaciones activas + definición de gateways
├── daemon/stream_daemon.py       ← Health + grabación MP3 + espejo a PostgreSQL
├── dashboard/dashboard_v4.py     ← referencia histórica (NO corre)
├── monitor/monitor.py            ← Monitoreo WireGuard + alertas Telegram
├── mcp/server.py                 ← MCP de observabilidad (17 tools)
└── scripts/
    ├── stream_run.sh             ← Runner unificado (lee url/type/route de stations.json)
    ├── video_segment_uploader.py ← TV .ts → S3
    └── gateway_switch.sh         ← Cambia el gateway activo

/var/www/streams/{id}/
├── index.m3u8                    ← Playlist HLS activa (nginx la sirve)
├── seg_NNNNN.ts                  ← Segmentos de 4s (se acumulan ~8h para auditoría)
└── recordings/YYYY-MM-DD_HHh.mp3 ← Grabación horaria de audio

/etc/
├── wireguard/wg0.conf            ← Túnel VPN a los gateways
├── mediadev/gateway.conf         ← Gateway activo (vía gateway_switch.sh)
├── mediadev-s3.env               ← Credenciales AWS S3 (chmod 600)
├── mediadev-db.env               ← Credenciales PostgreSQL (chmod 600)
├── privoxy/config                ← HTTP proxy → SOCKS5
├── nginx/sites-enabled/default   ← Config nginx :80
└── supervisor/conf.d/            ← Config de los 13 procesos ffmpeg
```

> **Persistencia:** ya no se usa SQLite; el estado vive en PostgreSQL (media-db).
> **Segmentos:** se acumulan ~8h para auditoría y para el uploader de video; un cron
> (`*/30`) limpia los `.ts` de radio con más de 120 min (los de TV los conserva más para el uploader).
> **Config operativa** (systemd, supervisor, nginx, wireguard) versionada en `gchiham/mediadev-infra`.

---

## 6. Cómo agregar una nueva radio o TV

### Paso 1 — Probar que la URL funciona desde Honduras
```bash
source /etc/mediadev/gateway.conf   # carga GW_SOCKS5 del gateway activo
curl -I --max-time 10 --socks5-hostname $GW_SOCKS5 https://URL_DEL_STREAM
curl -s --max-time 15 --socks5-hostname $GW_SOCKS5 https://URL_DEL_STREAM \
  | ffmpeg -i pipe:0 -t 10 -c copy /tmp/test.ts && ls -lh /tmp/test.ts
```

### Paso 2 — Agregar a stations.json
```json
{
  "id": "nueva_radio",
  "name": "Nombre de la Emisora",
  "type": "radio",
  "route": "auto",
  "gateway": "hn02",
  "url": "https://URL_DEL_STREAM",
  "enabled": true
}
```
- `"type"`: `radio` (audio) o `tv` (video).
- `"route"`: lo aplica el runner unificado (`stream_run.sh`):
  | valor | comportamiento |
  |---|---|
  | `auto` | prueba directo; si falla usa gateway. **Recomendado** — re-evalúa en cada arranque |
  | `gateway` | siempre por gateway (fuentes geo-bloqueadas fijas como `ice42.securenetsystems.net`) |
  | `direct` | siempre directo (sin gateway, sin fallback) |

> No hay un script por estación: el runner único `stream_run.sh` lee URL, tipo y route de
> `stations.json` y decide captura (curl-pipe para Icecast, ffmpeg para el resto), transporte y salida.

### Paso 3 — Registrar en supervisord
```bash
cat >> /etc/supervisor/conf.d/mediadev_streams.conf << 'CONF'

[program:stream_nueva_radio]
command=/opt/media-ai/scripts/stream_run.sh nueva_radio
autostart=true
autorestart=true
startsecs=5
startretries=999
stopwaitsecs=10
stdout_logfile=/var/log/streams/nueva_radio.log
stderr_logfile=/var/log/streams/nueva_radio.err
environment=HOME="/root"
CONF
```

### Paso 4 — Activar y verificar
```bash
supervisorctl reread && supervisorctl update
supervisorctl status stream_nueva_radio
sleep 20
curl -s http://localhost/streams/nueva_radio/index.m3u8 | head -5
```

> Atajo: el MCP expone `add_stream(...)` que hace los 3 pasos (stations.json + supervisor + daemon).
> Si agregás un canal de TV, recordá excluir sus segmentos del cron de limpieza si querés retención larga.

---

## 7. Cómo eliminar o deshabilitar una estación

```bash
# Deshabilitar temporal:
supervisorctl stop stream_{id}      # reactivar: supervisorctl start stream_{id}

# Eliminar permanente:
supervisorctl stop stream_{id}
nano /etc/supervisor/conf.d/mediadev_streams.conf   # borrar bloque [program:stream_{id}]
supervisorctl reread && supervisorctl update
rm -rf /var/www/streams/{id}/
nano /opt/media-ai/config/stations.json             # quitar la entrada
```

---

## 8. Operación diaria — comandos esenciales

```bash
supervisorctl status                       # estado de los 13 streams
supervisorctl restart stream_xy_hrn        # reiniciar uno
supervisorctl restart all                  # reiniciar todos
tail -f /var/log/streams/xy_hrn.err        # errores de una estación
journalctl -u stream-daemon -f             # health checks, CB, grabaciones

# Segmentos y espacio:
for d in /var/www/streams/*/; do echo "$(basename $d): $(ls $d*.ts 2>/dev/null|wc -l) segs"; done
du -sh /var/www/streams/
```

---

## 9. Gateways y failover

El gateway activo es la **fuente de verdad** en `/etc/mediadev/gateway.conf` (exporta `GW_SOCKS5`).
Los scripts de stream SOCKS5 hacen `source` de ese archivo.

```bash
# Cambiar de gateway manualmente (NO editar gateway.conf a mano):
sudo /opt/media-ai/scripts/gateway_switch.sh hn02
# Actualiza gateway.conf + Privoxy + stations.json y reinicia los streams.

# Failover automático: lo decide health_engine por health score.
journalctl -u mediadev-health-engine -f

# Verificar IP de salida del gateway activo (debe ser hondureña):
curl -s --proxy http://127.0.0.1:3128 https://api.ipify.org

# Verificar el túnel WireGuard:
wg show wg0
```

Gateways (en `config/stations.json`):

| ID | VPN IP | Rol |
|---|---|---|
| `hn01` | 10.101.0.2 | failover (RPi Honduras 01) |
| `hn02` | 10.101.0.5 | PC-LCE |
| `hn03` | 10.101.0.6 | failover (RPi-Levi) |

> El health check del failover mide alcanzabilidad del m3u8, **no throughput de segmentos** — un
> gateway puede pasar el m3u8 (texto) pero no los `.ts` (video pesado). Si TV no graba pero el m3u8
> responde, sospechá throughput del gateway (ver el caso Teleceiba en el historial).

---

## 10. Diagnóstico de problemas

### Un stream está en FATAL/BACKOFF o aparece STALE
```bash
tail -20 /var/log/streams/{id}.err
source /etc/mediadev/gateway.conf
curl -I --max-time 10 --socks5-hostname $GW_SOCKS5 URL_DEL_STREAM
supervisorctl restart stream_{id}
# El stream-daemon también reinicia tras varios fallos (Circuit Breaker, con ventana de gracia 35s).
```

### Todos los streams caen al mismo tiempo
Casi siempre es el gateway / WireGuard:
```bash
wg show wg0
curl -s --proxy http://127.0.0.1:3128 https://api.ipify.org   # ¿sale por Honduras?
sudo /opt/media-ai/scripts/gateway_switch.sh <otro_gateway>
```

### Circuit Breaker abierto (stream DISABLED)
```bash
# Espera 30 min (reset automático) o resetea en PG:
#   UPDATE mediadev_stream_status SET cb_state='CLOSED', cb_fails=0 WHERE stream_id='{id}';
```

### nginx 404 en un stream
```bash
ls /var/www/streams/{id}/        # ¿existe el directorio + index.m3u8?
nginx -t && systemctl reload nginx
```

---

## 11. Reinicio completo del stack (mediaCAP)

```bash
systemctl start wg-quick@wg0           # 1. VPN
systemctl start privoxy                # 2. Bridge HTTP→SOCKS5
systemctl start supervisor             # 3. 13x ffmpeg
systemctl start stream-daemon          # 4. Daemon
systemctl start mediadev-gateway-api   # 5. API heartbeats
systemctl start mediadev-health-engine # 6. Failover
systemctl start mediadev-monitor       # 7. Monitoreo WireGuard
systemctl start video-segment-uploader # 8. Uploader TV→S3
systemctl start nginx                  # 9. Reverse proxy / HLS

# Verificar:
supervisorctl status
systemctl is-active stream-daemon nginx mediadev-health-engine
```

> `dashboard_mediadev` NO se arranca (está inactivo a propósito). El producto corre en mediaAPP
> (`systemctl status media-app` en `137.184.53.234`).

---

## 12. Referencia rápida

### Puertos (mediaCAP)
| Puerto | Servicio | Acceso |
|---|---|---|
| `80` | nginx — HLS, catálogo | Público |
| `3128` | privoxy — HTTP→SOCKS5 | Solo localhost |
| `9000` | gunicorn — dashboard_v4 (**inactivo**) | — |
| `51820/udp` | WireGuard VPN | Solo VPN |
| `22` | SSH | Administración |

### IPs
| Host | IP Pública | IP VPN | Rol |
|---|---|---|---|
| mediaCAP | `159.223.104.91` | `10.101.0.1` | Captura (DO, 2 vCPU/4GB) |
| mediaAPP | `137.184.53.234` | — | App/control (DO, 2 vCPU/2GB) |
| Gateway hn01 | residencial HN | `10.101.0.2` | Salida Honduras (failover) |
| Gateway hn02 | residencial HN | `10.101.0.5` | Salida Honduras (PC-LCE) |
| Gateway hn03 | residencial HN | `10.101.0.6` | Salida Honduras (failover) |

### Servicios systemd (mediaCAP)
```bash
systemctl status stream-daemon mediadev-gateway-api mediadev-health-engine \
                 mediadev-monitor video-segment-uploader nginx privoxy wg-quick@wg0
supervisorctl status   # 13 procesos ffmpeg
```

### Cron (mediaCAP)
```bash
# Único cron operativo — limpieza de segmentos de radio viejos (TV se conserva para el uploader):
*/30 * * * * find /var/www/streams/ -maxdepth 2 -name "seg_*.ts" \
  -not -path "*/hch_tv/*" -not -path "*/teleceiba/*" -not -path "*/canal_11/*" -mmin +120 -delete
```
> El Destroyer ya **no** se dispara por cron local — corre en AWS (EventBridge horario). El launcher
> viejo de DigitalOcean quedó desmantelado (`/opt/destroyer/launcher.py.do-legacy.DISABLED`).

### Rutas clave
| Ruta | Descripción |
|---|---|
| `/opt/media-ai/config/stations.json` | Estaciones + gateways |
| `/opt/media-ai/scripts/stream_run.sh` | Runner unificado de streams |
| `/etc/mediadev/gateway.conf` | Gateway activo (vía gateway_switch.sh) |
| `/etc/mediadev-{s3,db}.env` | Credenciales (chmod 600) |
| `/var/www/streams/{id}/index.m3u8` | Playlist HLS activa |
| `/var/www/streams/{id}/recordings/` | Grabaciones MP3 horarias |
| `/etc/supervisor/conf.d/` | Config de supervisord (13 ffmpeg) |

---

*MediaDEV — Sistema de monitoreo, grabación y auditoría de medios Honduras 24/7 · 2 nodos + Destroyer en AWS*
