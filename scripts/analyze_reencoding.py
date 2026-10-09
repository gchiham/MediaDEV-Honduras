#!/usr/bin/env python3
"""
analyze_reencoding.py - Detecta re-encodeo de audio en streams del catálogo

Técnica: compara energía del espectro completo vs energía sobre 14kHz.
Audio re-encodeado desde fuente lossy tiene corte brusco en altas frecuencias
(MP3 128kbps corta a ~15.5kHz, 192kbps a ~16kHz, AAC a ~16-18kHz).
Diferencia > 25dB entre full y highpass → sospechoso o re-encodeado.

Uso:
  python3 analyze_reencoding.py              # solo activos (rápido)
  python3 analyze_reencoding.py --all        # catálogo completo
  python3 analyze_reencoding.py --workers 4  # ajustar concurrencia
"""

import os, sys, json, re, subprocess, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DB_ENV       = Path("/etc/mediadev-db.env")
GW_CONF      = Path("/etc/mediadev/gateway.conf")
SAMPLE_SECS  = 8
WORKERS      = 6
CUTOFF_HZ    = 14000   # Hz desde donde medimos energía alta
REENC_DB     = 30      # diferencia ≥ 30dB → RE-ENCODEADO
SUSPECT_DB   = 20      # diferencia 20-30dB → SOSPECHOSO


# ── DB ────────────────────────────────────────────────────────────────────────
def load_env(path):
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

