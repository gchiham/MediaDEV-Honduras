#!/usr/bin/env python3
"""MediaDEV Dashboard v4 — PostgreSQL (media-db), KPIs completos"""
import os, sys, json, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, abort
import psycopg2, psycopg2.extras
TGU = timezone(timedelta(hours=-6))  # America/Tegucigalpa GMT-6

app = Flask(__name__, template_folder="templates")

STREAMS_ROOT = Path("/var/www/streams")
STREAMS = [
    "fm_941","hch_tv","radio_america","radio_choluteca","radio_el_patio",
    "radio_globo","radio_satelite","suave_fm","teleceiba",
    "xy_hrn","xy_sps","xy_tgu",
]

PG_HOST = os.environ.get("PG_HOST")
PG_PORT = int(os.environ.get("PG_PORT", "25060"))
PG_DB   = os.environ.get("PG_DB")
PG_USER = os.environ.get("PG_USER")
PG_PASS = os.environ.get("PG_PASS")

def db():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
        connect_timeout=5, sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

def qall(con, sql, params=()):
    with con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def qone(con, sql, params=()):
    with con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def ts_now(): return int(__import__("time").time())

def uptime_pct(con, sid, cutoff):
    r = qone(con,
        "SELECT COUNT(*) tot, SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) ok"
        " FROM mediadev_metrics WHERE stream_id=%s AND ts>=%s", (sid, cutoff))
    tot = r["tot"] or 0
    return round((r["ok"] or 0)/tot*100, 1) if tot else None   # None = sin datos aún

def mb_sum(con, sid, cutoff):
    r = qone(con, "SELECT SUM(bytes)::bigint s FROM mediadev_metrics WHERE stream_id=%s AND ts>=%s", (sid, cutoff))
    return round((r["s"] or 0)/1048576, 1)

def bitrate_kbs(con, sid, cutoff):
    r = qone(con, "SELECT SUM(bytes)::bigint s FROM mediadev_metrics WHERE stream_id=%s AND ts>=%s", (sid, cutoff))
    v = r["s"] or 0
    secs = ts_now() - cutoff
    return round(v / secs / 1024, 1) if secs > 0 else 0

def downs_count(con, sid, cutoff):
    r = qone(con, "SELECT COUNT(*) c FROM mediadev_events WHERE stream_id=%s AND ts>=%s AND etype='DOWN'", (sid, cutoff))
    return r["c"] or 0

def last_event_str(con, sid, etype):
    r = qone(con, "SELECT ts FROM mediadev_events WHERE stream_id=%s AND etype=%s ORDER BY ts DESC LIMIT 1", (sid, etype))
    if not r or not r["ts"]: return "—"
    return datetime.fromtimestamp(r["ts"], tz=TGU).strftime("%d/%m %H:%M")

def disk_segments(sid):
    """Auditoría: cuenta y suma bytes de los .ts en disco (PG no guarda segmentos)."""
    d = STREAMS_ROOT / sid
    cnt = 0; total = 0
    if d.exists():
        for seg in d.glob("seg_*.ts"):
            try:
                total += seg.stat().st_size; cnt += 1
            except:
                pass
    return cnt, total

def color(status):
    return {"OK":"ok","STALE":"stale","DISABLED":"disabled"}.get(status,"error")

