#!/bin/bash
LOCKFILE="/tmp/health_monitor.lock"
LOG="/var/log/streams/health.log"
STATUS_JSON="/var/www/streams/status.json"
STALE_SECS=30
STATIONS="xy_hrn xy_tgu xy_sps radio_satelite fm_941 suave_fm radio_america radio_globo radio_el_patio hch_tv teleceiba radio_choluteca"

# Una sola instancia — si ya corre, salir
[ -f "$LOCKFILE" ] && exit 0
touch "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT INT TERM

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
SUP_STATUS=$(timeout 5 supervisorctl status 2>/dev/null)

ok=0; fail=0; entries=""; restarts=""

for sid in $STATIONS; do
    m3u8="/var/www/streams/${sid}/index.m3u8"
    sup=$(echo "$SUP_STATUS" | awk "/stream_${sid} /{print \$2}")
    [ -z "$sup" ] && sup="UNKNOWN"

    if [ -f "$m3u8" ]; then
        age=$(( $(date +%s) - $(stat -c %Y "$m3u8") ))
        segs=$(find "/var/www/streams/${sid}" -maxdepth 1 -name "seg_*.ts" -printf "." 2>/dev/null | wc -c)
        if [ "$age" -le "$STALE_SECS" ] && [ "$segs" -gt 0 ]; then
            st="OK"; ok=$((ok+1))
        else
            st="STALE"; fail=$((fail+1))
            echo "[$TIMESTAMP] STALE: $sid age=${age}s" >> "$LOG"
            restarts="$restarts $sid"
        fi
    else
        age=0; segs=0; st="NO_M3U8"; fail=$((fail+1))
    fi

    entries="${entries},\"${sid}\":{\"status\":\"${st}\",\"sup\":\"${sup}\",\"segs\":${segs},\"age\":${age}}"
done

# Escribir status primero
entries="${entries#,}"
echo "{\"updated\":\"${TIMESTAMP}\",\"streams\":{${entries}},\"summary\":{\"ok\":${ok},\"fail\":${fail},\"total\":12}}" > "$STATUS_JSON"

# Liberar lock ANTES de los restarts lentos
rm -f "$LOCKFILE"
trap - EXIT INT TERM

# Restart streams caídos (sin bloquear futuros runs)
for sid in $restarts; do
    timeout 15 supervisorctl restart "stream_${sid}" >> "$LOG" 2>&1
done

[ $fail -gt 0 ] && echo "[$TIMESTAMP] SUMMARY ok=$ok fail=$fail" >> "$LOG"
