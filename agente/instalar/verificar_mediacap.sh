#!/bin/bash
# Verifica (SOLO LECTURA) que vigia no pueda modificar ni eludir el forced command.
# Uso en mediaCAP, como root:  bash verificar_mediacap.sh <IP_MEDIAAPP>
# Sale con código 1 si algo no cumple. No cambia nada.
set -u
IP_APP="${1:?uso: verificar_mediacap.sh <IP_MEDIAAPP>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
fallas=0
mal() { echo "FALLA: $*"; fallas=$((fallas+1)); }
ok()  { echo "ok:    $*"; }

# 1) dueños y modos del manifiesto
while read -r ruta dueno grupo modo; do
  [[ -z "$ruta" || "$ruta" == \#* ]] && continue
  if [[ ! -e "$ruta" ]]; then mal "$ruta no existe"; continue; fi
  real="$(stat -c '%U %G %a' "$ruta")"
  [[ "$real" == "$dueno $grupo $modo" ]] && ok "$ruta $real" || mal "$ruta es '$real', se espera '$dueno $grupo $modo'"
  [[ -L "$ruta" ]] && mal "$ruta es un enlace simbólico"
done < "$DIR/permisos_mediacap.txt"

# 2) vigia no puede escribir nada de lo que define el forced command (ni sus directorios)
for p in /usr/local/bin /usr/local/bin/mediacap-ro /usr/local/bin/mediacap-vista /etc/ssh \
         /etc/ssh/authorized_keys /etc/ssh/authorized_keys/vigia /etc/mediadev /var/lib/mediadev-vigia \
         /var/lib/mediadev-vigia/vista.json /usr/lib/python3 /etc/systemd/system; do
  sudo -u vigia test -w "$p" && mal "vigia puede escribir $p" || ok "vigia no escribe $p"
done
sudo -u vigia test -r /etc/mediadev/stream_config.cache.json && mal "vigia puede leer el cache del daemon" \
  || ok "vigia no lee el cache del daemon"

# 3) cuenta: sin contraseña usable, sin sudo
passwd -S vigia | grep -qE '^vigia (L|LK) ' && ok "contraseña bloqueada" || mal "la contraseña de vigia no está bloqueada"
sudo -l -U vigia 2>/dev/null | grep -q "may run" && mal "vigia tiene reglas de sudo" || ok "sin sudo"

# 4) sshd efectivo para vigia desde mediaAPP (independiente de dónde esté la config)
T="$(sshd -T -C "user=vigia,host=mediaapp,addr=$IP_APP" 2>/dev/null)"
want() { echo "$T" | grep -qix "$1" && ok "sshd: $1" || mal "sshd: falta '$1'"; }
want "forcecommand /usr/local/bin/mediacap-ro"
want "authorizedkeysfile /etc/ssh/authorized_keys/%u"
want "allowtcpforwarding no"
want "permittty no"
want "permituserrc no"
want "x11forwarding no"
want "passwordauthentication no"
want "permituserenvironment no"

# 5) la llave autorizada trae restricciones también por sí misma
grep -qE '^restrict,from="[^"]+",command="/usr/local/bin/mediacap-ro" ' /etc/ssh/authorized_keys/vigia \
  && ok "authorized_keys con restrict,from,command" || mal "authorized_keys sin restrict/from/command"
[[ "$(grep -cvE '^\s*(#|$)' /etc/ssh/authorized_keys/vigia)" == 1 ]] && ok "una sola llave" || mal "hay más de una llave"

# 6) el script corre en modo aislado
head -1 /usr/local/bin/mediacap-ro | grep -q -- "python3 -I" && ok "mediacap-ro con python -I" || mal "mediacap-ro sin -I"

echo; [[ $fallas -eq 0 ]] && echo "TODO OK" || { echo "$fallas falla(s)"; exit 1; }