# ─── MAIN DASHBOARD ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    con  = db()
    now  = ts_now()
    h24  = now - 86400

    # ── KPIs globales ──
    statuses  = {r["stream_id"]: r for r in qall(con, "SELECT * FROM mediadev_stream_status")}
    online    = sum(1 for r in statuses.values() if r["status"] == "OK")
    cb_open   = sum(1 for r in statuses.values() if r["cb_state"] == "OPEN")
    incidents = sum(1 for r in statuses.values() if r["status"] not in ("OK","DISABLED"))
    restarts  = sum((r["restart_today"] or 0) for r in statuses.values())

    # ── Métricas 24h agregadas ──
    m24 = {}
    for r in qall(con,
        "SELECT stream_id, COUNT(*) t, SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) ok,"
        " ROUND(SUM(bytes)/1048576.0,1)::float8 mb FROM mediadev_metrics WHERE ts>=%s GROUP BY stream_id",
        (h24,)
    ):
        m24[r["stream_id"]] = (r["t"], r["ok"], r["mb"])

    # ── Bitrate 1h agregado ──
    m1h = {r["stream_id"]: (r["s"] or 0) for r in qall(con,
        "SELECT stream_id, SUM(bytes)::bigint s FROM mediadev_metrics WHERE ts>=%s GROUP BY stream_id",
        (now - 3600,)
    )}

    # ── Caídas 24h + última caída ──
    ev24 = {r["stream_id"]: (r["c"], r["m"]) for r in qall(con,
        "SELECT stream_id, COUNT(*) c, MAX(ts) m FROM mediadev_events"
        " WHERE ts>=%s AND etype='DOWN' GROUP BY stream_id",
        (h24,)
    )}

    # ── Globales calculados ──
    tot_m = sum(v[0] for v in m24.values()) or 1
    ok_m  = sum((v[1] or 0) for v in m24.values())
    uptime_global = round(ok_m / tot_m * 100, 1)
    mb_global     = round(sum(v[2] or 0 for v in m24.values()), 1)
    bitrate_total = round(sum(m1h.values()) / 3600 / 1024, 1)

    # ── Cards de streams ──
    streams = []
    for sid in STREAMS:
        row = statuses.get(sid)
        if not row:
            continue
        r24  = m24.get(sid)
        up24 = round((r24[1] or 0)/r24[0]*100, 1) if r24 and r24[0] else None
        ev   = ev24.get(sid)
        if ev and ev[1]:
            last_down_str = datetime.fromtimestamp(ev[1], tz=TGU).strftime("%d/%m %H:%M")
        else:
            last_down_str = "—"
        streams.append({
            "id":          sid,
            "status":      row["status"],
            "color":       color(row["status"]),
            "cb_state":    row["cb_state"],
            "sup":         row["sup"],
            "segs":        row["segs"],
            "age":         row["age"],
            "uptime_24h":  up24,
            "mb_24h":      (r24[2] or 0) if r24 else 0,
            "bitrate":     round(m1h.get(sid, 0) / 3600 / 1024, 1),
            "downs_24h":   ev[0] if ev else 0,
            "last_down":   last_down_str,
            "last_up":     last_event_str(con, sid, "UP"),
            "restart_today": row["restart_today"],
            "url": f"/streams/{sid}/index.m3u8",
        })

    updated = datetime.now(tz=TGU).strftime("%Y-%m-%d %H:%M GMT-6")
    con.close()
    return render_template("dashboard_main.html",
        streams=streams, online=online, total=len(STREAMS),
        uptime_global=uptime_global, mb_global=mb_global,
        bitrate_total=bitrate_total, incidents=incidents,
        cb_open=cb_open, restarts=restarts, updated=updated)

