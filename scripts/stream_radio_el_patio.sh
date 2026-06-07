#!/bin/bash
# Stream relay: Radio El Patio
# URL: http://51.89.173.53:8045/elpatio
OUT_DIR="/var/www/streams/radio_el_patio"
mkdir -p "$OUT_DIR"
exec ffmpeg -y \
  -loglevel warning \
  -fflags nobuffer \
  -http_proxy http://127.0.0.1:3128 \
  -user_agent "MediaDEV/1.0" \
  -i "http://51.89.173.53:8045/elpatio" \
  -vn -c:a aac -b:a 64k -ac 1 -ar 22050 \
  -f hls \
  -hls_time 4 \
  -hls_list_size 10 \
  -hls_flags append_list \
  -hls_segment_filename "$OUT_DIR/seg_%05d.ts" \
  "$OUT_DIR/index.m3u8"
