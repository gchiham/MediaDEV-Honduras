#!/bin/bash
# =============================================================================
# MediaDEV -- Cambio de gateway
# =============================================================================
# Cambia el gateway activo en UN solo lugar y propaga el cambio a todos los
# componentes que lo necesitan, sin tener que editar multiples archivos.
#
# COMPONENTES QUE ACTUALIZA AUTOMATICAMENTE:
#   1. /etc/mediadev/gateway.conf  -> fuente de verdad (IP del nuevo gateway)
#   2. /etc/privoxy/config         -> proxy HTTP (Privoxy reenvía a SOCKS5)
#   3. /opt/media-ai/config/stations.json -> estado enabled/disabled gateways
#   4. supervisorctl               -> reinicia streams con proxy
#
# USO:
#   sudo /opt/media-ai/scripts/gateway_switch.sh <gateway_id>
#
# EJEMPLOS:
#   sudo gateway_switch.sh hn01    # -> RPi Honduras 01  (10.101.0.2:1080)
#   sudo gateway_switch.sh hn02    # -> PC-LCE           (10.101.0.5:1080)
#   sudo gateway_switch.sh hn03    # -> RPi-Levi          (10.101.0.6:1080)
# =============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[OK]${NC} $1"; }
warning() { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step()    { echo -e "\n${CYAN}---- $1${NC}"; }

[ "$EUID" -ne 0 ] && error "Debe correr como root: sudo gateway_switch.sh <id>"

NEW_GW_ID="${1:-}"
[ -z "$NEW_GW_ID" ] && error "Uso: sudo gateway_switch.sh <gateway_id>   (ej: hn01, hn02, hn03)"

# =============================================================================
# MAPA DE GATEWAYS
# =============================================================================
# Para agregar un nuevo gateway:
#   1. Agrega un bloque case aqui con su ID, nombre, IP VPN y puerto SOCKS5
#   2. Instala WireGuard en el dispositivo y agrega como peer en MediaDEV
#   3. Instala microsocks o dante en el dispositivo
#   4. Registra el gateway en /opt/media-ai/config/stations.json
#
# IPs WireGuard usadas:
#   10.101.0.1  = MediaDEV servidor (hub)
#   10.101.0.2  = hn01 RPi Honduras 01
#   10.101.0.3  = PC Sedesol (Windows)
#   10.101.0.4  = PC Developer
#   10.101.0.5  = hn02 PC-LCE
#   10.101.0.6  = hn03 RPi-Levi
#   10.101.0.7+ = disponibles para futuros gateways
# =============================================================================
case "$NEW_GW_ID" in
  hn01)
    NEW_GW_NAME="RPi Honduras 01"
    NEW_GW_HOST="10.101.0.2"
    NEW_GW_PORT="1080"
    ;;
  hn02)
    NEW_GW_NAME="PC-LCE Gateway"
    NEW_GW_HOST="10.101.0.5"
    NEW_GW_PORT="1080"
    ;;
  hn03)
    NEW_GW_NAME="RPi-Levi"
    NEW_GW_HOST="10.101.0.6"
    NEW_GW_PORT="1080"
    ;;
  # ---- Agregar nuevos gateways aqui (siguiente: hn04 con IP 10.101.0.7) --------
  # hn04)
  #   NEW_GW_NAME="Nombre del gateway"
  #   NEW_GW_HOST="10.101.0.7"
  #   NEW_GW_PORT="1080"
  #   ;;
  *)
    error "Gateway desconocido: '$NEW_GW_ID'. Opciones: hn01, hn02, hn03"
    ;;
esac

# Leer gateway actual para el log y el mensaje de rollback
CURRENT_GW_ID="desconocido"
if [ -f /etc/mediadev/gateway.conf ]; then
  # shellcheck source=/etc/mediadev/gateway.conf
  source /etc/mediadev/gateway.conf 2>/dev/null || true
  CURRENT_GW_ID="${GW_ACTIVE_ID:-desconocido}"
fi

