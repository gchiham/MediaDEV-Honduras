#!/bin/bash
# =============================================================================
# MediaDEV — Runner unificado de streams
# =============================================================================
# Uso: stream_run.sh <stream_id>
#
# Lee url / type / route de config/stations.json y decide cómo capturar:
#   route=gateway → siempre por el gateway activo (geo-restringidos, ej. ice42)
#   route=direct  → siempre conexión directa
#   route=auto    → prueba directo; si falla, usa gateway. Re-evalúa en CADA
#                   arranque, así que si una fuente directa es bloqueada, el
#                   siguiente reinicio (supervisord) cae automáticamente al gateway.
#
# Captura:
#   - Fuentes Icecast (ice42.securenetsystems.net) → curl pipe (headers Icy-MetaData)
#   - Resto → ffmpeg (directo, o -http_proxy vía Privoxy para el gateway)
# Salida:
#   - type=radio → audio AAC 64k mono 22050
#   - type=tv    → video copiado + audio AAC 128k
# =============================================================================
set -uo pipefail

STREAM_ID="${1:?uso: stream_run.sh <stream_id>}"
STATIONS="/opt/media-ai/config/stations.json"
GW_CONF="/etc/mediadev/gateway.conf"
OUT_DIR="/var/www/streams/$STREAM_ID"
mkdir -p "$OUT_DIR"

# Gateway activo (define GW_SOCKS5)
# shellcheck source=/etc/mediadev/gateway.conf
[ -f "$GW_CONF" ] && source "$GW_CONF"

# Config del stream (url|type|route) en una sola lectura
CFG=$(python3 -c "
import json,sys
d=json.load(open('$STATIONS'))
s=next((x for x in d['stations'] if x['id']=='$STREAM_ID'), None)
if not s: sys.exit(1)
print('|'.join([s['url'], s.get('type','radio'), s.get('route','gateway'), s.get('referer','')]))
") || { echo "[$STREAM_ID] no encontrado en stations.json"; exit 1; }
IFS='|' read -r URL TYPE ROUTE REFERER <<< "$CFG"

# ¿Fuente Icecast? (requiere curl pipe, no ffmpeg)
is_ice42=0
[[ "$URL" == *ice42.securenetsystems.net* ]] && is_ice42=1

# Decidir transporte
probe_direct() {
  # Conexión directa OK si la fuente responde 2xx. No usamos range requests:
  # muchos servidores Icecast los ignoran y mandan stream continuo (falso negativo).
  local code
  code=$(curl -s --max-time 6 -o /dev/null -w '%{http_code}' -L -A "MediaDEV/1.0" "$1" 2>/dev/null)
  [[ "$code" =~ ^2 ]]
}
USE_GATEWAY=1
case "$ROUTE" in
  direct)  USE_GATEWAY=0 ;;
  gateway) USE_GATEWAY=1 ;;
  auto)    if probe_direct "$URL"; then USE_GATEWAY=0; else USE_GATEWAY=1; fi ;;
  *)       USE_GATEWAY=1 ;;
esac

# Perfil de salida según tipo
if [ "$TYPE" = "tv" ]; then
  OUT=(-c:v copy -c:a aac -b:a 128k)
else
  OUT=(-vn -c:a aac -b:a 64k -ac 1 -ar 22050)
fi
HLS=(-f hls -hls_time 4 -hls_list_size 10 -hls_flags append_list
     -hls_segment_filename "$OUT_DIR/seg_%05d.ts" "$OUT_DIR/index.m3u8")

echo "[$STREAM_ID] type=$TYPE route=$ROUTE use_gateway=$USE_GATEWAY ice42=$is_ice42"

if [ "$is_ice42" = 1 ]; then
  # Icecast → curl pipe. Directo o por gateway según USE_GATEWAY.
  if [ "$USE_GATEWAY" = 1 ]; then
    exec curl -s --retry 999 --retry-delay 3 --socks5-hostname "$GW_SOCKS5" \
      -A "MediaDEV/1.0" -H "Icy-MetaData: 1" "$URL" \
    | ffmpeg -y -loglevel warning -fflags nobuffer -i pipe:0 "${OUT[@]}" "${HLS[@]}"
  else
    exec curl -s --retry 999 --retry-delay 3 \
      -A "MediaDEV/1.0" -H "Icy-MetaData: 1" "$URL" \
    | ffmpeg -y -loglevel warning -fflags nobuffer -i pipe:0 "${OUT[@]}" "${HLS[@]}"
  fi
else
  # Resto → ffmpeg directo o vía Privoxy (gateway)
  PROXY=()
  [ "$USE_GATEWAY" = 1 ] && PROXY=(-http_proxy http://127.0.0.1:3128)
  HDRS=()
  [ -n "$REFERER" ] && HDRS=(-headers "Referer: $REFERER
")
  exec ffmpeg -y -loglevel warning -fflags nobuffer \
    -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 \
    -reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx \
    -reconnect_delay_max 10 "${PROXY[@]}" "${HDRS[@]}" \
    -user_agent "MediaDEV/1.0" -i "$URL" "${OUT[@]}" "${HLS[@]}"
fi
