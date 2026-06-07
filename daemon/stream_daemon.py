#!/usr/bin/env python3
"""
MediaDEV Stream Daemon — optimizado 1 vCPU / 2GB RAM
Health: 15s | Index: 60s | Metrics: 60s | Record: 120s | Cleanup: 1800s
"""
import os, sys, sqlite3, subprocess, time, signal, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_PATH      = "/var/www/streams/mediadev.db"
STREAMS_ROOT = Path("/var/www/streams")
LOG_FILE     = "/var/log/streams/daemon.log"

STREAMS = [
    "fm_941","hch_tv","radio_america","radio_choluteca","radio_el_patio",
    "radio_globo","radio_satelite","suave_fm","teleceiba",
    "xy_hrn","xy_sps","xy_tgu",
]

STALE_SECS     = 30
CB_FAIL_OPEN   = 5
CB_RESET_SECS  = 1800
SEG_DURATION   = 4
TGU = timezone(timedelta(hours=-6))  # America/Tegucigalpa GMT-6
KEEP_SEG_HOURS = 8
KEEP_MET_DAYS  = 7

# Intervalos conservadores para 1 vCPU
INTERVAL_HEALTH = 15    # era 3s  → ahora 15s
INTERVAL_INDEX  = 60    # era 10s → ahora 60s
INTERVAL_METRIC = 60
INTERVAL_RECORD = 120
INTERVAL_CLEAN  = 1800  # era 900s → ahora 30min
LOOP_SLEEP      = 2     # era 0.5s → ahora 2s

# ── LOGGING ───────────────────────────────────────────────────────────────────
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("daemon")

