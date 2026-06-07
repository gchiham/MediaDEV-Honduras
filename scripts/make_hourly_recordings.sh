#!/bin/bash
# Corre cada hora (minuto 1). Toma los segmentos .ts de la hora anterior
# y los convierte en un MP3 continuo. No abre conexiones nuevas.

STREAMS_DIR="/var/www/streams"

# Calcular hora anterior completa
HOUR_LABEL=$(date -u -d "1 hour ago" "+%Y-%m-%d_%Hh")
START_EPOCH=$(date -u -d "$(date -u -d 1 hour ago +%Y-%m-%d %H:00:00)" +%s)
END_EPOCH=$((START_EPOCH + 3600))

echo "[$(date -u +%H:%M:%S)] Generando grabaciones hora: $HOUR_LABEL"

for stream_dir in "$STREAMS_DIR"/*/; do
    stream_id=$(basename "$stream_dir")
    [[ "$stream_id" =~ ^(metrics|audit_index)$ ]] && continue
    [ ! -d "$stream_dir" ] && continue

    RECORDINGS_DIR="$stream_dir/recordings"
    mkdir -p "$RECORDINGS_DIR"
    OUTPUT="$RECORDINGS_DIR/${HOUR_LABEL}.mp3"

    # Si ya existe, no rehacer
    [ -f "$OUTPUT" ] && continue

    INDEX="$STREAMS_DIR/audit_index/${stream_id}.csv"
    [ ! -f "$INDEX" ] && echo "  [$stream_id] Sin índice, saltando" && continue

    # Recolectar segmentos dentro del rango de hora
    SEGS=()
    while IFS="|" read -r seg start_ts end_ts s_epoch e_epoch; do
        [ "$seg" = "segment" ] && continue
        if [ "$s_epoch" -ge "$START_EPOCH" ] && [ "$s_epoch" -lt "$END_EPOCH" ]; then
            f="$stream_dir/$seg"
            [ -f "$f" ] && SEGS+=("$f")
        fi
    done < "$INDEX"

    COUNT=${#SEGS[@]}
    if [ "$COUNT" -lt 10 ]; then
        echo "  [$stream_id] Solo $COUNT segmentos — no suficiente, saltando"
        continue
    fi

    MINUTES=$(( COUNT * 4 / 60 ))
    echo "  [$stream_id] $COUNT segmentos (~${MINUTES} min) → $HOUR_LABEL.mp3"

    (for f in "${SEGS[@]}"; do cat "$f"; done) | \
        ffmpeg -y -loglevel error \
            -i pipe:0 \
            -c:a libmp3lame -b:a 64k -ac 1 -ar 22050 \
            "$OUTPUT"

    if [ $? -eq 0 ]; then
        SIZE=$(du -h "$OUTPUT" | cut -f1)
        echo "  [$stream_id] ✓ $OUTPUT ($SIZE)"
    else
        echo "  [$stream_id] ✗ Error convirtiendo"
        rm -f "$OUTPUT"
    fi
done

# Limpiar: mantener solo las últimas 8 grabaciones por stream
for stream_dir in "$STREAMS_DIR"/*/; do
    stream_id=$(basename "$stream_dir")
    [[ "$stream_id" =~ ^(metrics|audit_index)$ ]] && continue
    RDIR="$stream_dir/recordings"
    [ -d "$RDIR" ] && ls -t "$RDIR"/*.mp3 2>/dev/null | tail -n +9 | xargs -r rm -f
done

echo "[$(date -u +%H:%M:%S)] Listo."
