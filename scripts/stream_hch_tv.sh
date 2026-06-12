#!/bin/bash
# Stream relay: HCH TV
# URL: https://live.streamhch.com/live/streams/hch1.m3u8
OUT_DIR="/var/www/streams/hch_tv"
mkdir -p "$OUT_DIR"
exec ffmpeg -y \
  -loglevel warning \
  -fflags nobuffer \
  -user_agent "MediaDEV/1.0" \
  -i "https://live.streamhch.com/live/streams/hch1.m3u8" \
  -c:v copy -c:a aac -b:a 128k \
  -f hls \
  -hls_time 4 \
  -hls_list_size 10 \
  -hls_flags append_list \
  -hls_segment_filename "$OUT_DIR/seg_%05d.ts" \
  "$OUT_DIR/index.m3u8"