def get_streams(conn, only_active=True):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sc.id, sc.name, sc.type, sc.status, sc.stream_url,
                   COALESCE(cc.route, 'direct') AS route
            FROM stream_catalog sc
            LEFT JOIN media_sources ms ON ms.stream_catalog_id = sc.id
            LEFT JOIN capture_config cc ON cc.media_source_id = ms.id
            WHERE sc.stream_url IS NOT NULL AND sc.stream_url != ''
              AND (%(only_active)s = false OR sc.status = 'active')
            ORDER BY sc.status DESC, sc.type, sc.id
        """, {"only_active": only_active})
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── GATEWAY ───────────────────────────────────────────────────────────────────
def get_privoxy_port():
    if not GW_CONF.exists():
        return None
    for line in GW_CONF.read_text().splitlines():
        if "GW_PRIVOXY_PORT" in line and "=" in line:
            v = line.split("=", 1)[-1].strip()
            return v if v.isdigit() else None
    return None


# ── ANÁLISIS ──────────────────────────────────────────────────────────────────
def parse_mean_volumes(stderr: str) -> list[float]:
    return [float(m) for m in re.findall(r'mean_volume:\s*([-\d.]+)\s*dB', stderr)]

def get_encoder_tag(url: str, proxy_args: list) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "stream_tags=encoder:format_tags=encoder"]
            + proxy_args + [url],
            capture_output=True, text=True, timeout=10
        )
        d = json.loads(r.stdout or "{}")
        tags = {}
        for s in d.get("streams", []):
            tags.update(s.get("tags", {}))
        tags.update(d.get("format", {}).get("tags", {}))
        return tags.get("encoder", "")
    except Exception:
        return ""

def analyze_stream(stream: dict, privoxy_port: str | None) -> dict:
    url   = stream["stream_url"]
    route = stream.get("route", "direct")
    slug  = stream["id"]

    proxy = ["-http_proxy", f"http://127.0.0.1:{privoxy_port}"] \
            if route == "socks5" and privoxy_port else []

    encoder = get_encoder_tag(url, proxy)
    raw     = f"/tmp/reenc_{slug[:20]}.raw"

    try:
        # Paso 1: capturar SAMPLE_SECS de audio como PCM raw (evita re-abrir el stream)
        cap = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
            + proxy
            + ["-i", url, "-t", str(SAMPLE_SECS), "-vn",
               "-ar", "44100", "-ac", "1", "-f", "s16le", raw],
            capture_output=True, timeout=SAMPLE_SECS + 20
        )
        if not Path(raw).exists() or Path(raw).stat().st_size < 4096:
            return _err(stream, "SIN_AUDIO", "captura vacía o stream no responde")

        # Paso 2a: nivel full-spectrum
        r_full = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "s16le", "-ar", "44100", "-ac", "1",
             "-i", raw, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30
        )
        # Paso 2b: nivel sobre CUTOFF_HZ
        r_high = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "s16le", "-ar", "44100", "-ac", "1",
             "-i", raw, "-af", f"highpass=f={CUTOFF_HZ},volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30
        )

        vols_full = parse_mean_volumes(r_full.stderr)
        vols_high = parse_mean_volumes(r_high.stderr)

        if not vols_full or not vols_high:
            return _err(stream, "SIN_AUDIO", "volumedetect sin output")

        full_db = vols_full[0]
        high_db = vols_high[0]
        diff    = full_db - high_db  # positivo = menos energía en altas frecuencias

        if high_db < -70 or diff >= REENC_DB:
            verdict = "RE-ENCODEADO"
        elif diff >= SUSPECT_DB:
            verdict = "SOSPECHOSO"
        else:
            verdict = "ORIGINAL"

        return {
            "id": slug, "name": stream["name"],
            "type": stream["type"], "status": stream["status"],
            "route": route, "url": url,
            "full_db": round(full_db, 1),
            "high_db": round(high_db, 1),
            "diff_db": round(diff, 1),
            "verdict": verdict,
            "encoder": encoder,
        }

    except subprocess.TimeoutExpired:
        return _err(stream, "TIMEOUT", f">{SAMPLE_SECS + 20}s")
    except Exception as e:
        return _err(stream, "ERROR", str(e)[:120])
    finally:
        Path(raw).unlink(missing_ok=True)

def _err(stream, verdict, detail):
    return {"id": stream["id"], "name": stream["name"],
            "type": stream["type"], "status": stream["status"],
            "route": stream.get("route", "direct"), "url": stream["stream_url"],
            "verdict": verdict, "detail": detail,
            "full_db": None, "high_db": None, "diff_db": None, "encoder": ""}


# ── MAIN ──────────────────────────────────────────────────────────────────────
ICONS = {"RE-ENCODEADO": "✗", "SOSPECHOSO": "⚠", "ORIGINAL": "✓", "SIN_AUDIO": "—"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",     action="store_true", help="Analizar catálogo completo (no solo activos)")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--output",  default="/tmp/reenc_analysis.json")
    args = parser.parse_args()

    privoxy_port = get_privoxy_port()
    conn = pg_connect()
    streams = get_streams(conn, only_active=not args.all)
    conn.close()

    scope = "catálogo completo" if args.all else "streams activos"
    print(f"\n{'='*65}")
    print(f"  Análisis de re-encodeo — {scope}")
    print(f"  {len(streams)} streams | {args.workers} workers | {SAMPLE_SECS}s muestra | corte {CUTOFF_HZ}Hz")
    print(f"  Thresholds: ORIGINAL <{SUSPECT_DB}dB | SOSPECHOSO {SUSPECT_DB}-{REENC_DB}dB | RE-ENCODEADO ≥{REENC_DB}dB")
    print(f"{'='*65}\n")

    results = []
    total   = len(streams)
    done    = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(analyze_stream, s, privoxy_port): s for s in streams}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            done += 1
            v    = r.get("verdict", "?")
            icon = ICONS.get(v, "?")
            full = f"{r['full_db']:>6}" if r.get("full_db") is not None else "   n/a"
            high = f"{r['high_db']:>6}" if r.get("high_db") is not None else "   n/a"
            diff = f"Δ{r['diff_db']:>4}dB" if r.get("diff_db") is not None else ""
            print(f"  [{done:3}/{total}] {icon} {v:<14} {full}/{high}dB {diff:<9}  {r['name'][:38]}")

    # ── Resumen por verdict ────────────────────────────────────────────────────
    by_verdict: dict[str, list] = {}
    for r in results:
        by_verdict.setdefault(r.get("verdict", "ERROR"), []).append(r)

    print(f"\n{'='*65}")
    print("RESUMEN:")
    for v in ["ORIGINAL", "SOSPECHOSO", "RE-ENCODEADO", "SIN_AUDIO", "TIMEOUT", "ERROR"]:
        items = by_verdict.get(v, [])
        if items:
            print(f"  {ICONS.get(v,'?')} {v:<15}  {len(items):>4}")

    for verdict in ["RE-ENCODEADO", "SOSPECHOSO"]:
        items = by_verdict.get(verdict, [])
        if items:
            print(f"\n{verdict} ({len(items)}):")
            for r in sorted(items, key=lambda x: x.get("diff_db") or 0, reverse=True):
                enc = f"  enc={r['encoder']}" if r.get("encoder") else ""
                print(f"  {r['id']:<25} Δ{r.get('diff_db','?'):>4}dB  {r['name'][:35]}{enc}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResultados completos: {args.output}\n")

if __name__ == "__main__":
    main()