if [ "$NEW_GW_ID" = "$CURRENT_GW_ID" ]; then
  warning "El gateway '$NEW_GW_ID' ya esta activo. No hay nada que cambiar."
  exit 0
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}   MediaDEV -- Cambio de Gateway${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
echo "  De:  $CURRENT_GW_ID"
echo "  A:   $NEW_GW_ID -- $NEW_GW_NAME ($NEW_GW_HOST:$NEW_GW_PORT)"
echo "  Hora: $TIMESTAMP"
echo ""

# =============================================================================
# PASO 1: /etc/mediadev/gateway.conf
# =============================================================================
# Fuente de verdad. Los scripts de stream hacen "source" de este archivo
# al arrancar, entonces basta con reiniciarlos para que tomen el nuevo gateway.
# =============================================================================
step "1/4  Actualizando /etc/mediadev/gateway.conf"

mkdir -p /etc/mediadev

{
  echo "# MediaDEV -- Configuracion activa del gateway"
  echo "# =============================================="
  echo "# NO edites este archivo manualmente. Usa:"
  echo "#   sudo /opt/media-ai/scripts/gateway_switch.sh <gateway_id>"
  echo "#"
  echo "# Gateways disponibles:"
  echo "#   hn01 -- RPi Honduras 01         ->  10.101.0.2:1080"
  echo "#   hn02 -- PC-LCE                  ->  10.101.0.5:1080"
  echo "#   hn03 -- RPi-Levi                ->  10.101.0.6:1080"
  echo "#"
  echo "# Ultima modificacion: $TIMESTAMP"
  echo "# Cambiado de: $CURRENT_GW_ID  ->  $NEW_GW_ID"
  echo "# =============================================="
  echo ""
  echo "GW_ACTIVE_ID=\"$NEW_GW_ID\""
  echo "GW_SOCKS5_HOST=\"$NEW_GW_HOST\""
  echo "GW_SOCKS5_PORT=\"$NEW_GW_PORT\""
  echo 'GW_SOCKS5="${GW_SOCKS5_HOST}:${GW_SOCKS5_PORT}"'
} > /etc/mediadev/gateway.conf

info "gateway.conf -> GW_ACTIVE_ID=$NEW_GW_ID  GW_SOCKS5=$NEW_GW_HOST:$NEW_GW_PORT"

# =============================================================================
# PASO 2: Privoxy
# =============================================================================
# Privoxy es el proxy HTTP en 127.0.0.1:3128. Algunos streams usan
# -http_proxy http://127.0.0.1:3128 y Privoxy reenvía al SOCKS5 del gateway.
# La linea forward-socks5 debe apuntar al nuevo gateway.
# =============================================================================
step "2/4  Actualizando Privoxy (/etc/privoxy/config)"

PRIVOXY_CONF="/etc/privoxy/config"
if [ ! -f "$PRIVOXY_CONF" ]; then
  warning "Privoxy no encontrado en $PRIVOXY_CONF -- saltando"
else
  sed -i "s|^forward-socks5 .*$|forward-socks5  /  $NEW_GW_HOST:$NEW_GW_PORT  .|" "$PRIVOXY_CONF"

  if systemctl is-active --quiet privoxy; then
    systemctl reload privoxy 2>/dev/null || systemctl restart privoxy
    info "Privoxy recargado -> forward-socks5 / $NEW_GW_HOST:$NEW_GW_PORT"
  else
    warning "Privoxy no esta corriendo -- config actualizada, pero no se recargo"
  fi
fi

# =============================================================================
# PASO 3: stations.json
# =============================================================================
# Actualiza los flags enabled/disabled de los gateways y el campo "gateway"
# de cada station para que el panel y el health_engine reflejen la realidad.
# =============================================================================
step "3/4  Actualizando stations.json"

STATIONS_JSON="/opt/media-ai/config/stations.json"

if command -v python3 &>/dev/null && [ -f "$STATIONS_JSON" ]; then
  python3 - "$NEW_GW_ID" "$CURRENT_GW_ID" "$TIMESTAMP" << 'PYEOF'
import json, sys

new_id     = sys.argv[1]
current_id = sys.argv[2]
ts         = sys.argv[3]

with open("/opt/media-ai/config/stations.json") as f:
    data = json.load(f)

# Actualizar flags enabled en la seccion gateways
for gw_id, gw in data.get("gateways", {}).items():
    gw["enabled"] = (gw_id == new_id)
    if gw_id == new_id:
        gw["note"] = f"Activo desde {ts} -- cambiado por gateway_switch.sh"
    elif gw_id == current_id:
        gw["note"] = f"Inactivo desde {ts} -- cambiado por gateway_switch.sh"

# Actualizar el campo gateway de cada station que usaba el gateway anterior
for station in data.get("stations", []):
    if station.get("gateway") == current_id:
        station["gateway"] = new_id

with open("/opt/media-ai/config/stations.json", "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"  stations.json: {current_id} -> {new_id}")
PYEOF
  info "stations.json actualizado"
else
  warning "python3 no disponible o stations.json no existe -- saltando"
fi

# =============================================================================
# PASO 4: Reiniciar streams con proxy
# =============================================================================
# Solo reiniciamos los streams que usan --socks5-hostname (los que leen
# GW_SOCKS5 de gateway.conf). Los streams directos no necesitan reinicio.
# =============================================================================
step "4/4  Reiniciando streams con proxy"

PROXY_STREAMS=(
  "stream_fm_941"
  "stream_radio_choluteca"
  "stream_radio_satelite"
  "stream_suave_fm"
  "stream_xy_hrn"
  "stream_xy_sps"
  "stream_xy_tgu"
)

RESTARTED=0
SKIPPED=0

for stream in "${PROXY_STREAMS[@]}"; do
  if supervisorctl status "$stream" &>/dev/null 2>&1; then
    supervisorctl restart "$stream" > /dev/null 2>&1
    info "Reiniciado: $stream"
    RESTARTED=$((RESTARTED + 1))
  else
    warning "No en supervisorctl: $stream (revisar nombre)"
    SKIPPED=$((SKIPPED + 1))
  fi
done

# Esperar a que levanten y mostrar estado
sleep 3
echo ""
step "Estado de streams tras el cambio"
supervisorctl status | grep "stream_" || echo "  (no hay streams en supervisorctl)"

# =============================================================================
# RESUMEN
# =============================================================================
echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}   Gateway cambiado exitosamente${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "  Gateway activo:  $NEW_GW_ID -- $NEW_GW_NAME"
echo "  SOCKS5:          $NEW_GW_HOST:$NEW_GW_PORT"
echo ""
echo "  Cambios aplicados:"
echo "    /etc/mediadev/gateway.conf   (fuente de verdad)"
echo "    /etc/privoxy/config          (Privoxy reapuntado)"
echo "    /opt/media-ai/config/stations.json"
echo "    $RESTARTED streams reiniciados"
echo ""
echo "  Para revertir:"
echo "    sudo /opt/media-ai/scripts/gateway_switch.sh $CURRENT_GW_ID"
echo ""
