#!/bin/bash
# Cargar el gateway activo (fuente de verdad: /etc/mediadev/gateway.conf)
# Para cambiar de gateway usa: sudo gateway_switch.sh <gateway_id>
# shellcheck source=/etc/mediadev/gateway.conf
source /etc/mediadev/gateway.conf

# Stream relay: Radio Choluteca
# URL: https://ice42.securenetsystems.net/CHOLUTEC
OUT_DIR="/var/www/streams/radio_choluteca"
mkdir -p "$OUT_DIR"
exec curl -s --retry 999 --retry-delay 3 \
  --socks5-hostname $GW_SOCKS5 \
  -A "MediaDEV/1.0" -H "Icy-MetaData: 1" \
  "https://ice42.securenetsystems.net/CHOLUTEC" \
| ffmpeg -y \
  -loglevel warning \
  -fflags nobuffer \
  -i pipe:0 \
  -vn -c:a aac -b:a 64k -ac 1 -ar 22050 \
  -f hls \
  -hls_time 4 \
  -hls_list_size 10 \
  -hls_flags append_list \
  -hls_segment_filename "$OUT_DIR/seg_%05d.ts" \
  "$OUT_DIR/index.m3u8"
