#!/usr/bin/env python3
"""
sync_streams.py — Mantiene en sincronía las 3 fuentes de verdad de streams activos:
  1. capture_config + media_sources  (config de grabación — fuente primaria)
  2. stream_catalog                  (catálogo del producto — debe reflejar lo activo)
  3. stations.json                   (fallback del daemon — se regenera desde DB)

Uso:
  python3 sync_streams.py            # modo dry-run (solo muestra diferencias)
  python3 sync_streams.py --apply    # aplica cambios

Requiere: /etc/mediadev-db.env con PG_HOST, PG_PORT, PG_USER, PG_PASS, PG_DB
"""

import os, sys, json, argparse
from pathlib import Path

STATIONS_JSON = Path(os.environ.get("STATIONS_JSON", "/opt/media-ai/config/stations.json"))
DB_ENV        = Path("/etc/mediadev-db.env")

# ── DB ────────────────────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def pg_connect():
    import psycopg2
    e = load_env(DB_ENV)
    return psycopg2.connect(
        host=e["PG_HOST"], port=int(e.get("PG_PORT", 5432)),
        user=e["PG_USER"], password=e["PG_PASS"], dbname=e["PG_DB"],
        connect_timeout=10,
    )

# ── LEER FUENTE PRIMARIA (capture_config + media_sources) ────────────────────
def read_capture(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ms.slug, ms.name, ms.media_type, ms.stream_catalog_id,
                   cc.stream_url, cc.route, cc.is_enabled
            FROM capture_config cc
            JOIN media_sources ms ON ms.id = cc.media_source_id
            WHERE ms.lifecycle_status = 'active'
              AND ms.slug IS NOT NULL
            ORDER BY ms.media_type, ms.slug
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

# ── SYNC → stream_catalog ─────────────────────────────────────────────────────
def sync_catalog(conn, captures: list[dict], apply: bool) -> list[str]:
    changes = []
    with conn.cursor() as cur:
        for c in captures:
            slug = c["slug"]
            cur.execute(
                "SELECT id, stream_url, status FROM stream_catalog WHERE id = %s",
                (slug,)
            )
            row = cur.fetchone()
            if row is None:
                changes.append(f"  [FALTA EN CATALOG] {slug} — no existe en stream_catalog")
                continue
            sc_id, sc_url, sc_status = row
            needs = []
            if sc_url != c["stream_url"]:
                needs.append(f"stream_url: {sc_url!r} → {c['stream_url']!r}")
            if sc_status != "active":
                needs.append(f"status: {sc_status!r} → 'active'")
            if needs:
                changes.append(f"  [{slug}] " + " | ".join(needs))
                if apply:
                    cur.execute(
                        "UPDATE stream_catalog SET stream_url=%s, status='active', updated_at=now() WHERE id=%s",
                        (c["stream_url"], slug),
                    )
    if apply:
        conn.commit()
    return changes

# ── SYNC → media_sources.stream_catalog_id ────────────────────────────────────
def sync_links(conn, captures: list[dict], apply: bool) -> list[str]:
    changes = []
    with conn.cursor() as cur:
        for c in captures:
            if c["stream_catalog_id"] is None:
                cur.execute("SELECT 1 FROM stream_catalog WHERE id=%s", (c["slug"],))
                if cur.fetchone():
                    changes.append(f"  [{c['slug']}] stream_catalog_id NULL → '{c['slug']}'")
                    if apply:
                        cur.execute(
                            """UPDATE media_sources SET stream_catalog_id=%s, updated_at=now()
                               WHERE slug=%s""",
                            (c["slug"], c["slug"]),
                        )
                else:
                    changes.append(f"  [{c['slug']}] NO LINK y no existe en stream_catalog — agregar manualmente")
    if apply:
        conn.commit()
    return changes

# ── SYNC → stations.json ──────────────────────────────────────────────────────
def sync_stations_json(captures: list[dict], apply: bool) -> list[str]:
    changes = []
    try:
        data = json.loads(STATIONS_JSON.read_text())
    except Exception as e:
        return [f"  ERROR leyendo {STATIONS_JSON}: {e}"]

    current_ids = {s["id"] for s in data.get("stations", [])}
    active_ids  = {c["slug"] for c in captures}

    # IDs que faltan o sobran en stations
    missing = active_ids - current_ids
    extra   = current_ids - active_ids

    if missing:
        changes.append(f"  [stations.json] agregar: {sorted(missing)}")
    if extra:
        changes.append(f"  [stations.json] remover: {sorted(extra)}")

    # Verificar URLs en stations (solo las que tienen url explícita)
    url_map = {s["id"]: s.get("url") or (s.get("urls") or [None])[0]
               for s in data.get("stations", [])}
    capture_url = {c["slug"]: c["stream_url"] for c in captures}
    for sid, url in url_map.items():
        if sid in capture_url and url and url != capture_url[sid]:
            changes.append(f"  [stations.json] {sid} url mismatch: {url!r} → {capture_url[sid]!r}")

    if not changes:
        return changes

    if apply:
        # Reconstruir stations desde captures, preservar gateways
        new_stations = []
        for c in captures:
            entry = {"id": c["slug"], "type": c["media_type"],
                     "url": c["stream_url"], "route": c["route"], "enabled": True}
            new_stations.append(entry)
        data["stations"] = new_stations
        STATIONS_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    return changes

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (default: dry-run)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n=== sync_streams.py [{mode}] ===\n")

    conn = pg_connect()
    captures = read_capture(conn)
    print(f"Streams activos en capture_config: {len(captures)}")

    all_changes = []

    print("\n── stream_catalog sync ──────────────────")
    ch = sync_catalog(conn, captures, args.apply)
    if ch:
        for c in ch: print(c)
        all_changes += ch
    else:
        print("  OK — sin diferencias")

    print("\n── media_sources links ──────────────────")
    ch = sync_links(conn, captures, args.apply)
    if ch:
        for c in ch: print(c)
        all_changes += ch
    else:
        print("  OK — todos linkeados")

    print("\n── stations.json sync ───────────────────")
    ch = sync_stations_json(captures, args.apply)
    if ch:
        for c in ch: print(c)
        all_changes += ch
    else:
        print("  OK — sin diferencias")

    conn.close()

    print(f"\n{'✓ Cambios aplicados.' if args.apply and all_changes else '→ Sin cambios.' if not all_changes else '→ Ejecutar con --apply para aplicar.'}")
    print(f"Total diferencias encontradas: {len(all_changes)}\n")

if __name__ == "__main__":
    main()
