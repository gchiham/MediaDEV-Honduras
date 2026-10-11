#!/bin/bash
# Verifica la instalación del vigía en mediaAPP (fase 1). SOLO LECTURA: no cambia archivos,
# servicios ni la base. Imprime nombres de variables, nunca sus valores.
# Uso, como root:  bash verificar_mediaapp.sh [--enviar-prueba]
#   --enviar-prueba  además manda un mensaje de prueba por Telegram (único efecto externo).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/mediadev-agente
ENVF=/etc/mediadev-agent.env
PY="$APP/venv/bin/python"
USR=mediadev-vigia
fallas=0
mal() { echo "FALLA: $*"; fallas=$((fallas+1)); }
ok()  { echo "ok:    $*"; }
valor() { sed -n "s/^$1=//p" "$ENVF" | tail -1 | tr -d '\r"'\'''; }
correr() {   # corre con el usuario y el entorno del servicio, en una unidad transitoria
  systemd-run --quiet --wait --pipe --collect -p User="$USR" -p EnvironmentFile="$ENVF" "$@"
}

# 1) dueños y modos
while read -r ruta dueno grupo modo; do
  [[ -z "$ruta" || "$ruta" == \#* ]] && continue
  if [[ ! -e "$ruta" ]]; then mal "$ruta no existe"; continue; fi
  real="$(stat -c '%U %G %a' "$ruta")"
  [[ "$real" == "$dueno $grupo $modo" ]] && ok "$ruta $real" || mal "$ruta es '$real', se espera '$dueno $grupo $modo'"
done < "$DIR/permisos_mediaapp.txt"

# 2) el usuario del servicio no puede tocar su código, su unidad ni sus secretos
for p in "$APP" "$APP/vigia.py" "$APP/comun.py" "$APP/venv" "$ENVF" /etc/mediadev-vigia \
         /etc/mediadev-vigia/known_hosts /etc/systemd/system/mediadev-vigia.service; do
  sudo -u "$USR" test -w "$p" && mal "$USR puede escribir $p" || ok "$USR no escribe $p"
done
sudo -u "$USR" test -r "$ENVF" && mal "$USR puede leer $ENVF directamente" || ok "$ENVF solo lo lee systemd"

# 3) variables (solo nombres) y fase 1: IA apagada
for v in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID MEDIACAP_SSH DATABASE_URL_RO; do
  [[ -n "$(valor $v)" ]] && ok "$v definida" || mal "$v no definida"
done
[[ "$(valor VIGIA_AGENTE)" == "1" ]] && mal "VIGIA_AGENTE=1: la IA está encendida (fase 1 la deja apagada)" \
  || ok "IA apagada (VIGIA_AGENTE != 1)"

# 4) el bot es nuevo: su id no coincide con el de ningún otro token de este nodo
nuevo_id="$(valor TELEGRAM_BOT_TOKEN | cut -d: -f1)"
echo "id del bot del vigía: $nuevo_id (comparar a mano con el del monitor en mediaCAP)"
otros="$(grep -rhoE '[0-9]{8,10}:[A-Za-z0-9_-]{30,}' /etc/*.env /opt/destroyer /opt/media-app 2>/dev/null \
         | grep -v "^$nuevo_id:" | cut -d: -f1 | sort -u | tr '\n' ' ')"
[[ -n "$otros" ]] && echo "otros bots en este nodo (ids): $otros"
dup="$(grep -rlE "\b$nuevo_id:[A-Za-z0-9_-]{30,}" /etc/*.env /opt/destroyer /opt/media-app 2>/dev/null | grep -vx "$ENVF")"
[[ -z "$dup" ]] && ok "el bot del vigía no se reutiliza en este nodo" \
  || mal "el token del vigía también aparece en: $(echo $dup)"
prohibidos="$(valor VIGIA_BOT_PROHIBIDOS)"
[[ -n "$prohibidos" ]] && ok "VIGIA_BOT_PROHIBIDOS definida" || echo "aviso: definir VIGIA_BOT_PROHIBIDOS con los ids de los bots viejos"

# 5) SSH a mediaCAP: el comando forzado responde y no se puede eludir
dest="$(valor MEDIACAP_SSH)"
SSH=(ssh -i /etc/mediadev-vigia/id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes
     -o UserKnownHostsFile=/etc/mediadev-vigia/known_hosts)
r="$(sudo -u "$USR" "${SSH[@]}" "$dest" chequeo 2>&1)"
echo "$r" | grep -q '"estaciones"' && ok "chequeo por ssh responde" || mal "chequeo por ssh no responde: ${r:0:200}"
r="$(sudo -u "$USR" "${SSH[@]}" "$dest" bash 2>&1)"
echo "$r" | grep -q 'comando no permitido' && ok "un shell es rechazado" || mal "un shell no fue rechazado: ${r:0:200}"
r="$(sudo -u "$USR" "${SSH[@]}" -tt "$dest" 2>&1 < /dev/null)"
# seguro = sshd niega la pty (el cliente corta sin ejecutar nada) o el comando forzado responde error
echo "$r" | grep -qE 'PTY allocation request failed|"error"' && ! echo "$r" | grep -qE '\$ |# ' \
  && ok "sin pty ni shell interactivo" || mal "sesión interactiva: ${r:0:200}"
sudo -u "$USR" "${SSH[@]}" -o ExitOnForwardFailure=yes -R 127.0.0.1:0:127.0.0.1:22 "$dest" chequeo \
  </dev/null >/dev/null 2>&1 && mal "el reenvío de puertos fue aceptado" || ok "reenvío de puertos rechazado"

# 6) base de datos: rol de solo lectura y con lo mínimo
r="$(correr "$PY" -c '
import os, psycopg2
c = psycopg2.connect(os.environ["DATABASE_URL_RO"], connect_timeout=8); cur = c.cursor()
cur.execute("SHOW default_transaction_read_only"); print("ro", cur.fetchone()[0])
cur.execute("SELECT has_table_privilege(%s, %s)", ("public.capture_config", "INSERT")); print("insert", cur.fetchone()[0])
cur.execute("SELECT has_table_privilege(%s, %s)", ("public.mediadev_events", "SELECT")); print("eventos", cur.fetchone()[0])
cur.execute("SELECT count(*) FROM capture_config WHERE is_enabled"); print("habilitadas", cur.fetchone()[0])
c.close()' 2>&1)"
echo "$r" | grep -qx "ro on" && ok "DB: transacciones de solo lectura" || mal "DB: no es read-only ($r)"
echo "$r" | grep -qx "insert False" && ok "DB: sin INSERT" || mal "DB: tiene INSERT"
echo "$r" | grep -qx "eventos False" && ok "DB: sin tablas de fase 2" || mal "DB: tiene permisos de más"
echo "$r" | grep -q "^habilitadas [0-9]" && ok "DB: $(echo "$r" | grep ^habilitadas)" || mal "DB: no lee capture_config"

# 7) una vuelta completa simulada (no manda a Telegram ni guarda estado)
r="$(correr -E VIGIA_SIMULAR=1 "$PY" "$APP/vigia.py" --simular 2>&1)"
[[ $? -eq 0 ]] && ok "vuelta simulada sin errores" || mal "la vuelta simulada falló: ${r:0:300}"
echo "$r" | sed -n '/SIMULADO/,$p' | head -20

# 8) unidad y timer (lectura)
systemctl is-enabled mediadev-vigia.timer >/dev/null 2>&1 && ok "timer habilitado" || echo "aviso: timer todavía no habilitado"

# 9) opcional: mensaje de prueba real
if [[ "${1:-}" == "--enviar-prueba" ]]; then
  correr "$PY" "$APP/vigia.py" --probar-telegram && ok "Telegram: prueba enviada" || mal "Telegram: falló la prueba"
fi

echo; [[ $fallas -eq 0 ]] && echo "TODO OK" || { echo "$fallas falla(s)"; exit 1; }
