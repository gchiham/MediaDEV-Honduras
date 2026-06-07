#!/usr/bin/env python3
"""MediaDEV Stream Dashboard v3"""
import os, sys, json, argparse, csv, glob
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template, jsonify, abort

app = Flask(__name__, template_folder="templates")

STATUS_FILE  = "/var/www/streams/status.json"
HISTORY_DB   = "/var/www/streams/metrics/history.db"
STREAMS_ROOT = "/var/www/streams"
HLS_BASE     = "/streams"   # served by nginx on port 80

# ── helpers ──────────────────────────────────────────────────────────────────

def load_status():
    if not Path(STATUS_FILE).exists():
        return {"streams": {}, "summary": {"ok": 0, "fail": 0, "total": 0}}
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return {"streams": {}, "summary": {"ok": 0, "fail": 0, "total": 0}}

def parse_history(stream_id=None, hours=24):
    """Lee histórico. Si stream_id es None, devuelve todos."""
    history = defaultdict(list)
    cutoff  = datetime.utcnow() - timedelta(hours=hours)
    if not Path(HISTORY_DB).exists():
        return history
    try:
        with open(HISTORY_DB) as f:
            reader = csv.DictReader(f, delimiter='|')
            for row in reader:
                if stream_id and row.get('stream_id') != stream_id:
                    continue
                try:
                    ts = datetime.strptime(row['timestamp'], "%Y-%m-%d %H:%M:%S")
                    if ts >= cutoff:
                        history[row['stream_id']].append({
                            'ts':     ts.isoformat(),
                            'status': row['status'],
                            'segs':   int(row.get('segs', 0)),
                            'mb':     int(row.get('mb_total', 0)),
                        })
                except:
                    pass
    except:
        pass
    return history

def calc_kpis(stream_id, history, hours=24):
    data = history.get(stream_id, [])
    if not data:
        return {'uptime': 0, 'mb_total': 0, 'throughput_avg': 0, 'downtime_events': 0}
    total    = len(data)
    ok_count = sum(1 for d in data if d['status'] == 'OK')
    uptime   = round(ok_count / total * 100, 1) if total else 0
    mb_total = sum(d['mb'] for d in data)
    down_events = sum(
        1 for i in range(1, len(data))
        if data[i-1]['status'] == 'OK' and data[i]['status'] != 'OK'
    )
    return {
        'uptime':          uptime,
        'mb_total':        mb_total,
        'throughput_avg':  round(mb_total / total, 1) if total else 0,
        'downtime_events': down_events,
    }

def disk_audit_info(stream_id):
    """Cuenta segmentos en disco y estima horas almacenadas."""
    pattern = f"{STREAMS_ROOT}/{stream_id}/seg_*.ts"
    files   = glob.glob(pattern)
    count   = len(files)
    total_bytes = sum(os.path.getsize(f) for f in files if os.path.exists(f))
    hours_stored = round((count * 4) / 3600, 1)   # 4s por segmento
    return {
        'segments': count,
        'bytes':    total_bytes,
        'mb':       round(total_bytes / 1048576, 1),
        'hours':    hours_stored,
    }

def enrich_streams(data, history):
    streams = []
    for sid, info in data.get("streams", {}).items():
        status = info.get("status", "UNKNOWN")
        kpis   = calc_kpis(sid, history)
        color  = "ok" if status == "OK" else ("stale" if status == "STALE" else "error")
        label  = "ACTIVO" if status == "OK" else ("DESACT." if status == "STALE" else "ERROR")
        streams.append({
            "id":               sid,
            "status":           label,
            "color":            color,
            "segs":             info.get("segs", 0),
            "age":              info.get("age", 0),
            "sup":              info.get("sup", "UNKNOWN"),
            "uptime":           kpis['uptime'],
            "mb_total":         kpis['mb_total'],
            "throughput":       kpis['throughput_avg'],
            "downtime_events":  kpis['downtime_events'],
            "url":              f"{HLS_BASE}/{sid}/index.m3u8",
        })
    return sorted(streams, key=lambda x: x["id"])

# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    data    = load_status()
    history = parse_history(hours=24)
    streams = enrich_streams(data, history)
    summary = data.get("summary", {})
    updated = data.get("updated", "—")
    total_uptime = round(sum(s['uptime'] for s in streams) / len(streams), 1) if streams else 0
    total_mb     = sum(s['mb_total'] for s in streams)
    return render_template("dashboard_main.html",
                           streams=streams,
                           summary=summary,
                           updated=updated,
                           total_uptime=total_uptime,
                           total_mb=total_mb)

@app.route("/stream/<stream_id>")
def stream_detail(stream_id):
    data = load_status()
    if stream_id not in data.get("streams", {}):
        abort(404)

    info    = data["streams"][stream_id]
    history_24h = parse_history(stream_id, hours=24)
    history_8h  = parse_history(stream_id, hours=8)

    kpis_24h = calc_kpis(stream_id, history_24h)
    kpis_8h  = calc_kpis(stream_id, history_8h)
    kpis_1h  = calc_kpis(stream_id, parse_history(stream_id, hours=1))

    status  = info.get("status", "UNKNOWN")
    color   = "ok" if status == "OK" else ("stale" if status == "STALE" else "error")
    label   = "ACTIVO" if status == "OK" else ("DESACT." if status == "STALE" else "ERROR")

    audit   = disk_audit_info(stream_id)
    updated = data.get("updated", "—")

    # Serie temporal para gráfico 24h
    entries_24h = history_24h.get(stream_id, [])
    chart_labels = [e['ts'][11:16] for e in entries_24h]   # HH:MM
    chart_mb     = [e['mb'] for e in entries_24h]
    chart_status = [1 if e['status'] == 'OK' else 0 for e in entries_24h]

    all_streams = sorted(data["streams"].keys())

    return render_template("stream_detail.html",
                           stream_id=stream_id,
                           status=label,
                           color=color,
                           segs=info.get("segs", 0),
                           age=info.get("age", 0),
                           sup=info.get("sup", "UNKNOWN"),
                           kpis_1h=kpis_1h,
                           kpis_8h=kpis_8h,
                           kpis_24h=kpis_24h,
                           audit=audit,
                           updated=updated,
                           chart_labels=json.dumps(chart_labels),
                           chart_mb=json.dumps(chart_mb),
                           chart_status=json.dumps(chart_status),
                           url=f"{HLS_BASE}/{stream_id}/index.m3u8",
                           all_streams=all_streams)

@app.route("/api/status")
def api_status():
    data    = load_status()
    history = parse_history(hours=24)
    streams = enrich_streams(data, history)
    return jsonify({"streams": streams, "summary": data.get("summary", {}),
                    "updated": data.get("updated", "—")})

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "9000")))
    args = p.parse_args()
    print(f"\n📺 MediaDEV Dashboard v3 → http://localhost:{args.port}\n")
    app.run(host=args.host, port=args.port, threaded=True)
