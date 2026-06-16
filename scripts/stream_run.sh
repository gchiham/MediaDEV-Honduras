#!/bin/bash
# /opt/media-ai/scripts/stream_run.sh
# Captura de stream con soporte para hasta 3 URLs de fallback automático.
# Supervisord gestiona el proceso; este script NUNCA sale en operación normal.

set -uo pipefail

STREAM_ID="${1:?Uso: stream_run.sh <stream_id>}"

STATIONS_JSON="/opt/media-ai/config/stations.json"
GATEWAY_CONF="/etc/mediadev/gateway.conf"
HLS_DIR="/var/www/streams/${STREAM_ID}"
RETRY_DELAY=5       # segundos entre reintentos
URL_RETRY_LIMIT=3   # fallos consecutivos por URL antes de rotar a la siguiente

mkdir -p "$HLS_DIR"
source "$GATEWAY_CONF"   # → GW_SOCKS5, GW_PRIVOXY_PORT

# ── Leer configuración del stream (una sola llamada Python) ───────────────────
# Soporta "urls": [...] (array, hasta 3) y "url": "..." (string, retrocompat).
_PY='
import json, sys
sid, path = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
st = next((s for s in cfg["stations"] if s["id"] == sid), None)
if not st:
    print("ERROR: stream no encontrado", file=sys.stderr); sys.exit(1)
urls = st.get("urls") or ([st["url"]] if st.get("url") else [])
if not urls:
    print("ERROR: sin URLs configuradas", file=sys.stderr); sys.exit(1)
print(st.get("type", "radio"))
print(st.get("route", "auto"))
print(st.get("referer", ""))
for u in urls:
    print(u)
'
mapfile -t _CFG < <(python3 -c "$_PY" "$STREAM_ID" "$STATIONS_JSON")

STREAM_TYPE="${_CFG[0]:-radio}"
STREAM_ROUTE="${_CFG[1]:-auto}"
STREAM_REFERER="${_CFG[2]:-}"
URLS=("${_CFG[@]:3}")
URL_COUNT="${#URLS[@]}"

if [[ $URL_COUNT -eq 0 ]]; then
    echo "[$STREAM_ID] ERROR: sin URLs en stations.json" >&2
    exit 1
fi

echo "[$STREAM_ID] type=$STREAM_TYPE route=$STREAM_ROUTE urls=$URL_COUNT"

# ── Determinar si usar proxy (se decide una sola vez al inicio) ───────────────
USE_PROXY=0
if [[ "$STREAM_ROUTE" == "gateway" ]]; then
    USE_PROXY=1
elif [[ "$STREAM_ROUTE" == "auto" ]]; then
    # Prueba rápida: ¿la primera URL es accesible directo?
    _code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "${URLS[0]}" 2>/dev/null | head -c 3)
    if [[ "$_code" != 2* && "$_code" != 3* ]]; then
        USE_PROXY=1
        echo "[$STREAM_ID] auto: directo bloqueado (HTTP $_code) → vía gateway"
    else
        echo "[$STREAM_ID] auto: acceso directo OK (HTTP $_code)"
    fi
fi

# Construir arrays de flags (vacíos si no aplican)
PROXY_FLAGS=()
if [[ $USE_PROXY -eq 1 ]]; then
    PROXY_FLAGS=(-http_proxy "http://127.0.0.1:${GW_PRIVOXY_PORT:-3128}")
fi

REFERER_FLAGS=()
if [[ -n "$STREAM_REFERER" ]]; then
    REFERER_FLAGS=(-headers "Referer: ${STREAM_REFERER}\r\n")
fi

# Flags de reconexión HLS (-reconnect_at_eof 0: crítico para HLS de ventana corta)
RECONNECT_FLAGS=(
    -reconnect 1
    -reconnect_at_eof 0
    -reconnect_streamed 1
    -reconnect_delay_max 8
    -rw_timeout 20000000
    -timeout 15000000
)

# Salida HLS local
HLS_OUT=(
    -f hls
    -hls_time 4
    -hls_list_size 10
    -hls_flags "append_list+omit_endlist"
    -hls_segment_filename "${HLS_DIR}/seg_%05d.ts"
    "${HLS_DIR}/index.m3u8"
)

# ── Ejecutar ffmpeg para una URL concreta ─────────────────────────────────────
run_url() {
    local url="$1"

    if [[ "$STREAM_TYPE" == "tv" ]]; then
        ffmpeg -hide_banner -loglevel warning \
            "${PROXY_FLAGS[@]+"${PROXY_FLAGS[@]}"}" \
            "${REFERER_FLAGS[@]+"${REFERER_FLAGS[@]}"}" \
            "${RECONNECT_FLAGS[@]}" \
            -i "$url" \
            -c:v copy -c:a aac -b:a 128k -ac 2 \
            "${HLS_OUT[@]}"

    elif [[ "$url" == *".m3u8"* || "$url" == *".m3u"* ]]; then
        # Radio HLS
        ffmpeg -hide_banner -loglevel warning \
            "${PROXY_FLAGS[@]+"${PROXY_FLAGS[@]}"}" \
            "${REFERER_FLAGS[@]+"${REFERER_FLAGS[@]}"}" \
            "${RECONNECT_FLAGS[@]}" \
            -i "$url" \
            -vn -c:a aac -b:a 64k -ac 1 -ar 22050 \
            "${HLS_OUT[@]}"

    else
        # Radio Icecast / flujo continuo → curl | ffmpeg
        local -a CURL_FLAGS=(-s --max-time 0 --retry 0 -A "MediaDEV/1.0" -L)
        if [[ $USE_PROXY -eq 1 ]]; then
            CURL_FLAGS+=(--socks5-hostname "${GW_SOCKS5#socks5://}")
        fi
        curl "${CURL_FLAGS[@]}" "$url" | \
        ffmpeg -hide_banner -loglevel warning \
            -i pipe:0 \
            -vn -c:a aac -b:a 64k -ac 1 -ar 22050 \
            "${HLS_OUT[@]}"
    fi
}

# ── Loop principal: rotar URLs ante fallos consecutivos ───────────────────────
url_idx=0
url_fails=0

while true; do
    URL="${URLS[$url_idx]}"
    if [[ $URL_COUNT -gt 1 ]]; then
        echo "[$STREAM_ID] URL $((url_idx+1))/$URL_COUNT → $URL"
    fi

    run_url "$URL"
    RC=$?
    echo "[$STREAM_ID] ffmpeg salió rc=$RC"

    sleep "$RETRY_DELAY"

    if [[ $URL_COUNT -gt 1 ]]; then
        url_fails=$(( url_fails + 1 ))
        if [[ $url_fails -ge $URL_RETRY_LIMIT ]]; then
            url_idx=$(( (url_idx + 1) % URL_COUNT ))
            url_fails=0
            echo "[$STREAM_ID] → rotando a URL $((url_idx+1))/$URL_COUNT"
        fi
    fi
done
