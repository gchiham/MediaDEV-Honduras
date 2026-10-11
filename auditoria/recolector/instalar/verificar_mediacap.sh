#!/bin/bash
# Verifica en mediaCAP (como root) que el acceso del auditor es de solo lectura. NO modifica nada.
#   bash verificar_mediacap.sh [ruta/permisos_mediacap.txt]
set -u
PERMISOS=${1:-$(dirname "$0")/permisos_mediacap.txt}
IP_MEDIAAPP=10.136.0.6
INV=/var/lib/mediacap-auditor/inventario.json
fallas=0
ok()   { echo "OK    $*"; }
mal()  { echo "FALLA $*"; fallas=$((fallas+1)); }

id auditor >/dev/null 2>&1 || { mal "no existe el usuario auditor"; exit 1; }
[ "$(id -u auditor)" -ne 0 ] && ok "auditor no es root" || mal "auditor tiene uid 0"
id -nG auditor | grep -qwE "sudo|wheel|adm|root|docker|vigia" \
  && mal "auditor en grupo privilegiado: $(id -nG auditor)" || ok "sin grupos privilegiados ($(id -nG auditor))"
sudo -l -U auditor 2>/dev/null | grep -q "may run" && mal "auditor tiene reglas sudo" || ok "sin sudo"
passwd -S auditor 2>/dev/null | awk '{print $2}' | grep -qE "^(L|LK|NP)$" \
  && ok "contraseña bloqueada" || mal "auditor tiene contraseña utilizable"

# Permisos: nada que defina o ejecute el auditor es escribible por él.
grep -vE '^\s*(#|$)' "$PERMISOS" | while read -r ruta dueno grupo modo; do
  if [ ! -e "$ruta" ]; then echo "FALLA no existe $ruta"; continue; fi
  real=$(stat -c '%U %G %a' "$ruta")
  [ "$real" = "$dueno $grupo $modo" ] && echo "OK    $ruta $real" || echo "FALLA $ruta es '$real', se espera '$dueno $grupo $modo'"
  sudo -u auditor test -w "$ruta" && echo "FALLA auditor puede escribir $ruta"
done | tee /dev/stderr | grep -q '^FALLA' && fallas=$((fallas+1))

# sshd: lo que realmente aplica al usuario (no lo que dice el archivo).
efectivo=$(sshd -T -C "user=auditor,host=x,addr=$IP_MEDIAAPP" 2>&1)
for esperado in "forcecommand /bin/cat $INV" "permittty no" "allowtcpforwarding no" \
                "allowagentforwarding no" "x11forwarding no" "permittunnel no" \
                "passwordauthentication no" "authorizedkeysfile /etc/ssh/authorized_keys/%u"; do
  echo "$efectivo" | grep -qixF "$esperado" && ok "sshd: $esperado" || mal "sshd no aplica: $esperado"
done
llave=/etc/ssh/authorized_keys/auditor
[ "$(grep -cvE '^\s*(#|$)' "$llave")" -eq 1 ] && ok "una sola llave" || mal "$llave no tiene exactamente 1 llave"
grep -vE '^\s*(#|$)' "$llave" | grep -q "^restrict,from=\"$IP_MEDIAAPP\",command=\"/bin/cat $INV\" " \
  && ok "llave con restrict + from + command" || mal "la llave no tiene restrict,from=\"$IP_MEDIAAPP\",command=..."

# Servicio: sandbox de solo lectura.
props=$(systemctl show mediacap-auditor-inventario.service -p ProtectSystem,ReadWritePaths,NoNewPrivileges,User)
for esperado in "ProtectSystem=strict" "ReadWritePaths=/var/lib/mediacap-auditor" "NoNewPrivileges=yes"; do
  echo "$props" | grep -qxF "$esperado" && ok "servicio: $esperado" || mal "servicio sin $esperado"
done
systemctl is-active --quiet mediacap-auditor-inventario.timer && ok "timer activo" || mal "timer inactivo"

# Inventario: fresco, JSON válido, sin secretos.
if [ -f "$INV" ]; then
  edad=$(( $(date +%s) - $(stat -c %Y "$INV") ))
  [ "$edad" -lt 25200 ] && ok "inventario de hace $((edad/60)) min" || mal "inventario viejo ($((edad/3600)) h)"
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$INV" && ok "JSON válido" || mal "JSON inválido"
  grep -qE '[0-9]{8,10}:[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9_-]{16,}' "$INV" \
    && mal "el inventario contiene algo con forma de secreto" || ok "sin patrones de secreto"
  # Cruce con los valores reales (solo variables de clave/token/llave; PG_USER=destroyer coincide
  # con /opt/destroyer y no es secreto). Nunca imprime el valor.
  fugas=0
  for f in /etc/*.env; do
    while IFS='=' read -r k v; do
      v=${v%\"}; v=${v#\"}
      [ ${#v} -ge 8 ] && grep -qF -- "$v" "$INV" && { mal "el inventario contiene el valor de $k ($f)"; fugas=1; }
    done < <(grep -E '^[A-Za-z_]*(PASS|TOKEN|SECRET|KEY)[A-Za-z_]*=' "$f")
  done
  [ "$fugas" -eq 0 ] && ok "ningún valor de /etc/*.env aparece en el inventario"
else
  mal "no existe $INV"
fi

echo; [ "$fallas" -eq 0 ] && echo "TODO OK" || { echo "$fallas FALLA(S)"; exit 1; }
