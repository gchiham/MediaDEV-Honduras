#!/bin/bash
# Stream relay: Radio Globo
# URL: https://stream.radiosmundiales.com/stream/radioglobo
OUT_DIR="/var/www/streams/radio_globo"
mkdir -p "$OUT_DIR"
exec ffmpeg -y \
  -loglevel warning \
  -fflags nobuffer \
  -http_proxy http://127.0.0.1:3128 \
  -user_agent "MediaDEV/1.0" \
  -i "https://stream.radiosmundiales.com/stream/radioglobo" \
  -vn -c:a aac -b:a 64k -ac 1 -ar 22050 \
  -f hls \
  -hls_time 4 \
  -hls_list_size 10 \
  -hls_flags append_list \
  -hls_segment_filename "$OUT_DIR/seg_%05d.ts" \
  "$OUT_DIR/index.m3u8"
