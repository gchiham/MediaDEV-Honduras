# MediaDEV — Stream Relay Stack
## Guía de Operación para Desarrolladores

**Versión:** 2.0 | **Actualizado:** 2026-06-11

---

## Tabla de Contenidos

1. [Arquitectura del sistema](#1-arquitectura-del-sistema)
2. [Acceso a los servidores](#2-acceso-a-los-servidores)
3. [Cómo escuchar / reproducir un stream](#3-cómo-escuchar--reproducir-un-stream)
4. [API REST](#4-api-rest)
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
  hn01 10.101.0.2 · hn02 10.101.0.5 (activo) · hn03 10.101.0.6
  microsocks : <ip>:1080 (SOCKS5) + agente heartbeat
              |
              | WireGuard VPN wg0 (cifrado)
              v
[MediaDEV — DigitalOcean — 159.223.104.91 — 2 vCPU / 4 GB]
  privoxy        127.0.0.1:3128   HTTP proxy → SOCKS5
  12x ffmpeg     /var/www/streams/{id}/  (HLS; radio audio, TV video)
  supervisord    auto-restart de los 12 procesos ffmpeg
  stream-daemon  health + grabación MP3 + espejo de estado a PostgreSQL
  gateway-api +  recibe heartbeats, calcula health score, failover automático
   health-engine
  video-uploader segmentos .ts de TV → S3 (insumo de Destroyer)
  nginx :80      dashboard, catálogo /estaciones/, HLS /streams/
  PostgreSQL     media-db (DO Managed) — estado, métricas, catálogo, detecciones
```

### Por qué esta arquitectura

| Problema | Solución |
|---|---|
| Streams geobloqueados en Honduras | Gateways residenciales hondureños (SOCKS5) |
| ffmpeg no soporta SOCKS5+HTTPS con cabeceras Icecast | curl pipe (ice42) o privoxy (HTTP→SOCKS5) |
| Un gateway puede caer | 3 gateways + failover automático (health-engine) |
| Streams que se cortan | supervisord reinicia ffmpeg; Circuit Breaker evita storms |
| Auditoría y detección de anuncios | segmentos persisten 8h; video de TV se archiva en S3 |
| Estado consultable sin exponer el servidor | dashboard + API REST en nginx :80 |

---

## 2. Acceso a los servidores

### MediaDEV (servidor principal)
```bash
ssh -i ~/.ssh/keySED root@159.223.104.91
# Windows: C:\Users\Sedesol\.ssh\keySED
```

### Gateways Honduras
Definidos en `config/stations.json`. El acceso varía por nodo (RPi vía SSH local, etc.).
El gateway **activo** está en `/etc/mediadev/gateway.conf` y se cambia con `gateway_switch.sh`.

---

## 3. Cómo escuchar / reproducir un stream

Todos los streams están disponibles como **HLS en vivo** en:
```
http://159.223.104.91/streams/{id}/index.m3u8
```

### Streams disponibles (12)

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

### Reproducir
```bash
vlc   http://159.223.104.91/streams/xy_hrn/index.m3u8
ffplay http://159.223.104.91/streams/hch_tv/index.m3u8
# Grabar 60s:
ffmpeg -i http://159.223.104.91/streams/fm_941/index.m3u8 -t 60 -c copy salida.mp3
```

### Dashboards web
- `http://159.223.104.91/` — panel de salud (KPIs + 12 streams).
- `http://159.223.104.91/estaciones/` — catálogo de 195 estaciones con reproductor.

---

## 4. API REST

Endpoints JSON read-only servidos por `dashboard_v4.py` (nginx :80). Sin autenticación.

| Endpoint | Descripción |
|---|---|
| `GET /api/summary` | KPIs globales (streams en vivo, catálogo, detecciones hoy/total) |
| `GET /api/streams` (=`/api/status`) | Estado de salud en vivo de los 12 streams |
| `GET /api/stations` | Catálogo. Filtros `?status=active\|inactive`, `?type=radio\|tv` |
| `GET /api/detections` | Detecciones de anuncios recientes (`?limit=N`, máx 500) |

```bash
curl http://159.223.104.91/api/summary
curl 'http://159.223.104.91/api/stations?type=tv'
curl 'http://159.223.104.91/api/detections?limit=20'
```

Campos de `/api/streams`: `status` (OK/STALE/NO_M3U8/DISABLED), `sup` (estado supervisord),
`segs` (segmentos .ts activos), `age` (seg desde última actualización del m3u8, debe ser < 60),
`cb_state` (CLOSED/OPEN), `restart_today`.

---

## 5. Estructura de archivos

```
/opt/media-ai/
├── config/stations.json          ← Estaciones activas + definición de gateways
├── daemon/stream_daemon.py       ← Health + grabación MP3 + espejo a PostgreSQL
├── dashboard/dashboard_v4.py     ← Dashboard web + API JSON (lee de PostgreSQL)
├── monitor/monitor.py            ← Monitoreo WireGuard + alertas Telegram
└── scripts/
    ├── stream_run.sh            ← Runner unificado (lee url/type/route de stations.json)
    ├── video_segment_uploader.py ← TV .ts → S3
    ├── gateway_switch.sh         ← Cambia el gateway activo
    └── gateway_watchdog.py       ← Watchdog del gateway (cron cada minuto)

/var/www/streams/{id}/
├── index.m3u8                    ← Playlist HLS activa (nginx la sirve)
├── seg_NNNNN.ts                  ← Segmentos de 4s (se acumulan 8h para auditoría)
└── recordings/YYYY-MM-DD_HHh.mp3 ← Grabación horaria de audio (GMT-6)

/var/www/html/estaciones/index.html  ← Dashboard de catálogo

/etc/
├── wireguard/wg0.conf            ← Túnel VPN a los gateways
├── mediadev/gateway.conf         ← Gateway activo (vía gateway_switch.sh)
├── mediadev-s3.env               ← Credenciales AWS S3 (chmod 600)
├── mediadev-db.env               ← Credenciales PostgreSQL (chmod 600)
├── privoxy/config                ← HTTP proxy → SOCKS5
├── nginx/sites-enabled/default   ← Config nginx :80
└── supervisor/conf.d/            ← Config de los 12 procesos ffmpeg
```

> **Persistencia:** ya no se usa SQLite. El estado vive en PostgreSQL (media-db).
> **Segmentos:** se acumulan ~8h (no se borran por ffmpeg) para auditoría y para el
> uploader de video; el daemon los limpia en el cleanup de 30 min.

---

## 6. Cómo agregar una nueva radio o TV

### Paso 1 — Probar que la URL funciona desde Honduras
```bash
source /etc/mediadev/gateway.conf   # carga GW_SOCKS5 del gateway activo
curl -I --max-time 10 --socks5-hostname $GW_SOCKS5 https://URL_DEL_STREAM
# Grabar 10s de prueba:
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
Campos clave:
- `"type"`: `radio` (audio) o `tv` (video).
- `"route"`: cómo sale el stream — lo aplica el **runner unificado** (`stream_run.sh`):
  | valor | comportamiento |
  |---|---|
  | `auto` | prueba directo; si falla usa gateway. **Recomendado** — re-evalúa en cada arranque, así que si bloquean la fuente, cae solo al gateway |
  | `gateway` | siempre por gateway (úsalo para fuentes geo-bloqueadas fijas como `ice42.securenetsystems.net`) |
  | `direct` | siempre directo (sin gateway, sin fallback) |

> No hay que crear un script por estación: el **runner único** `stream_run.sh` lee la URL,
> el tipo y el route desde `stations.json` y decide captura (curl-pipe para Icecast, ffmpeg
> para el resto), transporte (directo/gateway) y salida (audio/video) automáticamente.

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
# Ver qué transporte eligió (directo o gateway):
supervisorctl tail stream_nueva_radio stdout | grep use_gateway
sleep 20
curl -s http://localhost/streams/nueva_radio/index.m3u8 | head -5
```

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
# (no hay script por estación que borrar — el runner es compartido)
```

---

## 8. Operación diaria — comandos esenciales

```bash
supervisorctl status                       # estado de los 12 streams
supervisorctl restart stream_xy_hrn        # reiniciar uno
supervisorctl restart all                  # reiniciar todos
tail -f /var/log/streams/xy_hrn.err        # errores de una estación
journalctl -u stream-daemon -f             # health checks, CB, grabaciones
curl http://localhost/api/streams          # estado JSON en vivo

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
| `hn01` | 10.101.0.2 | failover-1 (RPi Honduras 01) |
| `hn02` | 10.101.0.5 | **primary** (PC-LCE) |
| `hn03` | 10.101.0.6 | failover-2 (RPi-Levi) |

---

## 10. Diagnóstico de problemas

### Un stream está en FATAL/BACKOFF o aparece STALE
```bash
tail -20 /var/log/streams/{id}.err
source /etc/mediadev/gateway.conf
curl -I --max-time 10 --socks5-hostname $GW_SOCKS5 URL_DEL_STREAM
supervisorctl restart stream_{id}
# El stream-daemon también reinicia automáticamente tras varios fallos (Circuit Breaker).
```

### Todos los streams caen al mismo tiempo
Casi siempre es el gateway / WireGuard:
```bash
wg show wg0
curl -s --proxy http://127.0.0.1:3128 https://api.ipify.org   # ¿sale por Honduras?
# El health_engine debería hacer failover solo. Forzar si hace falta:
sudo /opt/media-ai/scripts/gateway_switch.sh <otro_gateway>
```

### Circuit Breaker abierto (stream DISABLED)
```bash
# Espera 30 min (reset automático) o resetea en PG:
#   UPDATE mediadev_stream_status SET cb_state='CLOSED', cb_fails=0;
```

### nginx 404 en un stream
```bash
ls /var/www/streams/{id}/        # ¿existe el directorio + index.m3u8?
nginx -t && systemctl reload nginx
```

### El dashboard da error
```bash
journalctl -u dashboard-mediadev -n 50
# Verificar que /etc/mediadev-db.env tiene las credenciales PG y que media-db responde.
```

---

## 11. Reinicio completo del stack

```bash
# Todos los servicios tienen systemctl enable. Orden manual si hace falta:
systemctl start wg-quick@wg0           # 1. VPN
systemctl start privoxy                # 2. Bridge HTTP→SOCKS5
systemctl start supervisor             # 3. 12x ffmpeg
systemctl start stream-daemon          # 4. Daemon
systemctl start mediadev-gateway-api   # 5. API heartbeats
systemctl start mediadev-health-engine # 6. Failover
systemctl start mediadev-monitor       # 7. Monitoreo WireGuard
systemctl start video-segment-uploader # 8. Uploader TV→S3
systemctl start dashboard-mediadev     # 9. Dashboard
systemctl start nginx                  # 10. Reverse proxy

# Verificar:
supervisorctl status
systemctl is-active stream-daemon dashboard-mediadev nginx mediadev-health-engine
curl http://localhost/api/summary
```

---

## 12. Referencia rápida

### Puertos
| Puerto | Servicio | Acceso |
|---|---|---|
| `80` | nginx — dashboards, API, HLS | Público |
| `3128` | privoxy — HTTP→SOCKS5 | Solo localhost |
| `9000` | gunicorn — dashboard_v4 (tras nginx) | Solo localhost |
| `51820/udp` | WireGuard VPN | Solo VPN |
| `22` | SSH | Administración |

> El puerto `8080` lo usa otro proyecto (media-app), NO los streams.

### IPs
| Host | IP Pública | IP VPN | Rol |
|---|---|---|---|
| MediaDEV | `159.223.104.91` | `10.101.0.1` | Servidor DigitalOcean (2 vCPU/4GB) |
| Gateway hn01 | residencial HN | `10.101.0.2` | Salida Honduras (failover) |
| Gateway hn02 | residencial HN | `10.101.0.5` | Salida Honduras (activo) |
| Gateway hn03 | residencial HN | `10.101.0.6` | Salida Honduras (failover) |

### Servicios systemd
```bash
systemctl status stream-daemon mediadev-gateway-api mediadev-health-engine \
                 mediadev-monitor video-segment-uploader dashboard-mediadev \
                 nginx privoxy wg-quick@wg0
supervisorctl status   # 12 procesos ffmpeg
```

### Cron
```bash
* * * * *           gateway_watchdog.py          # vigila el gateway
0 0,6,12,18 * * *   /opt/destroyer/launcher.py    # Destroyer (detección de anuncios)
```

### Rutas clave
| Ruta | Descripción |
|---|---|
| `/opt/media-ai/config/stations.json` | Estaciones + gateways |
| `/opt/media-ai/scripts/stream_run.sh` | Runner unificado de streams (todos) |
| `/etc/mediadev/gateway.conf` | Gateway activo (vía gateway_switch.sh) |
| `/etc/mediadev-{s3,db}.env` | Credenciales (chmod 600) |
| `/var/www/streams/{id}/index.m3u8` | Playlist HLS activa |
| `/var/www/streams/{id}/recordings/` | Grabaciones MP3 horarias |
| `/etc/supervisor/conf.d/` | Config de supervisord |

---

*MediaDEV — Sistema de monitoreo, grabación y auditoría de medios Honduras 24/7*
