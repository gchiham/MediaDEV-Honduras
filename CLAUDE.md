# MediaDEV Stream Monitor — Contexto raíz

## Propósito del proyecto
Sistema de monitoreo 24/7 de 12 estaciones de radio y TV de Honduras.
Captura streams de audio, los sirve como HLS, y expone un dashboard con KPIs en tiempo real.
Incluye capacidad de auditoría: almacena 8 horas de audio por stream y genera grabaciones horarias en MP3.

## Arquitectura de alto nivel

```
[Streams Honduras] → [Exit Node SOCKS5] ──WireGuard──► [DigitalOcean 159.223.104.91]
                                                              │
                                          ┌───────────────────┼───────────────────┐
                                     supervisord          stream-daemon        nginx
                                     (12 ffmpeg)         (Python daemon)    (puerto 80)
                                          │                    │                  │
                                   /var/www/streams/     mediadev.db         gunicorn
                                   {stream}/seg_*.ts    (SQLite WAL)        dashboard_v4.py
                                   {stream}/index.m3u8                     (puerto 9000)
                                   {stream}/recordings/
```

## Restricción crítica de hardware
**1 vCPU / 2 GB RAM / 10 GB Disk (DigitalOcean Droplet)**
Toda decisión de diseño prioriza este constraint. No agregar workers, no reducir intervalos del daemon, no hacer glob masivo en disco.

## Componentes principales

| Componente | Ruta | Descripción |
|---|---|---|
| Daemon unificado |  | Loop principal: health, index, metrics, record, cleanup |
| Dashboard Flask |  | API + vistas web |
| Templates HTML |  | dashboard_main.html, stream_detail.html |
| Scripts de stream |  | Un script por stream, ffmpeg + proxy |
| Base de datos |  | SQLite WAL, única fuente de verdad |
| Configuración |  | Metadatos de estaciones |

## Responsabilidades por carpeta

-  → Lógica del daemon (health check, indexer, métricas, grabaciones, cleanup)
-  → Flask app + templates Jinja2
-  → Scripts ffmpeg individuales por stream
-  → Segmentos HLS activos (.ts) + playlist (index.m3u8) + grabaciones MP3
-  → Base de datos SQLite

## Servicios del sistema

```bash
systemctl status stream-daemon        # Daemon Python (health+index+metrics+record+cleanup)
systemctl status dashboard-mediadev   # Gunicorn → Flask (1 worker)
systemctl status nginx                # Reverse proxy puerto 80
systemctl status supervisor           # Gestiona 12 procesos ffmpeg
systemctl status privoxy              # HTTP proxy 127.0.0.1:3128 → SOCKS5
systemctl status wg-quick@wg0        # Túnel WireGuard
```

## Red y proxies

- **WireGuard wg0**: MediaDEV 10.101.0.1/24
- **Raspberry Pi** (gateway principal): 10.101.0.2:1080 SOCKS5
- **PC Sedesol** (gateway temporal): 10.101.0.3:1080 SOCKS5
- **PC Developer** (gateway temporal): 10.101.0.4:1080 SOCKS5
- 7 streams usan  directo en ffmpeg
- 5 streams usan  (Privoxy → SOCKS5)

## Zona horaria
Todo el sistema opera en **America/Tegucigalpa (GMT-6 / CST)**. Los timestamps en SQLite se almacenan como Unix epoch (timezone-neutral). La conversión a GMT-6 ocurre únicamente en la capa de presentación del dashboard.

## Schema de base de datos
```sql
stream_status  -- estado actual de cada stream (1 fila por stream)
metrics        -- muestra por minuto: status, segs, bytes (retención 7 días)
segments       -- índice de segmentos .ts en disco (retención 8h)
events         -- log de eventos UP/DOWN/RESTART/CB_OPEN/CB_CLOSE (retención 30 días)
```

## Principios arquitectónicos
1. **Un solo daemon** reemplaza 5 cron jobs — evita condiciones de carrera
2. **SQLite WAL** permite lecturas concurrentes sin bloquear escrituras del daemon
3. **Circuit Breaker** (5 fallos → OPEN, reset 30min) evita restart storms
4. **Sin glob masivo** — el health check lee solo el m3u8 en memoria, no escanea disco
5. **Batch queries** en el dashboard — 4-5 GROUP BY en vez de 72 queries individuales
6. **Segmentos persistentes** —  sin ; limpieza cada 30min

## Instrucciones para AI
- Leer el CLAUDE.md más cercano a los archivos del task antes de explorar el repo
- Solo inspeccionar archivos relacionados con la tarea solicitada
- Evitar búsquedas globales del repositorio salvo necesidad explícita
- Minimizar uso de tokens — preferir ediciones quirúrgicas
- Preservar consistencia arquitectónica — no cambiar infraestructura sin pedido explícito
- Para cambios en streams: siempre verificar si usan socks5 directo o http_proxy
- No reducir intervalos del daemon sin justificación — el servidor es 1 vCPU
- Las queries SQL siempre deben usar GROUP BY batch, nunca loops individuales por stream
