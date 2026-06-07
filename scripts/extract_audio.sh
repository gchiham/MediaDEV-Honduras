#!/bin/bash
# Extrae audio de un stream en un rango de tiempo
# Uso: extract_audio.sh <stream_id> <start "YYYY-MM-DD HH:MM:SS"> <end "YYYY-MM-DD HH:MM:SS"> [output.mp3]
#
# Ejemplo:
#   extract_audio.sh xy_hrn "2026-06-05 08:00:00" "2026-06-05 09:00:00" hora8.mp3

STREAM_ID="$1"
START_TS="$2"
END_TS="$3"
OUTPUT="${4:-/tmp/${STREAM_ID}_$(echo $START_TS | tr  : _).mp3}"

INDEX="/var/www/streams/audit_index/${STREAM_ID}.csv"
STREAM_DIR="/var/www/streams/${STREAM_ID}"

if [ ! -f "$INDEX" ]; then
    echo "ERROR: No hay índice para $STREAM_ID. Ejecuta primero audit_index.sh"
    exit 1
fi

START_EPOCH=$(date -u -d "$START_TS" +%s 2>/dev/null)
END_EPOCH=$(date -u -d "$END_TS" +%s 2>/dev/null)

if [ -z "$START_EPOCH" ] || [ -z "$END_EPOCH" ]; then
    echo "ERROR: Formato de fecha inválido. Usa: YYYY-MM-DD HH:MM:SS"
    exit 1
fi

echo "🔍 Buscando segmentos entre $START_TS y $END_TS..."

# Encontrar segmentos que intersectan el rango
SEGMENTS=()
while IFS="|" read -r seg start_ts end_ts s_epoch e_epoch; do
    [ "$seg" = "segment" ] && continue
    if [ "$s_epoch" -le "$END_EPOCH" ] && [ "$e_epoch" -ge "$START_EPOCH" ]; then
        f="$STREAM_DIR/$seg"
        [ -f "$f" ] && SEGMENTS+=("$f")
    fi
done < "$INDEX"

COUNT=${#SEGMENTS[@]}
if [ "$COUNT" -eq 0 ]; then
    echo "ERROR: No se encontraron segmentos en ese rango de tiempo"
    echo "Rango disponible en índice:"
    awk -F"|" NR==2{print   Desde: } END{print   Hasta: } "$INDEX"
    exit 1
fi

echo "✓ $COUNT segmentos encontrados (~$((COUNT * 4)) segundos = $((COUNT * 4 / 60)) minutos)"
echo "📁 Exportando a: $OUTPUT"

# Concatenar y convertir a MP3
(for f in "${SEGMENTS[@]}"; do cat "$f"; done) | \
    ffmpeg -y -loglevel warning \
        -i pipe:0 \
        -c:a libmp3lame -b:a 64k -ac 1 \
        "$OUTPUT"

if [ $? -eq 0 ]; then
    SIZE=$(du -h "$OUTPUT" | cut -f1)
    echo "✅ Listo: $OUTPUT ($SIZE)"
else
    echo "❌ Error al convertir"
    exit 1
fi
