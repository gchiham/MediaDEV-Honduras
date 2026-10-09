#!/usr/bin/env python3
"""tvprem_watch.py — chequeo diario de la cuenta tvprem y de los IDs que usamos.

Corre desde cron (21:00 hora Honduras). Solo CONSULTA player_api.php (no abre conexiones de
stream, no gasta el cupo de 2) y NOTIFICA por Telegram. NUNCA modifica capture_config: un
cambio de ID puede ser legitimo o un error, y decidirlo es del operador.

Que "usamos": cada ID de tvprem en capture_config.stream_url / fallback_url de los canales
habilitados (se lee de la DB en cada corrida: si se suma un canal nuevo queda cubierto solo).

Alertas (silencio si todo esta sano):
  - cuenta no Active / auth != 1 / API inalcanzable
  - suscripcion vence en <= WARN_DAYS dias (se repite a diario)
  - max_connections cambio
  - un ID en uso ya no existe en el catalogo (sugiere IDs con el mismo nombre)
  - un ID en uso cambio de nombre (posible reasignacion a otro canal)

Estado: /opt/media-ai/config/tvprem_watch.json (snapshot id -> nombre; se refresca en cada corrida).
Uso: tvprem_watch.py [--dry-run] [--snapshot RUTA]   (--dry-run imprime en vez de enviar a Telegram)
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import psycopg2
import requests

DB_ENV = "/etc/mediadev-db.env"
TG_ENV = os.environ.get("TG_ENV_FILE", "/opt/destroyer/.env")
SNAP_DEFAULT = "/opt/media-ai/config/tvprem_watch.json"
WARN_DAYS = 7
HOST = "http://tvprem.pro:8080"
URL_RE = re.compile(r"tvprem\.pro(?::\d+)?/live/([^/]+)/([^/]+)/(\d+)\.(?:ts|m3u8)")


def load_env(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.replace("export ", "").strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def used_ids():
    """{(user, pass): {id: [(slug, rol), ...]}} desde capture_config (solo habilitados)."""
    env = load_env(DB_ENV)
    conn = psycopg2.connect(
        host=env["PG_HOST"], port=env.get("PG_PORT", "5432"), dbname=env["PG_DB"],
        user=env["PG_USER"], password=env["PG_PASS"], connect_timeout=10, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ms.slug, cc.stream_url, cc.fallback_url
                FROM capture_config cc JOIN media_sources ms ON ms.id = cc.media_source_id
                WHERE cc.is_enabled = true
                  AND (cc.stream_url ILIKE '%tvprem.pro%' OR cc.fallback_url ILIKE '%tvprem.pro%')
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    acc = {}
    for slug, url, fb in rows:
        for rol, u in (("primaria", url), ("fallback", fb)):
            m = URL_RE.search(u or "")
            if m:
                user, pw, sid = m.group(1), m.group(2), m.group(3)
                acc.setdefault((user, pw), {}).setdefault(sid, []).append((slug, rol))
    return acc


def api(user, pw, action=None):
    q = f"{HOST}/player_api.php?username={user}&password={pw}"
    if action:
        q += f"&action={action}"
    last = None
    for _ in range(3):
        try:
            r = requests.get(q, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(20)
    raise RuntimeError(f"{last}")


def tg_send(text, dry):
    if dry:
        print("[TG dry-run]\n" + text + "\n")
        return
    env = load_env(TG_ENV)
    tok = os.environ.get("TG_TOKEN") or env.get("TG_TOKEN")
    chat = os.environ.get("TG_CHAT") or env.get("TG_CHAT")
    if not (tok and chat):
        print("sin credenciales Telegram", file=sys.stderr)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"tg_send error: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--snapshot", default=SNAP_DEFAULT)
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    try:
        snap = json.load(open(a.snapshot, encoding="utf-8"))
    except (OSError, ValueError):
        snap = {}
    ids_snap = snap.get("ids", {})
    alerts = []
    new_snap = {"updated": stamp, "ids": {}, "account": snap.get("account", {})}

    try:
        accounts = used_ids()
    except Exception as e:  # noqa: BLE001
        tg_send(f"⚠️ tvprem_watch: no pude leer capture_config ({e})", a.dry_run)
        print(f"{stamp} ERROR db: {e}")
        return 2

    if not accounts:
        print(f"{stamp} sin canales tvprem habilitados en capture_config — nada que vigilar")
        return 0

    for (user, pw), by_id in accounts.items():
        try:
            info = api(user, pw)
            ui = info.get("user_info", {})
            catalog = api(user, pw, "get_live_streams")
        except Exception as e:  # noqa: BLE001
            alerts.append(f"No pude consultar el panel tvprem ({user}) tras 3 intentos: {e}")
            continue

        # --- cuenta ---
        if ui.get("auth") != 1 or ui.get("status") != "Active":
            alerts.append(f"Cuenta {user}: auth={ui.get('auth')} status={ui.get('status')} "
                          "(esperado auth=1, Active). La captura de tvprem se va a caer.")
        try:
            exp = datetime.fromtimestamp(int(ui["exp_date"]), timezone.utc)
            days = (exp - now).total_seconds() / 86400
            if days <= WARN_DAYS:
                alerts.append(f"La suscripción tvprem ({user}) vence en {days:.1f} días "
                              f"({exp:%Y-%m-%d %H:%M} UTC). Renovar o canal_5 deja de grabar.")
        except (KeyError, ValueError, TypeError):
            alerts.append(f"Cuenta {user}: exp_date ilegible ({ui.get('exp_date')!r})")
        mc = str(ui.get("max_connections"))
        prev_mc = new_snap["account"].get(user, {}).get("max_connections")
        if prev_mc and prev_mc != mc:
            alerts.append(f"Cuenta {user}: max_connections cambió {prev_mc} -> {mc}")
        new_snap["account"][user] = {"max_connections": mc, "exp_date": ui.get("exp_date"),
                                     "status": ui.get("status")}

        # --- IDs en uso ---
        by_catalog = {str(s["stream_id"]): s.get("name", "") for s in catalog}
        for sid, uses in sorted(by_id.items()):
            who = ", ".join(f"{slug}/{rol}" for slug, rol in uses)
            old = ids_snap.get(sid, {}).get("name")
            cur = by_catalog.get(sid)
            if cur is None:
                cands = [f"{i} ({n})" for i, n in by_catalog.items()
                         if old and norm(n) == norm(old)][:5]
                alerts.append(f"ID {sid} ({old or '?'}) usado por {who} YA NO EXISTE en tvprem."
                              + (f" Candidatos con el mismo nombre: {', '.join(cands)}" if cands else ""))
                if old:  # conservar el nombre esperado para seguir alertando
                    new_snap["ids"][sid] = ids_snap[sid]
            else:
                if old and norm(old) != norm(cur):
                    alerts.append(f"ID {sid} usado por {who} cambió de nombre: "
                                  f"'{old}' -> '{cur}'. Posible reasignación a otro canal: verificar.")
                new_snap["ids"][sid] = {"name": cur, "uses": [f"{s}/{r}" for s, r in uses],
                                        "seen": now.strftime("%Y-%m-%d")}

    os.makedirs(os.path.dirname(a.snapshot), exist_ok=True)
    json.dump(new_snap, open(a.snapshot, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    n_ids = len(new_snap["ids"])
    if alerts:
        tg_send("🔔 tvprem_watch\n\n" + "\n\n".join(f"• {x}" for x in alerts), a.dry_run)
    print(f"{stamp} ids_vigilados={n_ids} alertas={len(alerts)}"
          + ("".join(f"\n  - {x}" for x in alerts) if alerts else " OK"))
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
