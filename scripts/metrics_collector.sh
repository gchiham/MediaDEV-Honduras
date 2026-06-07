#!/bin/bash
# Recolector de métricas histórico para dashboard
# Corre cada minuto vía cron

METRICS_DIR="/var/www/streams/metrics"
STATUS_FILE="/var/www/streams/status.json"
HISTORY_DB="$METRICS_DIR/history.db"

mkdir -p "$METRICS_DIR"

# Si no existe el archivo de historial, crearlo
if [ ! -f "$HISTORY_DB" ]; then
    echo "timestamp|stream_id|status|segs|mb_total" > "$HISTORY_DB"
fi

# Leer status.json y extraer métricas
if [ -f "$STATUS_FILE" ]; then
    TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M:%S")

    # Parsear cada stream con jq
    jq -r '.streams | to_entries[] | "\(.key)|\(.value.status)|\(.value.segs)"' "$STATUS_FILE" 2>/dev/null | while IFS='|' read -r stream_id status segs; do
        # Calcular MB aproximado (35 kbps = ~263 MB/h, por segmento ~10MB)
        mb_segment=$((segs * 10 / 60))

        echo "$TIMESTAMP|$stream_id|$status|$segs|$mb_segment" >> "$HISTORY_DB"
    done
fi

# Limpiar registros más viejos de 10 días (para no crecer indefinidamente)
if [ -f "$HISTORY_DB" ]; then
    tail -n 14400 "$HISTORY_DB" > "$HISTORY_DB.tmp" && mv "$HISTORY_DB.tmp" "$HISTORY_DB"
fi
