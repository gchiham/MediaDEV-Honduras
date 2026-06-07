#!/usr/bin/env python3
"""MediaDEV Dashboard v4 — SQLite-backed, KPIs completos"""
import os, sys, json, sqlite3, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, abort
TGU = timezone(timedelta(hours=-6))  # America/Tegucigalpa GMT-6

app = Flask(__name__, template_folder="templates")

DB_PATH      = "/var/www/streams/mediadev.db"
STREAMS_ROOT = Path("/var/www/streams")
STREAMS = [
    "fm_941","hch_tv","radio_america","radio_choluteca","radio_el_patio",
    "radio_globo","radio_satelite","suave_fm","teleceiba",
    "xy_hrn","xy_sps","xy_tgu",
]

def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def ts_now(): return int(__import__("time").time())

def uptime_pct(con, sid, cutoff):
    tot = con.execute("SELECT COUNT(*) FROM metrics WHERE stream_id=? AND ts>=?", (sid,cutoff)).fetchone()[0]
    ok  = con.execute("SELECT COUNT(*) FROM metrics WHERE stream_id=? AND ts>=? AND status='OK'", (sid,cutoff)).fetchone()[0]
    return round(ok/tot*100, 1) if tot else None   # None = sin datos aún

def mb_sum(con, sid, cutoff):
    v = con.execute("SELECT SUM(bytes) FROM metrics WHERE stream_id=? AND ts>=?", (sid,cutoff)).fetchone()[0]
    return round((v or 0)/1048576, 1)

def bitrate_kbs(con, sid, cutoff):
    v = con.execute("SELECT SUM(bytes) FROM metrics WHERE stream_id=? AND ts>=?", (sid,cutoff)).fetchone()[0] or 0
    secs = ts_now() - cutoff
    return round(v / secs / 1024, 1) if secs > 0 else 0

def downs_count(con, sid, cutoff):
    return con.execute(
        "SELECT COUNT(*) FROM events WHERE stream_id=? AND ts>=? AND etype='DOWN'", (sid,cutoff)
    ).fetchone()[0]

def last_event_str(con, sid, etype):
    r = con.execute(
        "SELECT ts FROM events WHERE stream_id=? AND etype=? ORDER BY ts DESC LIMIT 1", (sid,etype)
    ).fetchone()
    if not r or not r[0]: return "—"
    return datetime.fromtimestamp(r[0], tz=TGU).strftime("%d/%m %H:%M")

def color(status):
    return {"OK":"ok","STALE":"stale","DISABLED":"disabled"}.get(status,"error")

# ─── MAIN DASHBOARD ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    con  = db()
    now  = ts_now()
    h24  = now - 86400

    # ── KPIs globales (1 query) ──
    statuses  = {r["stream_id"]: r for r in con.execute("SELECT * FROM stream_status")}
    online    = sum(1 for r in statuses.values() if r["status"] == "OK")
    cb_open   = sum(1 for r in statuses.values() if r["cb_state"] == "OPEN")
    incidents = sum(1 for r in statuses.values() if r["status"] not in ("OK","DISABLED"))
    restarts  = sum((r["restart_today"] or 0) for r in statuses.values())

    # ── Métricas 24h agregadas (1 query) ──
    m24 = {}
    for r in con.execute(
        "SELECT stream_id, COUNT(*) t, SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) ok,"
        " ROUND(SUM(bytes)/1048576.0,1) mb FROM metrics WHERE ts>=? GROUP BY stream_id",
        (h24,)
    ):
        m24[r[0]] = (r[1], r[2], r[3])   # (total, ok_count, mb)

    # ── Bitrate 1h agregado (1 query) ──
    m1h = {r[0]: (r[1] or 0) for r in con.execute(
        "SELECT stream_id, SUM(bytes) FROM metrics WHERE ts>=? GROUP BY stream_id",
        (now - 3600,)
    )}

    # ── Caídas 24h + última caída (1 query) ──
    ev24 = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT stream_id, COUNT(*), MAX(ts) FROM events"
        " WHERE ts>=? AND etype='DOWN' GROUP BY stream_id",
        (h24,)
    )}

    # ── Globales calculados ──
    tot_m = sum(v[0] for v in m24.values()) or 1
    ok_m  = sum(v[1] for v in m24.values())
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
        up24 = round(r24[1]/r24[0]*100, 1) if r24 and r24[0] else None
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
    row = con.execute("SELECT * FROM stream_status WHERE stream_id=?", (sid,)).fetchone()
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

    # ── Gráfico: uptime % por hora (1 query con GROUP BY) ──
    window = now - 24*3600
    hour_data = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT (ts/3600)*3600 h, COUNT(*) tot,"
        " SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) ok, SUM(bytes) bw"
        " FROM metrics WHERE stream_id=? AND ts>=?"
        " GROUP BY h ORDER BY h",
        (sid, window)
    )}
    chart_labels, chart_up, chart_bw = [], [], []
    for h in range(23, -1, -1):
        bucket = ((now - h*3600) // 3600) * 3600
        label  = datetime.fromtimestamp(bucket + 3600, tz=TGU).strftime("%Hh")
        hr     = hour_data.get(bucket)
        up_pct = round(hr[1]/hr[0]*100, 1) if hr and hr[0] else 0
        bw_mb  = round((hr[2] or 0)/1048576, 2) if hr else 0
        chart_labels.append(label)
        chart_up.append(up_pct)
        chart_bw.append(bw_mb)

    # ── Eventos recientes ──
    events = [{
        "ts":     datetime.fromtimestamp(e["ts"], tz=TGU).strftime("%d/%m %H:%M:%S"),
        "etype":  e["etype"],
        "detail": e["detail"],
    } for e in con.execute(
        "SELECT ts,etype,detail FROM events WHERE stream_id=? ORDER BY ts DESC LIMIT 25", (sid,)
    )]

    # ── Auditoría en disco ──
    seg_count = con.execute("SELECT COUNT(*) FROM segments WHERE stream_id=?", (sid,)).fetchone()[0]
    seg_bytes = con.execute("SELECT SUM(bytes) FROM segments WHERE stream_id=?", (sid,)).fetchone()[0] or 0
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

# ─── API ──────────────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    con = db()
    rows = {r["stream_id"]: dict(r) for r in con.execute("SELECT * FROM stream_status")}
    con.close()
    ok = sum(1 for r in rows.values() if r["status"]=="OK")
    return jsonify({"streams": rows, "summary": {"ok":ok,"total":len(STREAMS)},
                    "updated": datetime.now(tz=TGU).isoformat()})

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    app.run(host=args.host, port=args.port)
