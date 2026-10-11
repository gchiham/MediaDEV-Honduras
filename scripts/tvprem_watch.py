#!/usr/bin/env python3
"""tvprem_watch.py — chequeo diario de las cuentas IPTV (Xtream), de los IDs que usamos y guia HN.

Corre desde cron (03:00 hora Honduras). Solo CONSULTA player_api.php (no abre conexiones de
stream, no gasta cupo) y NOTIFICA por Telegram. NUNCA modifica capture_config: un cambio de ID
puede ser legitimo o un error, y decidirlo es del operador.

Cuentas: /etc/mediadev-iptv-accounts.json (600, fuera de git)
  [{"id": "tvprem1", "base": "http://tvprem.pro:8080", "user": "..", "pass": ".."}, ...]

Que "usamos": cada /live/<user>/<pass>/<id> de capture_config.stream_url / fallback_url de los
canales habilitados cuyo <user> sea una de las cuentas (se lee de la DB en cada corrida).

Alertas (silencio si todo esta sano):
  - cuenta no Active / auth != 1 / API inalcanzable
  - suscripcion vence en <= WARN_DAYS dias (se repite a diario)
  - max_connections cambio
  - un ID en uso ya no existe en el catalogo (sugiere IDs con el mismo nombre)
  - un ID en uso cambio de nombre (posible reasignacion a otro canal)
Freno anti falso positivo: si el catalogo viene vacio o recortado (< MIN_KEEP_RATIO del previo)
NO se evaluan los IDs de esa cuenta ni se pisa su guia; solo se avisa que el panel respondio mal.

Salida (config/):
  tvprem_watch.json       estado: cuentas, IDs en uso con nombre esperado, guia HN por cuenta
  IPTV_GUIDE.md           guia humana: estacion -> IDs por cuenta + listado completo HN
  iptv_guide_changes.log  historial de altas/bajas/renombres de canales HN entre corridas
Uso: tvprem_watch.py [--dry-run] [--snapshot RUTA]   (--dry-run imprime y no escribe nada)
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

ACCOUNTS_FILE = os.environ.get("IPTV_ACCOUNTS", "/etc/mediadev-iptv-accounts.json")
DB_ENV = "/etc/mediadev-db.env"
TG_ENV = os.environ.get("TG_ENV_FILE", "/opt/destroyer/.env")
SNAP_DEFAULT = "/opt/media-ai/config/tvprem_watch.json"
WARN_DAYS = 7
MIN_KEEP_RATIO = 0.5
URL_RE = re.compile(r"/live/([^/]+)/([^/]+)/(\d+)\.(?:ts|m3u8)")
HN_CAT = re.compile(r"hondur", re.I)
HN_NAME = re.compile(r"^\s*(hn|hon|hond)\b", re.I)

# estacion de capture_config -> regex sobre el nombre del canal (ya filtrado a Honduras)
STATIONS = {
    "canal_5": r"\bcanal ?5\b", "canal_6": r"\bcanal ?6\b", "canal_10": r"\bcanal ?10\b",
    "canal_11": r"\bcanal ?11\b", "hch_tv": r"\bhch\b", "tsi": r"\btsi\b",
    "teleceiba": r"\bteleceiba\b", "telecadena": r"\btelecadena\b", "tnh": r"\btnh\b",
}


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


def low(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def used_ids(accounts):
    """{acc_id: {id: [(slug, rol), ...]}} desde capture_config (solo habilitados)."""
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
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    by_user = {a["user"]: a["id"] for a in accounts}
    acc = {}
    for slug, url, fb in rows:
        for rol, u in (("primaria", url), ("fallback", fb)):
            m = URL_RE.search(u or "")
            if m and m.group(1) in by_user:
                acc.setdefault(by_user[m.group(1)], {}).setdefault(m.group(3), []).append((slug, rol))
    return acc


def api(acc, action=None):
    q = f"{acc['base']}/player_api.php?username={acc['user']}&password={acc['pass']}"
    if action:
        q += f"&action={action}"
    last = None
    for _ in range(3):
        try:
            r = requests.get(q, timeout=90)
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


def migrate(snap, accounts):
    """Snapshot v1 (una cuenta: {"ids":{..}, "account":{user:{..}}}) -> v2 por cuenta."""
    if "accounts" in snap or not snap:
        return snap
    by_user = {a["user"]: a["id"] for a in accounts}
    out = {"accounts": {}}
    for user, info in snap.get("account", {}).items():
        aid = by_user.get(user)
        if aid:
            out["accounts"][aid] = {"account": info, "ids": snap.get("ids", {}), "guide": {}}
    return out


def write_md(path, stamp, state, accounts):
    L = [f"# Guia IPTV — canales de Honduras\n\nActualizada: {stamp} (`tvprem_watch.py`, cron 03:00 HN)\n",
         "## Cuentas\n\n| id | host | usuario | estado | max_con | vence (UTC) | canales HN |\n|---|---|---|---|---|---|---|"]
    for a in accounts:
        s = state["accounts"].get(a["id"], {})
        ac, exp = s.get("account", {}), ""
        if ac.get("exp_date"):
            exp = datetime.fromtimestamp(int(ac["exp_date"]), timezone.utc).strftime("%Y-%m-%d")
        L.append(f"| {a['id']} | {a['base']} | {a['user']} | {ac.get('status', '?')} | "
                 f"{ac.get('max_connections', '?')} | {exp} | {len(s.get('guide', {}))} |")
    L.append("\n## Estaciones de captura -> IDs por cuenta\n")
    L.append("| estacion | " + " | ".join(a["id"] for a in accounts) + " |\n|---|" + "---|" * len(accounts))
    for slug, rx in STATIONS.items():
        pat, cells = re.compile(rx), []
        for a in accounts:
            g = state["accounts"].get(a["id"], {}).get("guide", {})
            ids = sorted(i for i, c in g.items() if pat.search(low(c["name"])))
            cells.append("<br>".join(f"`{i}` {g[i]['name'].replace('|', '/')}" for i in ids) or "—")
        L.append(f"| {slug} | " + " | ".join(cells) + " |")
    L.append("\nLos IDs son propios de cada cuenta (no se reutilizan entre cuentas). Variantes "
             "OP1/OP2/HD pueden ser fuentes distintas: verificar la senal antes de usarla de respaldo.\n")
    for a in accounts:
        g = state["accounts"].get(a["id"], {}).get("guide", {})
        L.append(f"## Listado completo — {a['id']} ({len(g)})\n\n| id | nombre | categoria |\n|---|---|---|")
        for sid, c in sorted(g.items(), key=lambda kv: low(kv[1]["name"])):
            L.append(f"| {sid} | {c['name'].replace('|', '/')} | {c['cat']} |")
        L.append("")
    open(path + ".tmp", "w", encoding="utf-8").write("\n".join(L))
    os.replace(path + ".tmp", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--snapshot", default=SNAP_DEFAULT)
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    accounts = json.load(open(ACCOUNTS_FILE, encoding="utf-8"))
    try:
        snap = json.load(open(a.snapshot, encoding="utf-8"))
    except (OSError, ValueError):
        snap = {}
    snap = migrate(snap, accounts)
    prev = snap.get("accounts", {})
    state = {"updated": stamp, "accounts": {}}
    alerts, changes = [], []

    try:
        used = used_ids(accounts)
    except Exception as e:  # noqa: BLE001
        tg_send(f"⚠️ tvprem_watch: no pude leer capture_config ({e})", a.dry_run)
        print(f"{stamp} ERROR db: {e}")
        return 2

    for acc in accounts:
        aid, user = acc["id"], acc["user"]
        old = prev.get(aid, {})
        ids_snap, old_guide = old.get("ids", {}), old.get("guide", {})
        state["accounts"][aid] = dict(old)  # si algo falla, se conserva lo anterior
        try:
            ui = api(acc).get("user_info", {})
            cats = {str(c["category_id"]): (c.get("category_name") or "").strip()
                    for c in api(acc, "get_live_categories")}
            catalog = api(acc, "get_live_streams")
        except Exception as e:  # noqa: BLE001
            alerts.append(f"No pude consultar el panel {aid} ({user}) tras 3 intentos: {e}")
            continue

        # --- cuenta ---
        if ui.get("auth") != 1 or ui.get("status") != "Active":
            alerts.append(f"Cuenta {aid} ({user}): auth={ui.get('auth')} status={ui.get('status')} "
                          "(esperado auth=1, Active). La captura de esa cuenta se va a caer.")
        try:
            exp = datetime.fromtimestamp(int(ui["exp_date"]), timezone.utc)
            days = (exp - now).total_seconds() / 86400
            if days <= WARN_DAYS:
                alerts.append(f"La suscripción {aid} ({user}) vence en {days:.1f} días "
                              f"({exp:%Y-%m-%d %H:%M} UTC). Renovar.")
        except (KeyError, ValueError, TypeError):
            alerts.append(f"Cuenta {aid}: exp_date ilegible ({ui.get('exp_date')!r})")
        mc = str(ui.get("max_connections"))
        prev_mc = old.get("account", {}).get("max_connections")
        if prev_mc and prev_mc != mc:
            alerts.append(f"Cuenta {aid}: max_connections cambió {prev_mc} -> {mc}")
        state["accounts"][aid]["account"] = {"max_connections": mc, "exp_date": ui.get("exp_date"),
                                             "status": ui.get("status")}

        # --- freno: catalogo vacio o recortado = panel respondiendo mal, no conclusiones ---
        by_catalog = {str(s["stream_id"]): s.get("name", "") for s in catalog}
        guide = {}
        for s in catalog:
            cat, name = cats.get(str(s.get("category_id")), ""), (s.get("name") or "").strip()
            if HN_CAT.search(cat) or HN_NAME.search(name):
                guide[str(s["stream_id"])] = {"name": name, "cat": cat}
        if not guide or (old_guide and len(guide) < MIN_KEEP_RATIO * len(old_guide)):
            alerts.append(f"Panel {aid}: catálogo HN vacío/recortado ({len(guide)} vs {len(old_guide)} "
                          f"previos, {len(by_catalog)} canales totales). No evalúo IDs hoy; se conserva la guía.")
            continue

        # --- guia HN ---
        if old_guide:
            for sid, c in guide.items():
                if sid not in old_guide:
                    changes.append(f"{aid} ALTA   {sid} {c['name']}")
                elif norm(old_guide[sid]["name"]) != norm(c["name"]):
                    changes.append(f"{aid} RENOMB {sid} '{old_guide[sid]['name']}' -> '{c['name']}'")
            changes += [f"{aid} BAJA   {sid} {c['name']}" for sid, c in old_guide.items() if sid not in guide]
        state["accounts"][aid]["guide"] = guide

        # --- IDs en uso ---
        new_ids = {}
        for sid, uses in sorted(used.get(aid, {}).items()):
            who = ", ".join(f"{slug}/{rol}" for slug, rol in uses)
            old_name = ids_snap.get(sid, {}).get("name")
            cur = by_catalog.get(sid)
            if cur is None:
                cands = [f"{i} ({n})" for i, n in by_catalog.items()
                         if old_name and norm(n) == norm(old_name)][:5]
                alerts.append(f"ID {sid} ({old_name or '?'}) de {aid}, usado por {who}, YA NO EXISTE en el panel."
                              + (f" Candidatos con el mismo nombre: {', '.join(cands)}" if cands else ""))
                if old_name:  # conservar el nombre esperado para seguir alertando
                    new_ids[sid] = ids_snap[sid]
            else:
                if old_name and norm(old_name) != norm(cur):
                    alerts.append(f"ID {sid} de {aid}, usado por {who}, cambió de nombre: "
                                  f"'{old_name}' -> '{cur}'. Posible reasignación a otro canal: verificar.")
                new_ids[sid] = {"name": cur, "uses": [f"{s}/{r}" for s, r in uses],
                                "seen": now.strftime("%Y-%m-%d")}
        state["accounts"][aid]["ids"] = new_ids

    if not a.dry_run:
        out_dir = os.path.dirname(a.snapshot)
        os.makedirs(out_dir, exist_ok=True)
        json.dump(state, open(a.snapshot + ".tmp", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        os.replace(a.snapshot + ".tmp", a.snapshot)
        write_md(os.path.join(out_dir, "IPTV_GUIDE.md"), stamp, state, accounts)
        if changes:
            with open(os.path.join(out_dir, "iptv_guide_changes.log"), "a", encoding="utf-8") as f:
                f.write(f"## {stamp}\n" + "\n".join(changes) + "\n")

    if alerts:
        tg_send("🔔 tvprem_watch\n\n" + "\n\n".join(f"• {x}" for x in alerts), a.dry_run)
    n_ids = sum(len(s.get("ids", {})) for s in state["accounts"].values())
    hn = ",".join(f"{k}:{len(v.get('guide', {}))}" for k, v in state["accounts"].items())
    print(f"{stamp} ids_vigilados={n_ids} guia_HN={hn} cambios={len(changes)} alertas={len(alerts)}"
          + ("".join(f"\n  - {x}" for x in alerts) if alerts else " OK")
          + "".join(f"\n  ~ {x}" for x in changes[:40]))
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
