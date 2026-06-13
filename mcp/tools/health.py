"""
tools/health.py — get_service_health()
Health del ecosistema: gateways, VPN, proxies, DB.
"""
import subprocess
import re
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from db import qall, qone, db_ping

HN_TZ = timezone(timedelta(hours=-6))

STATIONS_JSON = Path("/opt/media-ai/config/stations.json")
GATEWAY_CONF  = Path("/etc/mediadev/gateway.conf")


def _run(cmd: list[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


def _wireguard_status() -> dict:
    """Lee el estado de WireGuard vía 'wg show wg0'."""
    out = _run(["wg", "show", "wg0"])
    if not out:
        return {"status": "down", "peers": 0, "active_peers": 0}

    peers = re.findall(r"^peer:", out, re.MULTILINE)
    # Contar peers con handshake reciente (< 5 minutos)
    handshakes = re.findall(r"latest handshake: (.+)", out)
    active = 0
    for hs in handshakes:
        hs_low = hs.lower()
        if "second" in hs_low:
            active += 1
        elif "minute" in hs_low:
            m = re.search(r"(\d+) minute", hs_low)
            if m and int(m.group(1)) <= 5:
                active += 1

    return {
        "status": "up" if peers else "no_peers",
        "peers":  len(peers),
        "active_peers": active,
    }


def _active_gateway_from_conf() -> str | None:
    """Lee el gateway activo desde /etc/mediadev/gateway.conf."""
    try:
        content = GATEWAY_CONF.read_text()
        m = re.search(r"GW_SOCKS5=socks5://([^:]+):", content)
        if m:
            return m.group(1)  # VPN IP del gateway activo
    except Exception:
        pass
    return None


def _gateways_from_stations() -> tuple[list[dict], str | None]:
    """
    Lee los gateways desde stations.json (fuente de verdad cuando la DB está vacía).
    Retorna (lista_gateways, id_activo).
    """
    try:
        data = json.loads(STATIONS_JSON.read_text())
        gateways_cfg = data.get("gateways", {})
    except Exception:
        return [], None

    active_vpn_ip = _active_gateway_from_conf()
    active_id = None

    gateways = []
    for gw_id, cfg in gateways_cfg.items():
        vpn_ip = cfg.get("vpn_ip", "")
        is_enabled = cfg.get("enabled", False)

        if active_vpn_ip and vpn_ip == active_vpn_ip:
            active_id = gw_id
        elif is_enabled and active_id is None:
            active_id = gw_id

        # Verificar conectividad básica via WireGuard ping (no bloqueante)
        ping_ok = None
        if vpn_ip:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", vpn_ip],
                capture_output=True, timeout=5
            )
            ping_ok = result.returncode == 0

        gateways.append({
            "id":          gw_id,
            "name":        cfg.get("name"),
            "vpn_ip":      vpn_ip,
            "status":      "healthy" if is_enabled else "failed",
            "enabled":     is_enabled,
            "role":        cfg.get("role"),
            "priority":    cfg.get("priority"),
            "vpn_reachable": ping_ok,
            # No hay métricas DB — gateway_health_log vacío
            "score":       None,
            "last_heartbeat": None,
        })

    return gateways, active_id


def _gateways_from_db() -> tuple[list[dict], str | None]:
    """Lee gateways desde PostgreSQL (cuando la tabla tiene datos)."""
    rows = qall("""
        SELECT
            g.gateway_id, g.name, g.wg_ip, g.status, g.score,
            g.priority, g.maintenance, g.last_heartbeat,
            h.cpu_pct, h.ram_pct, h.latency_ms,
            h.internet_ok, h.socks5_ok, h.external_ip
        FROM gateways g
        LEFT JOIN LATERAL (
            SELECT cpu_pct, ram_pct, latency_ms, internet_ok, socks5_ok, external_ip
            FROM gateway_health_log
            WHERE gateway_id = g.gateway_id
            ORDER BY recorded_at DESC LIMIT 1
        ) h ON TRUE
        ORDER BY g.priority ASC
    """)
    if not rows:
        return [], None

    def _fmt_ago(dt) -> str | None:
        if not dt:
            return None
        now = datetime.now(timezone.utc)
        diff = (now - dt.astimezone(timezone.utc)).total_seconds()
        if diff < 60:  return f"{int(diff)}s ago"
        if diff < 3600: return f"{int(diff/60)}m ago"
        return f"{int(diff/3600)}h ago"

    active_id = None
    gateways = []
    for r in rows:
        if r.get("status") == "healthy" and not r.get("maintenance") and not active_id:
            active_id = r["gateway_id"]
        gateways.append({
            "id":             r["gateway_id"],
            "name":           r["name"],
            "vpn_ip":         r.get("wg_ip"),
            "status":         r.get("status") or "unknown",
            "score":          r.get("score") or 0,
            "maintenance":    bool(r.get("maintenance")),
            "last_heartbeat": _fmt_ago(r.get("last_heartbeat")),
            "cpu_pct":        r.get("cpu_pct"),
            "ram_pct":        r.get("ram_pct"),
            "latency_ms":     r.get("latency_ms"),
            "socks5_ok":      r.get("socks5_ok"),
            "internet_ok":    r.get("internet_ok"),
            "external_ip":    _mask_ip(r.get("external_ip")),
        })
    return gateways, active_id


def get_service_health() -> dict[str, Any]:
    """
    Estado de salud de la infraestructura de MediaDEV:
    - Red WireGuard VPN con Honduras
    - Gateways hondureños (score, estado, métricas)
    - Conectividad a PostgreSQL
    - Proxy Privoxy
    """
    wg  = _wireguard_status()
    db  = db_ping()
    prx = {"status": _run(["systemctl", "is-active", "privoxy"]) or "unknown"}

    # Intentar DB primero; si está vacía usar stations.json
    gateways, active_id = _gateways_from_db()
    data_source = "database"
    if not gateways:
        gateways, active_id = _gateways_from_stations()
        data_source = "stations.json"

    # Sobreescribir active_id con gateway.conf si disponible
    active_vpn = _active_gateway_from_conf()
    if active_vpn:
        for gw in gateways:
            if gw.get("vpn_ip") == active_vpn:
                active_id = gw["id"]
                break

    return {
        "wireguard":       wg,
        "database":        db,
        "privoxy":         prx,
        "gateways":        gateways,
        "active_gateway":  active_id,
        "gateway_source":  data_source,
    }


def _mask_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return "***"
