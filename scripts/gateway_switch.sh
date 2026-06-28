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
  hn04)
    NEW_GW_NAME="CNS"
    NEW_GW_HOST="10.101.0.7"
    NEW_GW_PORT="1080"
    ;;
  # ---- Agregar nuevos gateways aqui (siguiente: hn05 con IP 10.101.0.8) --------
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
# PASO 3: stations.json — YA NO se muta el estado de gateways aqui
# =============================================================================
# Consolidacion de fuente de verdad (27 jun 2026): el gateway activo vive en
# gateway.conf (verdad de runtime) y en la DB (gateways.is_active, auto-sanada
# cada ciclo por el health_engine). El bloque "gateways" de stations.json era una
# TERCERA copia que se desincronizaba cuando algo bypasaba este script (paso el
# 27 jun: stations.json decia hn04 con hn01 activo). Las MCP tools leen los
# gateways de la DB (_gateways_from_db); stations.json solo es fallback de
# bootstrap si la tabla esta vacia. Por eso ya NO tocamos stations.json en el
# switch — se elimina esa copia como fuente de drift. stations.json sigue siendo
# la fuente de la LISTA de streams (stations[]), eso no cambia.
# =============================================================================
step "3/4  stations.json — sin cambios (estado de gateways vive en DB + gateway.conf)"
info "stations.json no se muta (gateway activo = gateway.conf + DB is_active)"

# =============================================================================
# PASO 4: Reiniciar streams para que tomen el nuevo gateway
# =============================================================================
# Los streams socks5 corren bajo stream-daemon (NO supervisor — supervisor quedo
# obsoleto, 'supervisorctl status' sale vacio). Reiniciar el daemon respawnea
# todos los ffmpeg, que releen gateway.conf y reconectan por el nuevo gateway.
# Los streams 'direct' tambien respawnean (sin cambio de ruta).
#
# OJO timing: el respawn resetea el contador seg_%05d -> corrompe la 1a parte de
# la hora EN CURSO de TODAS las estaciones. Correr al filo de hora (HH:00-02 UTC).
#
# Fuente unica de verdad: este restart lo hace SOLO el script. El failover
# automatico (_apply_gateway_switch en health_engine.py) llama a este script y ya
# NO reinicia el daemon por su cuenta (antes lo hacia -> doble restart redundante).
# =============================================================================
step "4/4  Reiniciando stream-daemon (los streams releen el gateway)"

if systemctl is-active --quiet stream-daemon; then
  systemctl restart stream-daemon
  sleep 10
  info "stream-daemon reiniciado"
else
  warning "stream-daemon no esta activo -- los streams NO migraron al nuevo gateway"
fi

# Verificacion REAL: a que SOCKS5 quedaron conectados los ffmpeg
echo ""
step "Verificacion: ffmpeg conectados a SOCKS5 (esperado -> $NEW_GW_HOST:$NEW_GW_PORT)"
ps aux | grep -o 'socks5-hostname [0-9.]*:1080' | sort | uniq -c || echo "  (sin ffmpeg con proxy todavia)"

# =============================================================================
# PASO 5: Actualizar is_active en PostgreSQL
# =============================================================================
step "5/5  Actualizando is_active en base de datos"

DB_ENV="/etc/mediadev-db.env"
if [ -f "$DB_ENV" ]; then
  # shellcheck source=/etc/mediadev-db.env
  source "$DB_ENV" 2>/dev/null
  PGPASSWORD="$PG_PASS" psql -h "$PG_HOST" -p "${PG_PORT:-5432}" -U "$PG_USER" -d "$PG_DB" \
    -c "UPDATE gateways SET is_active = (gateway_id = '$NEW_GW_ID');" &>/dev/null \
    && info "DB: is_active → $NEW_GW_ID" \
    || warning "No se pudo actualizar is_active en DB (no crítico)"
else
  warning "/etc/mediadev-db.env no encontrado — is_active no actualizado en DB"
fi

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
echo "    stream-daemon reiniciado (streams reconectados al nuevo gateway)"
echo ""
echo "  Para revertir:"
echo "    sudo /opt/media-ai/scripts/gateway_switch.sh $CURRENT_GW_ID"
echo ""
