# MediaDEV — mediaCAP (nodo de captura)

Captura 24/7 de ~20 estaciones de Honduras (radios y canales de TV). Las sirve como HLS local
y sube la grabación horaria al bucket de objetos, de donde la toma el Destroyer (detección de
anuncios, que no corre en este nodo).

**Para revisar el sistema, empezá por [`MAPA_MEDIACAP.md`](MAPA_MEDIACAP.md).** Ahí está qué corre
hoy, desde qué repo y las zonas de riesgo. Detalle por carpeta:
[`daemon/CLAUDE.md`](daemon/CLAUDE.md) · [`scripts/CLAUDE.md`](scripts/CLAUDE.md) ·
[`mcp/README.md`](mcp/README.md). Contexto para agentes: [`CLAUDE.md`](CLAUDE.md).
Diagnóstico de captura: skill [`mediacap-ffmpeg`](.claude/skills/mediacap-ffmpeg/SKILL.md).

## Por qué existe
Las emisoras hondureñas bloquean IPs extranjeras. mediaCAP (droplet DigitalOcean
`159.223.104.91`, 2 vCPU / 4 GB) sale a internet por **gateways residenciales en Honduras**
(Raspberry Pi / PC) conectados por WireGuard, cada uno con un proxy SOCKS5. Las fuentes de CDN
global van directo.

## Arquitectura (octubre 2026)
```
capture_config JOIN media_sources (PostgreSQL media-db)   ← única fuente de verdad de estaciones
        │ releído cada 300 s
        ▼
stream-daemon ── 1 ffmpeg por estación ──► /var/www/streams/<sid>/seg_N.ts + index.m3u8 (HLS 4 s)
   │  route=socks5 → privoxy / curl --socks5 → WireGuard wg0 → gateway activo (/etc/mediadev/gateway.conf)
   │  route=direct → origen directo
   │
   ├─ radios: cada hora concatena (-c copy) → <sid>/YYYY/MM/YYYY-MM-DDTHHZ.ts → bucket
   └─ TV: video-segment-uploader → video_segments/<sid>/… + audio horario .ts + manifest → bucket
                                                   │
                                                   ▼
                                     Destroyer (AWS, repo gchiham/destroyer)

mediadev-health-engine + mediadev-gateway-api (repo destroyer, cap/gateway/) → sondean gateways y hacen failover
```

## Servicios en mediaCAP
| Unit | Código | Repo |
|---|---|---|
| `stream-daemon` | `daemon/stream_daemon.py` | este |
| `video-segment-uploader` | `scripts/video_segment_uploader.py` | este |
| `mediadev-ffmpeg-reaper.timer` | `scripts/ffmpeg_reaper.py` | este |
| `mediadev-monitor` | `monitor/monitor.py` (WireGuard → Telegram) | este |
| `mediadev-logs`, `mediadev-metrics` | `scripts/mediadev_logs.py`, `scripts/mediadev_metrics.py` (CloudWatch) | este |
| cron 21:00 HN | `scripts/tvprem_watch.py` | este |
| `mediadev-health-engine`, `mediadev-gateway-api` | `cap/gateway/engine/` | `gchiham/destroyer` |
| `wg-quick@wg0`, `privoxy`, `nginx` | config | `gchiham/mediadev-infra` (`mediacap/`) |

`supervisor` sigue activo **sin programas**. El dashboard Flask (`dashboard_v4.py`) y su API REST
se eliminaron el 14-jun y su código quedó en `archive/`.

## Verificar estado
```bash
systemctl is-active stream-daemon video-segment-uploader mediadev-health-engine \
  mediadev-gateway-api mediadev-monitor privoxy nginx wg-quick@wg0
pgrep -c ffmpeg                       # ≈ 1 por estación habilitada
cat /etc/mediadev/gateway.conf        # gateway activo
wg show wg0                           # handshakes de los gateways
tail -f /var/log/streams/daemon.log   # health, CB, grabación
df -h /                               # lo primero si algo "no graba"
```

## Repos del sistema
| Repo | Qué tiene |
|---|---|
| `gchiham/MediaDEV-Honduras` (este, público) | código de mediaCAP (`/opt/media-ai`) |
| `gchiham/destroyer` (privado) | `/opt/destroyer` de ambos nodos: `cap/` = gateways de mediaCAP, `app/` = Destroyer |
| `gchiham/mediadev-infra` (privado) | systemd, cron, nginx, privoxy, WireGuard (redactado) + `INVENTORY.md` |
| `gchiham/media-app` (privado) | producto PubliAudit (mediaAPP). Ver nota de drift en su README |

## Zona horaria
Backend en UTC (`TIMESTAMPTZ`) y display en GMT-6 (Honduras, sin DST). Cutover el 13-jun-2026
16:07 UTC: `pipeline_version = 'legacy'` es anterior y `'utc_v2'` posterior. Las horas grabadas
se nombran en UTC (`YYYY-MM-DDTHHZ`).

## Secretos
Nunca en git: `/etc/mediadev-db.env`, `/etc/mediadev-s3.env`, `/etc/mediadev-cw.env`,
`/etc/mediadev-monitor.env`, `/opt/destroyer/.env`, `/etc/wireguard/wg0.conf`.
`config/stations.json` tampoco (tiene credenciales IPTV). Ver `config/stations.json.example`.
