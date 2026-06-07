#!/bin/bash
# Stream relay: Radio Satelite
# URL: http://ice42.securenetsystems.net/SATELITE
OUT_DIR="/var/www/streams/radio_satelite"
mkdir -p "$OUT_DIR"
exec curl -s --retry 999 --retry-delay 3 \
  --socks5-hostname 10.101.0.3:1080 \
  -A "MediaDEV/1.0" -H "Icy-MetaData: 1" \
  "http://ice42.securenetsystems.net/SATELITE" \
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
