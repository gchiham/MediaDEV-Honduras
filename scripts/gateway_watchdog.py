#!/usr/bin/env python3
"""
MediaDEV — Gateway Watchdog v1.0
==================================
Corre cada minuto via cron. Verifica que el gateway activo funciona y hace
failover automático si detecta falla, con failback cuando el primario vuelve.

INSTALACIÓN (cron):
  crontab -e
  * * * * * /usr/bin/python3 /opt/media-ai/scripts/gateway_watchdog.py >> /var/log/gateway_watchdog.log 2>&1

LÓGICA:
  1. Lee el gateway activo desde /etc/mediadev/gateway.conf
  2. Lo testea: ping WireGuard (rápido) + curl SOCKS5 (definitivo)
  3. Si falla FAIL_THRESHOLD checks seguidos → failover al siguiente por prioridad
  4. Si el activo no es el de mayor prioridad disponible → failback automático
  5. Notifica por Telegram cada evento importante

ESTADO:
  Se guarda en /etc/mediadev/watchdog_state.json entre ejecuciones del cron.
  Permite contar fallas consecutivas sin perder el conteo entre minutos.

ARCHIVOS QUE LEE:
  /etc/mediadev/gateway.conf          → gateway activo actualmente
  /opt/media-ai/config/stations.json  → inventario de gateways con prioridades
  /opt/media-ai/scripts/gateway_switch.sh → para ejecutar el cambio

ARCHIVOS QUE ESCRIBE:
  /etc/mediadev/watchdog_state.json   → estado persistente entre ejecuciones
  /var/log/gateway_watchdog.log       → log (vía cron redirect)
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

# Cuántos checks fallidos consecutivos antes de hacer failover
# Con cron cada minuto: 2 = esperar 2 minutos antes de cambiar
FAIL_THRESHOLD = 2

# Timeout para el test de ping (segundos)
PING_TIMEOUT = 5

# Timeout para el test de SOCKS5 via curl (segundos)
SOCKS5_TIMEOUT = 10

# URL de prueba para verificar que el SOCKS5 realmente funciona
# Se hace una petición HTTP a través del proxy para confirmar conectividad real
SOCKS5_TEST_URL = "https://ifconfig.me/ip"

# Archivos de configuración
GATEWAY_CONF    = Path("/etc/mediadev/gateway.conf")
STATIONS_JSON   = Path("/opt/media-ai/config/stations.json")
STATE_FILE      = Path("/etc/mediadev/watchdog_state.json")
GATEWAY_SWITCH  = Path("/opt/media-ai/scripts/gateway_switch.sh")

# Telegram — mismo bot que usa el monitor de WireGuard
# Los valores se leen del monitor.py para no duplicar credenciales
TELEGRAM_TOKEN   = "5955525376:AAE63GJ5CwbJ6DWTTZnGch8VAVlhsM2zulY"
TELEGRAM_CHAT_ID = 1687412948

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchdog")


# =============================================================================
# TELEGRAM
# =============================================================================

def telegram(msg: str) -> None:
    """
    Manda un mensaje al chat de Telegram del equipo.
    No lanza excepción si falla — el watchdog no debe morir por esto.
    """
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=8)
        log.info(f"Telegram enviado: {msg[:80]}")
    except Exception as e:
        log.warning(f"Telegram falló (no crítico): {e}")


# =============================================================================
# LECTURA DE CONFIGURACIÓN
# =============================================================================

def read_active_gateway() -> str:
    """
    Lee el gateway actualmente activo desde gateway.conf.
    Retorna el ID (ej: 'hn01') o None si no puede leerlo.
    """
    if not GATEWAY_CONF.exists():
        log.error(f"No existe {GATEWAY_CONF}")
        return None
    for line in GATEWAY_CONF.read_text().splitlines():
        line = line.strip()
        if line.startswith("GW_ACTIVE_ID="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def load_gateways() -> dict:
    """
    Carga el inventario de gateways desde stations.json.
    Retorna dict: {gateway_id: {name, vpn_ip, socks5, priority, enabled, ...}}
    """
    if not STATIONS_JSON.exists():
        log.error(f"No existe {STATIONS_JSON}")
        return {}
    data = json.loads(STATIONS_JSON.read_text())
    return data.get("gateways", {})


def load_state() -> dict:
    """
    Carga el estado persistente del watchdog.
    Si no existe o está corrupto, retorna estado limpio.
    """
    default = {
        "fail_count": 0,          # fallas consecutivas del gateway activo
        "last_failover": None,     # ISO timestamp del último failover
        "last_ok": None,           # ISO timestamp del último check exitoso
        "alerted_no_gw": False,    # para no repetir alerta "sin gateways"
    }
    try:
        if STATE_FILE.exists():
            saved = json.loads(STATE_FILE.read_text())
            default.update(saved)
    except Exception as e:
        log.warning(f"Estado corrupto, usando default: {e}")
    return default


def save_state(state: dict) -> None:
    """Persiste el estado para la próxima ejecución del cron."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# =============================================================================
