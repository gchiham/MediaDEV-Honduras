#!/bin/bash
STREAMS_DIR="/var/www/streams"
INDEX_DIR="/var/www/streams/audit_index"
mkdir -p "$INDEX_DIR"

for stream_dir in "$STREAMS_DIR"/*/; do
    stream_id=$(basename "$stream_dir")
    [ "$stream_id" = "metrics" ] && continue
    [ "$stream_id" = "audit_index" ] && continue
    [ ! -d "$stream_dir" ] && continue

    INDEX_FILE="$INDEX_DIR/${stream_id}.csv"
    [ ! -f "$INDEX_FILE" ] && echo "segment|start_ts|end_ts|start_epoch|end_epoch" > "$INDEX_FILE"

    indexed=$(awk -F"|" "NR>1{print \$1}" "$INDEX_FILE" 2>/dev/null)

    for seg in $(ls "$stream_dir"seg_*.ts 2>/dev/null | sort); do
        fname=$(basename "$seg")
        if ! echo "$indexed" | grep -q "^${fname}$"; then
            epoch=$(stat -c %Y "$seg")
            s=$((epoch - 4))
            start_ts=$(date -u -d "@$s" "+%Y-%m-%d %H:%M:%S")
            end_ts=$(date -u -d "@$epoch" "+%Y-%m-%d %H:%M:%S")
            echo "$fname|$start_ts|$end_ts|$s|$epoch" >> "$INDEX_FILE"
        fi
    done

    # Limpiar entradas sin archivo en disco
    TMP=$(mktemp)
    head -1 "$INDEX_FILE" > "$TMP"
    while IFS="|" read -r seg rest; do
        [ -f "$stream_dir/$seg" ] && echo "$seg|$rest"
    done < <(tail -n +2 "$INDEX_FILE") >> "$TMP"
    mv "$TMP" "$INDEX_FILE"
done