# ─── STREAM DETAIL ────────────────────────────────────────────────────────────
@app.route("/stream/<sid>")
def stream_detail(sid):
    if sid not in STREAMS:
        abort(404)
    con = db()
    now = ts_now()
    row = qone(con, "SELECT * FROM mediadev_stream_status WHERE stream_id=%s", (sid,))
    if not row:
        abort(404)

    # ── KPIs por período ──
    periods = {}
    for label, hours in [("1h",1),("8h",8),("24h",24),("7d",168)]:
        cutoff = now - hours*3600
        periods[label] = {
            "uptime":   uptime_pct(con, sid, cutoff),
            "mb":       mb_sum(con, sid, cutoff),
            "bitrate":  bitrate_kbs(con, sid, cutoff),
            "downs":    downs_count(con, sid, cutoff),
        }

    # ── Gráfico: uptime % por hora ──
    window = now - 24*3600
    hour_data = {r["h"]: (r["tot"], r["ok"], r["bw"]) for r in qall(con,
        "SELECT (ts/3600)*3600 h, COUNT(*) tot,"
        " SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) ok, SUM(bytes)::bigint bw"
        " FROM mediadev_metrics WHERE stream_id=%s AND ts>=%s"
        " GROUP BY h ORDER BY h",
        (sid, window)
    )}
    chart_labels, chart_up, chart_bw = [], [], []
    for h in range(23, -1, -1):
        bucket = ((now - h*3600) // 3600) * 3600
        label  = datetime.fromtimestamp(bucket + 3600, tz=TGU).strftime("%Hh")
        hr     = hour_data.get(bucket)
        up_pct = round((hr[1] or 0)/hr[0]*100, 1) if hr and hr[0] else 0
        bw_mb  = round((hr[2] or 0)/1048576, 2) if hr else 0
        chart_labels.append(label)
        chart_up.append(up_pct)
        chart_bw.append(bw_mb)

    # ── Eventos recientes ──
    events = [{
        "ts":     datetime.fromtimestamp(e["ts"], tz=TGU).strftime("%d/%m %H:%M:%S"),
        "etype":  e["etype"],
        "detail": e["detail"],
    } for e in qall(con,
        "SELECT ts,etype,detail FROM mediadev_events WHERE stream_id=%s ORDER BY ts DESC LIMIT 25", (sid,)
    )]

    # ── Auditoría en disco ──
    seg_count, seg_bytes = disk_segments(sid)
    rec_dir   = STREAMS_ROOT / sid / "recordings"
    rec_files = sorted(rec_dir.glob("*.mp3")) if rec_dir.exists() else []

    updated = datetime.now(tz=TGU).strftime("%Y-%m-%d %H:%M GMT-6")
    last_down_r = last_event_str(con, sid, "DOWN")
    last_up_r   = last_event_str(con, sid, "UP")
    con.close()

    return render_template("stream_detail.html",
        sid=sid, status=row["status"], color=color(row["status"]),
        sup=row["sup"], segs=row["segs"], age=row["age"],
        cb_state=row["cb_state"], cb_fails=row["cb_fails"],
        restart_today=row["restart_today"],
        last_down=last_down_r,
        last_up  =last_up_r,
        periods=periods, events=events,
        chart_labels=json.dumps(chart_labels),
        chart_up=json.dumps(chart_up),
        chart_bw=json.dumps(chart_bw),
        audit_segs=seg_count,
        audit_mb=round(seg_bytes/1048576, 1),
        audit_hours=round(seg_count*4/3600, 1),
        rec_files=[f.stem for f in reversed(rec_files)],
        url=f"/streams/{sid}/index.m3u8",
        all_streams=STREAMS, updated=updated)

# ─── API JSON (para consumo externo / Claude Design) ───────────────────────────
# Endpoints read-only, sin autenticación (misma política que el resto del panel).
def _ser(v):
    """Serializa tipos de PostgreSQL a JSON-friendly."""
    from decimal import Decimal
    from uuid import UUID
    if isinstance(v, datetime): return v.isoformat()
    if isinstance(v, Decimal):  return float(v)
    if isinstance(v, UUID):     return str(v)
    return v

def rows_json(rows):
    return [{k: _ser(v) for k, v in r.items()} for r in rows]

@app.route("/api/status")
def api_status():
    """Estado operativo de los 12 streams que se graban (salud en vivo)."""
    con = db()
    rows = {r["stream_id"]: {k: _ser(v) for k, v in r.items()}
            for r in qall(con, "SELECT * FROM mediadev_stream_status ORDER BY stream_id")}
    con.close()
    ok = sum(1 for r in rows.values() if r["status"] == "OK")
    return jsonify({"streams": rows, "summary": {"ok": ok, "total": len(STREAMS)},
                    "updated": datetime.now(tz=TGU).isoformat()})

# Alias semántico de /api/status
app.add_url_rule("/api/streams", "api_streams", api_status)

@app.route("/api/stations")
def api_stations():
    """Catálogo completo de estaciones (radio/TV) en media-db.
    Query opcional ?status=active|inactive  y  ?type=radio|tv."""
    from flask import request
    con = db()
    where, params = [], []
    if request.args.get("status"):
        where.append("status = %s"); params.append(request.args["status"])
    if request.args.get("type"):
        where.append("type = %s");   params.append(request.args["type"])
    sql = ("SELECT id, name, type, country_code, city, frequency, coverage,"
           " callsign, logo_url, stream_url, status FROM stream_catalog")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY status, name"
    rows = rows_json(qall(con, sql, tuple(params)))
    con.close()
    return jsonify({"stations": rows, "count": len(rows),
                    "updated": datetime.now(tz=TGU).isoformat()})

@app.route("/api/detections")
def api_detections():
    """Detecciones de anuncios recientes con nombre de anuncio y estación.
    Query opcional ?limit=N (default 50, máx 500)."""
    from flask import request
    try:
        limit = min(int(request.args.get("limit", 50)), 500)
    except ValueError:
        limit = 50
    con = db()
    rows = rows_json(qall(con,
        "SELECT d.id, d.stream_id, a.name AS ad_name, d.air_time, d.ts_label,"
        " d.score, d.confidence_level, d.clip_s3_key, d.detected_at"
        " FROM fingerprint_detections d"
        " LEFT JOIN advertisements a ON a.id = d.ad_id"
        " WHERE d.deleted_at IS NULL"
        " ORDER BY d.air_time DESC LIMIT %s", (limit,)))
    con.close()
    return jsonify({"detections": rows, "count": len(rows),
                    "updated": datetime.now(tz=TGU).isoformat()})

@app.route("/api/summary")
def api_summary():
    """KPIs globales del sistema para tarjetas de resumen."""
    con = db()
    now = ts_now()
    online = qone(con, "SELECT COUNT(*) c FROM mediadev_stream_status WHERE status='OK'")["c"]
    total_live = qone(con, "SELECT COUNT(*) c FROM mediadev_stream_status")["c"]
    cat = qone(con, "SELECT COUNT(*) total,"
                    " SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active"
                    " FROM stream_catalog")
    det_today = qone(con,
        "SELECT COUNT(*) c FROM fingerprint_detections"
        " WHERE deleted_at IS NULL AND air_time >= %s",
        (datetime.now(tz=TGU).replace(hour=0, minute=0, second=0, microsecond=0),))["c"]
    det_total = qone(con, "SELECT COUNT(*) c FROM fingerprint_detections WHERE deleted_at IS NULL")["c"]
    con.close()
    return jsonify({
        "streams_live":      {"online": online, "total": total_live},
        "catalog":           {"active": cat["active"] or 0, "total": cat["total"] or 0},
        "detections":        {"today": det_today, "total": det_total},
        "updated": datetime.now(tz=TGU).isoformat(),
    })

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    app.run(host=args.host, port=args.port)
