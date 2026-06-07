#!/bin/bash
# Borra segmentos .ts que no están referenciados en ningún m3u8 activo
STREAMS_DIR="/var/www/streams"

for dir in "$STREAMS_DIR"/*/; do
    m3u8="$dir/index.m3u8"
    [ -f "$m3u8" ] || continue

    # Obtener lista de segmentos activos
    active=$(grep "\.ts$" "$m3u8" | tr -d "\r")

    # Borrar .ts que no están en el m3u8 y tienen más de 5 minutos
    find "$dir" -name "seg_*.ts" -mmin +5 | while read -r f; do
        basename=$(basename "$f")
        if ! echo "$active" | grep -q "^$basename$"; then
            rm -f "$f"
        fi
    done
done