# TESTS DE CONECTIVIDAD
# =============================================================================

def test_ping(ip: str) -> bool:
    """
    Ping al gateway por la VPN WireGuard.
    Rápido (~1s). Verifica que el túnel WireGuard está activo.
    No verifica que el SOCKS5 funcione — para eso está test_socks5.
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "2", "-W", str(PING_TIMEOUT), ip],
            capture_output=True, timeout=PING_TIMEOUT + 3
        )
        ok = result.returncode == 0
        log.debug(f"Ping {ip}: {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        log.debug(f"Ping {ip} error: {e}")
        return False


def test_socks5(host: str, port: str) -> bool:
    """
    Test real del proxy SOCKS5: hace una petición HTTP a través del proxy.
    Más lento (~3-8s) pero definitivo — verifica que los streams pueden pasar.

    Si el ping falla, ni intentamos esto (ahorra tiempo).
    """
    try:
        result = subprocess.run(
            [
                "curl", "-s",
                "--socks5-hostname", f"{host}:{port}",
                "--max-time", str(SOCKS5_TIMEOUT),
                "--connect-timeout", "5",
                "-o", "/dev/null",   # descartar output
                "-w", "%{http_code}",  # solo nos interesa el código HTTP
                SOCKS5_TEST_URL,
            ],
            capture_output=True, text=True, timeout=SOCKS5_TIMEOUT + 5
        )
        http_code = result.stdout.strip()
        ok = http_code == "200"
        log.debug(f"SOCKS5 {host}:{port} → HTTP {http_code}: {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        log.debug(f"SOCKS5 test error: {e}")
        return False


def is_gateway_healthy(gw_id: str, gw_info: dict) -> bool:
    """
    Verifica si un gateway está funcional.
    Primero hace ping (rápido), luego SOCKS5 (definitivo).
    Retorna True solo si ambos pasan.
    """
    vpn_ip = gw_info.get("vpn_ip", "")
    socks5 = gw_info.get("socks5", "").replace("socks5://", "")
    host, _, port = socks5.partition(":")
    port = port or "1080"

    log.info(f"Testeando {gw_id} ({vpn_ip})...")

    # Test 1: Ping via WireGuard (rápido, pero algunos equipos bloquean ICMP)
    # No es bloqueante — si falla igual intentamos SOCKS5
    ping_ok = test_ping(vpn_ip)
    if not ping_ok:
        log.info(f"  {gw_id}: ping bloqueado/fallido — verificando SOCKS5 igual")

    # Test 2: SOCKS5 real (definitivo)
    # Es el único test que importa. Un equipo puede bloquear ICMP pero
    # tener SOCKS5 perfectamente activo (caso PC-LCE).
    if not test_socks5(host, port):
        log.info(f"  {gw_id}: SOCKS5 FALLÓ ({host}:{port}) — gateway no disponible")
        return False

    log.info(f"  {gw_id}: OK ✓  ping={'OK' if ping_ok else 'bloqueado'} SOCKS5=OK")
    return True


# =============================================================================
# FAILOVER / FAILBACK
# =============================================================================

def find_best_gateway(gateways: dict, exclude_id: str = None) -> tuple:
    """
    Encuentra el mejor gateway disponible ordenado por prioridad (menor = mejor).
    Excluye el gateway actual (que sabemos que está caído).

    Retorna (gateway_id, gateway_info) o (None, None) si no hay ninguno.
    """
    # Ordenar por prioridad ascendente (1 es el mejor)
    candidates = [
        (gw_id, gw)
        for gw_id, gw in gateways.items()
        if gw_id != exclude_id
    ]
    candidates.sort(key=lambda x: x[1].get("priority", 99))

    for gw_id, gw_info in candidates:
        log.info(f"Probando candidato: {gw_id} (prioridad {gw_info.get('priority')})")
        if is_gateway_healthy(gw_id, gw_info):
            return gw_id, gw_info

    return None, None


def do_switch(new_gw_id: str) -> bool:
    """
    Ejecuta gateway_switch.sh para cambiar el gateway activo.
    Retorna True si el cambio fue exitoso.
    """
    if not GATEWAY_SWITCH.exists():
        log.error(f"No existe {GATEWAY_SWITCH}")
        return False
    try:
        result = subprocess.run(
            ["bash", str(GATEWAY_SWITCH), new_gw_id],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log.info(f"gateway_switch.sh {new_gw_id} exitoso")
            return True
        else:
            log.error(f"gateway_switch.sh falló: {result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"Error ejecutando gateway_switch.sh: {e}")
        return False


# =============================================================================
# LÓGICA PRINCIPAL
# =============================================================================

def main():
    now_iso = datetime.now(timezone.utc).isoformat()

    # Cargar configuración y estado
    gateways   = load_gateways()
    state      = load_state()
    active_id  = read_active_gateway()

    if not gateways:
        log.error("No se pudo cargar stations.json")
        sys.exit(1)

    if not active_id:
        log.error("No se pudo leer el gateway activo de gateway.conf")
        sys.exit(1)

    active_gw = gateways.get(active_id)
    if not active_gw:
        log.error(f"Gateway activo '{active_id}' no está en stations.json")
        sys.exit(1)

    log.info(f"Gateway activo: {active_id} ({active_gw.get('name')}) — chequeando...")

    # ── Test del gateway activo ────────────────────────────────────────────────
    active_healthy = is_gateway_healthy(active_id, active_gw)

    if active_healthy:
        # ── Gateway activo OK ─────────────────────────────────────────────────
        if state["fail_count"] > 0:
            log.info(f"{active_id} recuperado (tenía {state['fail_count']} fallas)")

        state["fail_count"] = 0
        state["last_ok"]    = now_iso
        state["alerted_no_gw"] = False

        # ── Failback: verificar si hay un gateway de mayor prioridad disponible ─
        # Si el activo no es el de menor número de prioridad (el mejor),
        # buscar si alguno de mayor prioridad está sano y hacer failback.
        active_priority = active_gw.get("priority", 99)

        better_candidates = [
            (gw_id, gw)
            for gw_id, gw in gateways.items()
            if gw.get("priority", 99) < active_priority
        ]
        better_candidates.sort(key=lambda x: x[1].get("priority", 99))

        for best_id, best_gw in better_candidates:
            log.info(f"Failback: verificando si {best_id} (prioridad {best_gw.get('priority')}) está disponible...")
            if is_gateway_healthy(best_id, best_gw):
                log.info(f"Failback: {best_id} está sano y tiene mayor prioridad — restaurando")
                if do_switch(best_id):
                    msg = (
                        f"✅ Gateway primario restaurado\n"
                        f"━━━━━━━━━━━━\n\n"
                        f"🔄 Activo ahora: {best_id} — {best_gw.get('name')}\n"
                        f"📡 SOCKS5:       {best_gw.get('vpn_ip')}:1080\n"
                        f"⬆️  Prioridad:   {best_gw.get('priority')} (más alta disponible)\n"
                        f"🕐 Hora:         {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
                    )
                    telegram(msg)
                    log.info(msg)
                break  # Solo hacemos failback al mejor disponible

        save_state(state)
        return

    # ── Gateway activo con problemas ──────────────────────────────────────────
    state["fail_count"] += 1
    log.warning(f"{active_id} FALLÓ (falla #{state['fail_count']} de {FAIL_THRESHOLD} para failover)")

    if state["fail_count"] < FAIL_THRESHOLD:
        # Aún no alcanzamos el umbral — esperamos otro ciclo antes de actuar
        log.info(f"Esperando {FAIL_THRESHOLD - state['fail_count']} checks más antes de failover")
        save_state(state)
        return

    # ── Umbral alcanzado: hacer failover ──────────────────────────────────────
    log.warning(f"Umbral de {FAIL_THRESHOLD} fallas alcanzado — iniciando failover")

    new_id, new_gw = find_best_gateway(gateways, exclude_id=active_id)

    if new_id is None:
        # No hay ningún gateway disponible
        log.error("SIN gateways disponibles — los streams pueden fallar")
        if not state["alerted_no_gw"]:
            telegram(
                f"🚨 ALERTA CRÍTICA — Sin gateways\n"
                f"━━━━━━━━━━━━\n\n"
                f"❌ Gateway caído: {active_id} — {active_gw.get('name')}\n"
                f"❌ Sin alternativas disponibles\n"
                f"📺 Los 7 streams pueden estar fallando\n"
                f"🕐 Hora: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
                f"👉 Acción requerida: revisar gateways"
            )
            state["alerted_no_gw"] = True
        save_state(state)
        return

    # Ejecutar el cambio
    log.info(f"Failover: {active_id} → {new_id}")
    if do_switch(new_id):
        state["fail_count"]   = 0
        state["last_failover"] = now_iso
        state["alerted_no_gw"] = False

        msg = (
            f"⚠️ Failover automático ejecutado\n"
            f"━━━━━━━━━━━━\n\n"
            f"🔴 Caído:     {active_id} — {active_gw.get('name')}\n"
            f"🟢 Activo:    {new_id} — {new_gw.get('name')}\n"
            f"📡 SOCKS5:    {new_gw.get('vpn_ip')}:1080\n"
            f"🕐 Hora:      {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"📺 Streams:   7 reiniciados automáticamente"
        )
        telegram(msg)
        log.info(f"Failover exitoso: {active_id} → {new_id}")
    else:
        log.error(f"Failover a {new_id} FALLÓ")
        telegram(
            f"🚨 Error en failover\n"
            f"━━━━━━━━━━━━\n\n"
            f"❌ Caído: {active_id} — {active_gw.get('name')}\n"
            f"❌ Falló cambio a: {new_id}\n"
            f"🕐 Hora: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"👉 Acción requerida: revisar gateway_switch.sh"
        )

    save_state(state)


if __name__ == "__main__":
    main()