# ── SCHEMA ────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS stream_status (
    stream_id      TEXT PRIMARY KEY,
    status         TEXT DEFAULT 'UNKNOWN',
    sup            TEXT DEFAULT 'UNKNOWN',
    segs           INTEGER DEFAULT 0,
    age            INTEGER DEFAULT 0,
    cb_state       TEXT DEFAULT 'CLOSED',
    cb_fails       INTEGER DEFAULT 0,
    cb_since       INTEGER DEFAULT 0,
    restart_today  INTEGER DEFAULT 0,
    last_down      INTEGER DEFAULT 0,
    last_up        INTEGER DEFAULT 0,
    updated_at     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
    stream_id  TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    status     TEXT    NOT NULL,
    segs       INTEGER DEFAULT 0,
    bytes      INTEGER DEFAULT 0,
    PRIMARY KEY (stream_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_met ON metrics(stream_id, ts DESC);

CREATE TABLE IF NOT EXISTS segments (
    stream_id  TEXT    NOT NULL,
    filename   TEXT    NOT NULL,
    ts_start   INTEGER NOT NULL,
    ts_end     INTEGER NOT NULL,
    bytes      INTEGER DEFAULT 0,
    PRIMARY KEY (stream_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_seg ON segments(stream_id, ts_start);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id  TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    etype      TEXT    NOT NULL,
    detail     TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evt ON events(stream_id, ts DESC);
"""

def init_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for sid in STREAMS:
        db.execute("INSERT OR IGNORE INTO stream_status (stream_id) VALUES (?)", (sid,))
    db.commit()
    return db

def log_event(db, sid, etype, detail=""):
    db.execute(
        "INSERT INTO events (stream_id,ts,etype,detail) VALUES (?,?,?,?)",
        (sid, int(time.time()), etype, detail)
    )

# ── HEALTH CHECK ──────────────────────────────────────────────────────────────
def sup_statuses():
    """Una sola llamada para todos los streams."""
    try:
        out = subprocess.check_output(
            ["timeout","5","supervisorctl","status"],
            stderr=subprocess.DEVNULL, text=True
        )
        return {
            line.split()[0].replace("stream_",""): line.split()[1]
            for line in out.splitlines() if len(line.split()) >= 2
        }
    except:
        return {}

def m3u8_seg_count(m3u8: Path) -> int:
    """Cuenta segmentos desde el m3u8 — sin tocar el disco."""
    try:
        return sum(1 for l in m3u8.read_text().splitlines() if l.endswith(".ts"))
    except:
        return 0

def restart_stream(db, sid):
    subprocess.Popen(
        ["supervisorctl","restart",f"stream_{sid}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    db.execute(
        "UPDATE stream_status SET restart_today=restart_today+1 WHERE stream_id=?",
        (sid,)
    )

def do_health(db, state):
    now  = int(time.time())
    sups = sup_statuses()

    for sid in STREAMS:
        row = db.execute(
            "SELECT status,cb_state,cb_fails,cb_since,last_down,last_up "
            "FROM stream_status WHERE stream_id=?", (sid,)
        ).fetchone()
        prev  = row["status"]
        cb_st = row["cb_state"]
        cb_f  = row["cb_fails"]
        cb_s  = row["cb_since"]
        l_d   = row["last_down"]
        l_u   = row["last_up"]

        sup  = sups.get(sid, "UNKNOWN")
        m3u8 = STREAMS_ROOT / sid / "index.m3u8"

        # Leer SOLO el m3u8 — sin glob en disco
        if m3u8.exists():
            age  = now - int(m3u8.stat().st_mtime)
            segs = m3u8_seg_count(m3u8)
            ok   = age <= STALE_SECS and segs > 0
        else:
            age = segs = 0; ok = False

        # Circuit Breaker
        if cb_st == "OPEN":
            if now - cb_s >= CB_RESET_SECS:
                cb_st = "CLOSED"; cb_f = 0
                log.info(f"[{sid}] CB → CLOSED (reset)")
                log_event(db, sid, "CB_CLOSE")
                restart_stream(db, sid)
            else:
                db.execute(
                    "UPDATE stream_status SET status='DISABLED',sup=?,segs=?,age=?,"
                    "cb_state=?,cb_fails=?,updated_at=? WHERE stream_id=?",
                    (sup, segs, age, cb_st, cb_f, now, sid)
                )
                db.commit()
                state["cur"][sid] = "DISABLED"
                continue

        if ok:
            new_st = "OK"; cb_f = 0
            if prev not in ("OK", "UNKNOWN"):
                log.info(f"[{sid}] ↑ UP")
                log_event(db, sid, "UP")
                l_u = now
        else:
            new_st = "STALE" if m3u8.exists() else "NO_M3U8"
            cb_f  += 1
            if prev == "OK":
                log.warning(f"[{sid}] ↓ DOWN age={age}s cb_fails={cb_f}")
                log_event(db, sid, "DOWN", f"age={age}s")
                l_d = now
                if cb_f <= CB_FAIL_OPEN:
                    restart_stream(db, sid)
            if cb_f >= CB_FAIL_OPEN and cb_st == "CLOSED":
                cb_st = "OPEN"; cb_s = now
                log.warning(f"[{sid}] CB → OPEN tras {cb_f} fallos")
                log_event(db, sid, "CB_OPEN", f"fails={cb_f}")

        db.execute(
            "UPDATE stream_status SET status=?,sup=?,segs=?,age=?,cb_state=?,"
            "cb_fails=?,cb_since=?,last_down=?,last_up=?,updated_at=? "
            "WHERE stream_id=?",
            (new_st, sup, segs, age, cb_st, cb_f, cb_s, l_d, l_u, now, sid)
        )
        db.commit()
        state["cur"][sid] = new_st

# ── SEGMENT INDEXER ───────────────────────────────────────────────────────────
def do_index(db, state):
    """Solo indexa archivos NUEVOS (mtime >= último run)."""
    since = state.get("last_index_ts", 0) - SEG_DURATION
    now   = int(time.time())

    for sid in STREAMS:
        sdir = STREAMS_ROOT / sid
        if not sdir.exists():
            continue
        new = []
        for seg in sdir.glob("seg_*.ts"):
            try:
                st = seg.stat()
                if st.st_mtime < since:
                    continue   # ya indexado en ciclo anterior
                mtime = int(st.st_mtime)
                new.append((sid, seg.name, mtime - SEG_DURATION, mtime, st.st_size))
                state["bw"][sid] = state["bw"].get(sid, 0) + st.st_size
            except:
                pass
        if new:
            db.executemany(
                "INSERT OR IGNORE INTO segments "
                "(stream_id,filename,ts_start,ts_end,bytes) VALUES (?,?,?,?,?)",
                new
            )
            db.commit()

    state["last_index_ts"] = now

# ── METRICS ───────────────────────────────────────────────────────────────────
def do_metrics(db, state):
    now = int(time.time())
    ts  = now - now % 60
    rows = []
    for sid in STREAMS:
        r    = db.execute("SELECT segs FROM stream_status WHERE stream_id=?", (sid,)).fetchone()
        segs = r["segs"] if r else 0
        bw   = state["bw"].pop(sid, 0)
        rows.append((sid, ts, state["cur"].get(sid, "UNKNOWN"), segs, bw))
    db.executemany(
        "INSERT OR REPLACE INTO metrics (stream_id,ts,status,segs,bytes) VALUES (?,?,?,?,?)",
        rows
    )
    db.commit()

# ── HOURLY RECORDINGS ─────────────────────────────────────────────────────────
def do_record(db, state):
    now = int(time.time())
    if (now % 3600) // 60 > 3:
        return
    h_start = now - now % 3600 - 3600
    h_end   = h_start + 3600
    h_label = datetime.fromtimestamp(h_start, tz=TGU).strftime("%Y-%m-%d_%Hh")

    for sid in STREAMS:
        rec_dir = STREAMS_ROOT / sid / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        out = rec_dir / f"{h_label}.mp3"
        if out.exists():
            continue

        rows = db.execute(
            "SELECT filename FROM segments WHERE stream_id=? "
            "AND ts_start>=? AND ts_start<? ORDER BY ts_start",
            (sid, h_start, h_end)
        ).fetchall()

        segs = [STREAMS_ROOT / sid / r["filename"] for r in rows]
        segs = [f for f in segs if f.exists()]
        if len(segs) < 10:
            continue

        log.info(f"[{sid}] Grabando {h_label} ({len(segs)} segs)")
        result = subprocess.run(
            ["ffmpeg","-y","-loglevel","error","-i","pipe:0",
             "-c:a","libmp3lame","-b:a","64k","-ac","1","-ar","22050",str(out)],
            input=b"".join(f.read_bytes() for f in segs),
            capture_output=True
        )
        if result.returncode == 0:
            log.info(f"[{sid}] {h_label}.mp3 OK ({out.stat().st_size//1024}KB)")
            for old in sorted(rec_dir.glob("*.mp3"), key=lambda f: f.name, reverse=True)[KEEP_SEG_HOURS:]:
                old.unlink()
        else:
            out.unlink(missing_ok=True)
            log.error(f"[{sid}] Fallo grabando {h_label}")

# ── CLEANUP ───────────────────────────────────────────────────────────────────
def do_cleanup(db, state):
    cutoff  = int(time.time()) - KEEP_SEG_HOURS * 3600
    deleted = 0
    for sid in STREAMS:
        old = db.execute(
            "SELECT filename FROM segments WHERE stream_id=? AND ts_end<?",
            (sid, cutoff)
        ).fetchall()
        for r in old:
            (STREAMS_ROOT / sid / r["filename"]).unlink(missing_ok=True)
            deleted += 1
        if old:
            db.execute("DELETE FROM segments WHERE stream_id=? AND ts_end<?", (sid, cutoff))
    db.execute("DELETE FROM metrics WHERE ts<?", (int(time.time()) - KEEP_MET_DAYS * 86400,))
    db.execute("DELETE FROM events  WHERE ts<?", (int(time.time()) - 30 * 86400,))
    db.commit()
    if deleted:
        log.info(f"Cleanup: {deleted} segmentos eliminados")

# ── DAILY RESET ───────────────────────────────────────────────────────────────
_last_reset_day = None
def do_daily_reset(db, state):
    global _last_reset_day
    today = datetime.now(tz=TGU).date()
    if _last_reset_day == today:
        return
    _last_reset_day = today
    db.execute("UPDATE stream_status SET restart_today=0")
    db.commit()
    log.info("Contadores diarios reseteados")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("MediaDEV Stream Daemon — 1vCPU mode")
    log.info(f"health={INTERVAL_HEALTH}s index={INTERVAL_INDEX}s "
             f"clean={INTERVAL_CLEAN}s loop={LOOP_SLEEP}s")

    db    = init_db()
    state = {
        "cur":           {s: "UNKNOWN" for s in STREAMS},
        "bw":            {},
        "last_index_ts": 0,
    }

    running = [True]
    def _stop(sig, frame):
        running[0] = False
        log.info("Señal de parada recibida")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    last = {k: 0 for k in ("health","index","metric","record","clean","daily")}
    log.info(f"Monitoreando {len(STREAMS)} streams")

    while running[0]:
        now = time.time()

        if now - last["health"] >= INTERVAL_HEALTH:
            do_health(db, state);  last["health"] = now

        if now - last["index"] >= INTERVAL_INDEX:
            do_index(db, state);   last["index"]  = now

        if now - last["metric"] >= INTERVAL_METRIC:
            do_metrics(db, state); last["metric"] = now

        if now - last["record"] >= INTERVAL_RECORD:
            do_record(db, state);  last["record"] = now

        if now - last["clean"] >= INTERVAL_CLEAN:
            do_cleanup(db, state); last["clean"]  = now

        if now - last["daily"] >= 3600:
            do_daily_reset(db, state); last["daily"] = now

        time.sleep(LOOP_SLEEP)

    db.close()
    log.info("Daemon detenido")

if __name__ == "__main__":
    main()
