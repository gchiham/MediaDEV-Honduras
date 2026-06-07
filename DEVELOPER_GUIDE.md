# MediaDEV — Stream Relay Stack
## Guía de Operación para Desarrolladores

**Versión:** 1.0 | **Actualizado:** 2026-06-04

---

## Tabla de Contenidos

1. [Arquitectura del sistema](#1-arquitectura-del-sistema)
2. [Acceso a los servidores](#2-acceso-a-los-servidores)
3. [Cómo escuchar / reproducir un stream](#3-cómo-escuchar--reproducir-un-stream)
4. [Estructura de archivos](#4-estructura-de-archivos)
5. [Cómo agregar una nueva radio o TV](#5-cómo-agregar-una-nueva-radio-o-tv)
6. [Cómo eliminar o deshabilitar una estación](#6-cómo-eliminar-o-deshabilitar-una-estación)
7. [Operación diaria — comandos esenciales](#7-operación-diaria--comandos-esenciales)
8. [Monitoreo y salud del sistema](#8-monitoreo-y-salud-del-sistema)
9. [Diagnóstico de problemas](#9-diagnóstico-de-problemas)
10. [Cómo funciona internamente](#10-cómo-funciona-internamente)
11. [Reinicio completo del stack](#11-reinicio-completo-del-stack)
12. [Referencia rápida](#12-referencia-rápida)

---

## 1. Arquitectura del sistema

```
[Internet Honduras — ice42, streamtheworld, etc.]
              |
              | HTTP/HTTPS (geobloqueado)
              v
[Raspberry Pi — rpitgu — Honduras]
  IP pública : 181.115.19.237
  IP local   : 192.168.4.189
  wg0  10.100.0.2  (túnel existente — NO tocar)
  wg1  10.101.0.2  (túnel MediaDEV)
  microsocks-mediadev : 10.101.0.2:1080  (SOCKS5 proxy)
              |
              | WireGuard VPN wg1 (cifrado extremo a extremo)
              v
[MediaDEV — DigitalOcean — 159.223.104.91]
  privoxy     127.0.0.1:3128   HTTP proxy → SOCKS5 Honduras
  12x ffmpeg  /var/www/streams/{id}/index.m3u8  (HLS en vivo)
  supervisord auto-restart de los 12 procesos ffmpeg
  nginx       localhost:8080   sirve los streams HLS
  health cron cada 2 min verifica y reinicia si hay fallas
```

### Por qué esta arquitectura

| Problema | Solución |
|---|---|
| Streams geobloqueados en Honduras | Raspberry Pi como nodo de salida residencial |
| ffmpeg no soporta SOCKS5+HTTPS | privoxy traduce HTTP→SOCKS5 transparentemente |
| Streams que se cortan | supervisord reinicia el proceso en segundos |
| Consumir streams internamente sin exponer proxies | nginx sirve HLS local |
| Detectar caídas automáticamente | health_monitor.sh corre cada 2 minutos |

---

## 2. Acceso a los servidores

### MediaDEV (servidor principal)
```bash
ssh -i ~/.ssh/keySED root@159.223.104.91
```

### Raspberry Pi Honduras
```bash
ssh -i ~/.ssh/keySED gustavo@192.168.4.189
```

> **Nota:** La llave SSH `keySED` es requerida para ambos servidores.  
> Desde Windows usar la ruta: `C:\Users\Sedesol\.ssh\keySED`

---

## 3. Cómo escuchar / reproducir un stream

Todos los streams están disponibles como **HLS en vivo** en:
```
http://159.223.104.91:8080/{id}/index.m3u8
```

### Streams disponibles

| ID | Nombre | Tipo | URL HLS |
|---|---|---|---|
| `xy_hrn` | XY HRN | Radio | `http://159.223.104.91:8080/xy_hrn/index.m3u8` |
| `xy_tgu` | XY TGU | Radio | `http://159.223.104.91:8080/xy_tgu/index.m3u8` |
| `xy_sps` | XY SPS | Radio | `http://159.223.104.91:8080/xy_sps/index.m3u8` |
| `radio_satelite` | Radio Satélite | Radio | `http://159.223.104.91:8080/radio_satelite/index.m3u8` |
| `fm_941` | 94.1 FM | Radio | `http://159.223.104.91:8080/fm_941/index.m3u8` |
| `suave_fm` | Suave FM | Radio | `http://159.223.104.91:8080/suave_fm/index.m3u8` |
| `radio_america` | Radio América | Radio | `http://159.223.104.91:8080/radio_america/index.m3u8` |
| `radio_globo` | Radio Globo | Radio | `http://159.223.104.91:8080/radio_globo/index.m3u8` |
| `radio_el_patio` | Radio El Patio | Radio | `http://159.223.104.91:8080/radio_el_patio/index.m3u8` |
| `hch_tv` | HCH TV | TV | `http://159.223.104.91:8080/hch_tv/index.m3u8` |
| `teleceiba` | Teleceiba | TV | `http://159.223.104.91:8080/teleceiba/index.m3u8` |
| `radio_choluteca` | Radio Choluteca | Radio | `http://159.223.104.91:8080/radio_choluteca/index.m3u8` |

### Reproducir con VLC (escritorio)
```bash
vlc http://159.223.104.91:8080/xy_hrn/index.m3u8
```

### Reproducir con ffplay (línea de comandos)
```bash
ffplay http://159.223.104.91:8080/hch_tv/index.m3u8
```

### Grabar 60 segundos con ffmpeg
```bash
ffmpeg -i http://159.223.104.91:8080/fm_941/index.m3u8 -t 60 -c copy salida.mp3
```

### Desde código Python
```python
import subprocess

stream_url = "http://159.223.104.91:8080/xy_hrn/index.m3u8"

proc = subprocess.Popen(
    ["ffmpeg", "-i", stream_url, "-f", "mp3", "pipe:1"],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL
)
chunk = proc.stdout.read(4096)   # leer chunk de audio
```

### Desde JavaScript / Node.js
```javascript
// Cualquier cliente HLS: hls.js, video.js, etc.
const streamUrl = 'http://159.223.104.91:8080/xy_hrn/index.m3u8';

// Con hls.js en el browser:
const hls = new Hls();
hls.loadSource(streamUrl);
hls.attachMedia(videoElement);
```

### Verificar estado de todos los streams (API JSON)
```bash
curl http://159.223.104.91:8080/status
```
Respuesta:
```json
{
  "updated": "2026-06-04T20:41:43Z",
  "streams": {
    "xy_hrn": { "status": "OK", "sup": "RUNNING", "segs": 13, "age": 0 },
    "hch_tv": { "status": "OK", "sup": "RUNNING", "segs": 11, "age": 4 }
  },
  "summary": { "ok": 12, "fail": 0, "total": 12 }
}
```

Campos:
- `status`: `OK` / `STALE` / `NO_M3U8`
- `sup`: estado en supervisord (`RUNNING` / `STOPPED` / `FATAL`)
- `segs`: cantidad de segmentos .ts activos
- `age`: segundos desde la última actualización del m3u8 (debe ser < 30)

---

## 4. Estructura de archivos

```
/opt/media-ai/
├── config/
│   └── stations.json              ← Registro de todas las estaciones
└── scripts/
    ├── stream_{id}.sh             ← Script de relay por estación (1 por cada una)
    └── health_monitor.sh          ← Watchdog automático (cron cada 2 min)

/var/www/streams/
├── status.json                    ← Estado JSON actualizado cada 2 min
├── xy_hrn/
│   ├── index.m3u8                 ← Playlist HLS activa (nginx la sirve)
│   ├── seg_00052.ts               ← Segmento de 4 segundos (se rota)
│   └── seg_00053.ts
└── hch_tv/
    └── index.m3u8

/var/log/streams/
├── {id}.log                       ← stdout de ffmpeg
├── {id}.err                       ← stderr de ffmpeg (donde ver errores)
└── health.log                     ← Log del watchdog

/etc/
├── wireguard/wg0.conf             ← Túnel VPN a Raspberry Pi Honduras
├── privoxy/config                 ← HTTP proxy → SOCKS5 Honduras
├── nginx/sites-available/streams  ← Config nginx puerto 8080
└── supervisor/conf.d/
    └── mediadev_streams.conf      ← Config de los 12 procesos ffmpeg
```

> **Segmentos HLS:** ffmpeg mantiene solo los últimos 10 segmentos (40 segundos).
> Los más antiguos se eliminan automáticamente. No hay acumulación en disco.

---

## 5. Cómo agregar una nueva radio o TV

### Paso 1 — Probar que la URL funciona desde Honduras

```bash
# Probar conectividad (debe devolver 200 OK o similar)
curl -I --max-time 10 --socks5-hostname 10.101.0.2:1080 https://URL_DEL_STREAM

# Grabar 10 segundos de prueba
curl -s --max-time 15 --socks5-hostname 10.101.0.2:1080 https://URL_DEL_STREAM \
  | ffmpeg -i pipe:0 -t 10 -c copy /tmp/test.ts

ls -lh /tmp/test.ts
# Si el archivo existe y tiene > 10KB, el stream funciona correctamente
```

### Paso 2 — Agregar a stations.json

```bash
nano /opt/media-ai/config/stations.json
```

Agregar dentro del array `"stations"`:
```json
{
  "id": "nueva_radio",
  "name": "Nombre de la Emisora",
  "type": "radio",
  "route": "honduras",
  "gateway": "hn01",
  "url": "https://URL_DEL_STREAM",
  "enabled": true
}
```

> - Para TV usar `"type": "tv"`
> - El campo `"gateway": "hn01"` siempre es el mismo (Raspberry Pi Honduras)
> - El `"id"` debe ser único, sin espacios, en minúsculas con guiones bajos

### Paso 3 — Crear el script de relay

**¿Qué método usar?**

| Si la URL contiene... | Usar |
|---|---|
| `ice42.securenetsystems.net` | **Caso A** — curl pipe |
| Cualquier otro dominio | **Caso B** — ffmpeg + privoxy |

---

**Caso A — ice42.securenetsystems.net (curl pipe):**
```bash
cat > /opt/media-ai/scripts/stream_nueva_radio.sh << 'SCRIPT'
#!/bin/bash
# Stream relay: Nombre de la Emisora
OUT_DIR="/var/www/streams/nueva_radio"
mkdir -p "$OUT_DIR"
exec curl -s --retry 999 --retry-delay 3 \
  --socks5-hostname 10.101.0.2:1080 \
  -A "MediaDEV/1.0" -H "Icy-MetaData: 1" \
  "https://ice42.securenetsystems.net/STREAM_ID" \
| ffmpeg -y \
  -loglevel warning \
  -fflags nobuffer \
  -i pipe:0 \
  -c:a copy \
  -f hls \
  -hls_time 4 \
  -hls_list_size 10 \
  -hls_flags delete_segments+append_list \
  -hls_segment_filename "$OUT_DIR/seg_%05d.ts" \
  "$OUT_DIR/index.m3u8"
SCRIPT
chmod +x /opt/media-ai/scripts/stream_nueva_radio.sh
```

---

**Caso B — Otro dominio (ffmpeg + privoxy):**
```bash
cat > /opt/media-ai/scripts/stream_nueva_radio.sh << 'SCRIPT'
#!/bin/bash
# Stream relay: Nombre de la Emisora
OUT_DIR="/var/www/streams/nueva_radio"
mkdir -p "$OUT_DIR"
exec ffmpeg -y \
  -loglevel warning \
  -fflags nobuffer \
  -http_proxy http://127.0.0.1:3128 \
  -user_agent "MediaDEV/1.0" \
  -i "https://URL_DEL_STREAM" \
  -c:a copy \
  -f hls \
  -hls_time 4 \
  -hls_list_size 10 \
  -hls_flags delete_segments+append_list \
  -hls_segment_filename "$OUT_DIR/seg_%05d.ts" \
  "$OUT_DIR/index.m3u8"
SCRIPT
chmod +x /opt/media-ai/scripts/stream_nueva_radio.sh
```

> Para **TV con video**: cambiar `-c:a copy` por `-c:v copy -c:a copy`

### Paso 4 — Registrar en supervisord

```bash
cat >> /etc/supervisor/conf.d/mediadev_streams.conf << 'CONF'

[program:stream_nueva_radio]
command=/opt/media-ai/scripts/stream_nueva_radio.sh
autostart=true
autorestart=true
startsecs=5
startretries=999
stopwaitsecs=10
stdout_logfile=/var/log/streams/nueva_radio.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2
stderr_logfile=/var/log/streams/nueva_radio.err
stderr_logfile_maxbytes=5MB
stderr_logfile_backups=2
environment=HOME="/root"
CONF
```

### Paso 5 — Activar y verificar

```bash
# Aplicar nueva config a supervisor
supervisorctl reread
supervisorctl update

# Verificar que levantó
supervisorctl status stream_nueva_radio

# Esperar segmentos (~20 segundos) y probar
sleep 20
curl -s http://localhost:8080/nueva_radio/index.m3u8 | head -5

# Reproducir
ffplay http://localhost:8080/nueva_radio/index.m3u8
```

---

## 6. Cómo eliminar o deshabilitar una estación

### Deshabilitar temporalmente (sin borrar)
```bash
supervisorctl stop stream_{id}

# Para reactivar:
supervisorctl start stream_{id}
```

### Eliminar permanentemente
```bash
# 1. Detener el proceso
supervisorctl stop stream_{id}

# 2. Eliminar el bloque [program:stream_{id}] del archivo
nano /etc/supervisor/conf.d/mediadev_streams.conf

# 3. Aplicar cambios
supervisorctl reread
supervisorctl update

# 4. Limpiar archivos
rm -f /opt/media-ai/scripts/stream_{id}.sh
rm -rf /var/www/streams/{id}/
rm -f /var/log/streams/{id}.log /var/log/streams/{id}.err

# 5. Eliminar de stations.json
nano /opt/media-ai/config/stations.json
```

---

## 7. Operación diaria — comandos esenciales

```bash
# Ver estado de todos los streams
supervisorctl status

# Reiniciar un stream específico
supervisorctl restart stream_xy_hrn

# Reiniciar todos los streams
supervisorctl restart all

# Ver errores de una estación en tiempo real
tail -f /var/log/streams/xy_hrn.err

# Ver log del watchdog automático
tail -f /var/log/streams/health.log

# Forzar verificación de salud ahora mismo
bash /opt/media-ai/scripts/health_monitor.sh

# Ver cuántos segmentos tiene cada stream actualmente
for d in /var/www/streams/*/; do
  echo "$(basename $d): $(ls $d*.ts 2>/dev/null | wc -l) segmentos"
done

# Ver cuánto espacio usan
du -sh /var/www/streams/
```

---

## 8. Monitoreo y salud del sistema

### API de estado (JSON en tiempo real)
```bash
# Desde el servidor
curl http://localhost:8080/status

# Desde fuera (internet)
curl http://159.223.104.91:8080/status
```

### Health check simple
```bash
curl http://localhost:8080/health
# Respuesta: OK
```

### Verificar el túnel WireGuard Honduras
```bash
wg show
```
Salida esperada:
```
interface: wg0
  public key: y37nKK/...
  listening port: 51820

peer: Xt4N/0OY6...
  endpoint: 181.115.19.237:XXXX
  latest handshake: 45 seconds ago       <-- debe ser reciente (< 2 min)
  transfer: X MiB received, X MiB sent
  persistent keepalive: every 25 seconds
```

### Verificar que la IP de salida es Honduras
```bash
curl -s --proxy http://127.0.0.1:3128 https://api.ipify.org
# Debe devolver: 181.115.19.237
```

### Verificar todos los servicios del stack
```bash
systemctl is-active wg-quick@wg0 privoxy supervisor nginx
# Los 4 deben mostrar: active
```

### Ver el cron del watchdog
```bash
crontab -l
# Debe mostrar: */2 * * * * /opt/media-ai/scripts/health_monitor.sh
```

---

## 9. Diagnóstico de problemas

### Un stream está en FATAL o BACKOFF

```bash
# 1. Ver el error exacto
tail -20 /var/log/streams/{id}.err

# 2. Probar la URL manualmente
curl -I --max-time 10 --socks5-hostname 10.101.0.2:1080 URL_DEL_STREAM

# 3. Grabar prueba
curl -s --max-time 15 --socks5-hostname 10.101.0.2:1080 URL \
  | ffmpeg -i pipe:0 -t 10 -c copy /tmp/debug.ts
ls -lh /tmp/debug.ts

# 4. Reiniciar
supervisorctl restart stream_{id}
```

### Todos los streams caen al mismo tiempo

Indica problema con el túnel WireGuard o la Raspberry Pi:
```bash
# Verificar túnel
wg show
ping 10.101.0.2

# Si no responde, reiniciar el túnel
systemctl restart wg-quick@wg0
sleep 5
wg show

# Verificar privoxy
curl -s --proxy http://127.0.0.1:3128 https://api.ipify.org
```

### Un stream aparece como STALE en el status JSON

El m3u8 deja de actualizarse — ffmpeg se congeló:
```bash
supervisorctl restart stream_{id}
# El watchdog también lo detectará y reiniciará en los próximos 2 minutos
```

### nginx devuelve 404

```bash
# Verificar que el directorio del stream existe
ls /var/www/streams/{id}/

# Verificar que el site está activo
ls /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

### La IP de salida no es Honduras

```bash
# Verificar que la Raspberry tiene el túnel wg1 activo
ssh -i ~/.ssh/keySED gustavo@192.168.4.189 "sudo wg show"
# Debe mostrar wg1 con handshake reciente y peer 159.223.104.91

# Verificar el proxy SOCKS5 en la Raspberry
ssh -i ~/.ssh/keySED gustavo@192.168.4.189 "sudo systemctl status microsocks-mediadev"
# Debe estar: active (running)
```

---

## 10. Cómo funciona internamente

### Flujo completo — stream ice42 (curl pipe)

```
supervisord arranca stream_xy_hrn.sh
    |
    v
curl --socks5-hostname 10.101.0.2:1080 https://ice42.securenetsystems.net/HRN
    |
    |-- SOCKS5 --> Raspberry Pi (10.101.0.2:1080)
    |                   |
    |                   |-- Sale por eth0 con IP 181.115.19.237 (Honduras)
    |                               |
    |                               v
    |                   ice42 ve IP hondureña -> autoriza el stream
    |                               |
    |<--- audio stream (MP3/AAC) ---+
    |
    v (pipe:0)
ffmpeg -i pipe:0 -c:a copy -f hls -hls_time 4 ...
    |
    v
/var/www/streams/xy_hrn/index.m3u8  (actualizado cada 4 seg)
/var/www/streams/xy_hrn/seg_XXXXX.ts
    |
    v
nginx :8080 sirve el HLS al cliente
```

### Flujo completo — otro dominio (ffmpeg + privoxy)

```
ffmpeg -http_proxy http://127.0.0.1:3128 -i https://URL
    |
    v
privoxy (127.0.0.1:3128)
    |
    |-- forward-socks5 --> 10.101.0.2:1080 (Raspberry)
    |                           |
    |                           |-- Sale por Honduras
    |                           v
    |               Servidor ve IP hondureña -> devuelve stream
    |
    v
ffmpeg genera segmentos HLS -> nginx sirve
```

### Por qué existen dos métodos

`ice42.securenetsystems.net` usa el protocolo **Icecast** con cabeceras HTTP
especiales (`Icy-MetaData: 1`, `icy-br`, etc.). ffmpeg no puede enviar estas
cabeceras al conectarse a través de SOCKS5. `curl` sí puede hacerlo, y una vez
establecida la conexión pasa el stream en bruto a ffmpeg por `pipe:0`, que solo
necesita decodificar el audio, sin preocuparse por el proxy.

---

## 11. Reinicio completo del stack

Si el servidor se reinicia o hay que levantar todo manualmente:

```bash
# Todos los servicios se configuraron para arrancar solos (systemctl enable)
# Si hay que forzar el inicio en orden:

systemctl start wg-quick@wg0     # 1. Túnel VPN Honduras (PRIMERO)
sleep 3
systemctl start privoxy          # 2. Bridge HTTP->SOCKS5
systemctl start supervisor       # 3. Gestor de procesos (arranca los 12 ffmpeg)
systemctl start nginx            # 4. Servidor HLS

# Verificar que todo quedó bien
sleep 15
supervisorctl status
curl http://localhost:8080/status
wg show
```

### Verificar servicios al boot
```bash
systemctl is-enabled wg-quick@wg0 privoxy supervisor nginx
# Todos deben mostrar: enabled
```

---

## 12. Referencia rápida

### Puertos del sistema

| Puerto | Servicio | Acceso |
|---|---|---|
| `8080` | nginx — Streams HLS | Público |
| `3128` | privoxy — HTTP proxy | Solo localhost |
| `51820/udp` | WireGuard VPN | Solo VPN |
| `22` | SSH | Administración |

### IPs del sistema

| Host | IP Pública | IP VPN | Rol |
|---|---|---|---|
| MediaDEV | `159.223.104.91` | `10.101.0.1` | Servidor principal DigitalOcean |
| Raspberry Pi | `181.115.19.237` | `10.101.0.2` | Nodo salida Honduras |

### supervisorctl — comandos más usados

```bash
supervisorctl status                           # Estado de todos
supervisorctl restart stream_{id}              # Reiniciar uno
supervisorctl restart all                      # Reiniciar todos
supervisorctl stop stream_{id}                 # Detener uno (sin borrar)
supervisorctl start stream_{id}                # Iniciar uno
supervisorctl tail stream_{id} stderr          # Ver últimos errores
supervisorctl reread && supervisorctl update   # Aplicar nueva config
```

### Rutas clave

| Archivo / Directorio | Descripción |
|---|---|
| `/opt/media-ai/config/stations.json` | Catálogo de estaciones |
| `/opt/media-ai/scripts/stream_{id}.sh` | Script de relay de cada estación |
| `/opt/media-ai/scripts/health_monitor.sh` | Watchdog automático |
| `/var/www/streams/{id}/index.m3u8` | Playlist HLS activa |
| `/var/www/streams/status.json` | Estado JSON del sistema |
| `/var/log/streams/{id}.err` | Log de errores por estación |
| `/etc/supervisor/conf.d/mediadev_streams.conf` | Config de supervisord |
| `/etc/wireguard/wg0.conf` | Configuración del túnel VPN |
| `/etc/privoxy/config` | Configuración del proxy HTTP→SOCKS5 |

---

*Infraestructura desplegada el 2026-06-04*  
*MediaDEV — Sistema de monitoreo de medios Honduras 24/7*
