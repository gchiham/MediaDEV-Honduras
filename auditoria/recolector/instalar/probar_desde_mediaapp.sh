#!/bin/bash
# Pruebas NEGATIVAS desde mediaAPP: cualquier cosa que pida el auditor devuelve solo el inventario.
#   bash probar_desde_mediaapp.sh ~/.ssh/mediacap_auditor
set -u
LLAVE=${1:?ruta a la llave privada del auditor}
SSH=(ssh -i "$LLAVE" -o BatchMode=yes -o ConnectTimeout=10 auditor@10.136.0.7)   # IP privada de mediaCAP
fallas=0
ref=$("${SSH[@]}" 2>/dev/null | sha256sum)
[ "$ref" != "$(printf '' | sha256sum)" ] && echo "OK    se recibe el inventario" || { echo "FALLA no llega el inventario"; exit 1; }

for intento in "id" "rm -rf /var/www/streams" "systemctl restart stream-daemon" "pkill ffmpeg" \
               "sudo -n true" "bash -i" "cat /etc/mediadev-db.env" "touch /tmp/auditor-estuvo-aqui"; do
  [ "$("${SSH[@]}" "$intento" 2>/dev/null | sha256sum)" = "$ref" ] \
    && echo "OK    '$intento' -> solo inventario" || { echo "FALLA '$intento' devolvió otra cosa"; fallas=$((fallas+1)); }
done
"${SSH[@]}" -t true 2>&1 | grep -qi "pty" && echo "OK    sin TTY" || echo "AVISO revisar TTY a mano"
timeout 15 "${SSH[@]}" -N -L 15432:127.0.0.1:5432 -o ExitOnForwardFailure=yes 2>/dev/null \
  && { echo "FALLA permite port forwarding"; fallas=$((fallas+1)); } || echo "OK    sin port forwarding"
echo ls | timeout 15 sftp -i "$LLAVE" -b - auditor@10.136.0.7 >/dev/null 2>&1 \
  && { echo "FALLA permite sftp"; fallas=$((fallas+1)); } || echo "OK    sin sftp"

echo; [ "$fallas" -eq 0 ] && echo "TODO OK" || { echo "$fallas FALLA(S)"; exit 1; }
