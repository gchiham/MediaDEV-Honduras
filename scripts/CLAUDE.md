# Scripts de Stream — CLAUDE.md

## Propósito
Un script bash por stream. Cada uno lanza ffmpeg gestionado por supervisord.
Capturan el stream origen vía proxy y lo recodifican a HLS de solo audio.

## Dos patrones de proxy

### SOCKS5 directo (7 streams): fm_941, radio_choluteca, radio_satelite, suave_fm, xy_hrn, xy_sps, xy_tgu
```bash
curl --socks5-hostname 10.101.0.X:1080 -s URL | ffmpeg -i pipe:0 ...
```

### HTTP proxy via Privoxy (5 streams): hch_tv, radio_america, radio_el_patio, radio_globo, teleceiba
```bash
ffmpeg -http_proxy http://127.0.0.1:3128 -i URL ...
# Privoxy en :3128 reenvía a SOCKS5 10.101.0.X:1080
```

## Parámetros ffmpeg obligatorios
```bash
-vn              # Strip video (algunos streams son TV con H.264)
-c:a aac         # Codec ANTES de -ac (evita AAC-HE v2 parametric stereo)
-b:a 64k         # Suficiente para transcripción Whisper
-ac 1            # Mono
-ar 22050        # 22kHz
-hls_time 4      # Segmentos de 4 segundos
-hls_list_size 10
-hls_flags append_list  # SIN delete_segments — segmentos persisten para auditoría 8h
```

## Cambiar gateway SOCKS5
```bash
NUEVA_IP="10.101.0.X"
sed -i "s/10\.101\.0\.[0-9]*:1080/${NUEVA_IP}:1080/g" /opt/media-ai/scripts/stream_*.sh
sed -i "s/forward-socks5  \/  10\.101\.0\.[0-9]*:1080/forward-socks5  \/  ${NUEVA_IP}:1080/" /etc/privoxy/config
systemctl restart privoxy && supervisorctl restart all
```

## Nodos WireGuard activos
- 10.101.0.2 — Raspberry Pi Honduras (principal, offline)
- 10.101.0.3 — PC Sedesol Windows (temporal activo)
- 10.101.0.4 — Mac Developer (temporal disponible)

## Supervisord
```bash
supervisorctl status                        # Estado de 12 streams
supervisorctl restart all                   # Reiniciar todos
supervisorctl tail stream_fm_941 stderr     # Errores de un stream
# Config: /etc/supervisor/conf.d/mediadev_streams.conf
```

## Pitfalls conocidos
- Orden de flags ffmpeg: `-c:a aac -b:a 64k -ac 1` (codec antes del canal)
- Sin `-vn` los streams de TV consumen CPU extra decodificando video innecesariamente
- xy_sps puede tener URL 404 (stream origen caído) — el CB lo deshabilitará tras 5 fallos
- Backup con IPs de la Pi: /opt/media-ai/scripts_backup_rpi/
