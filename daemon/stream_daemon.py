#!/usr/bin/env python3
"""
MediaDEV Stream Daemon — sin SQLite, estado en memoria + mtime
Health: 15s | Record: 120s | Cleanup: 1800s
"""
import os, sys, subprocess, time, signal, logging, boto3
from botocore.exceptions import ClientError
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
STREAMS_ROOT = Path(os.environ.get("STREAMS_ROOT", "/var/www/streams"))
LOG_FILE     = os.environ.get("STREAMS_LOG",  "/var/log/streams/daemon.log")

STREAMS = [
    "fm_941","hch_tv","radio_america","radio_choluteca","radio_el_patio",
    "radio_globo","radio_satelite","suave_fm","teleceiba",
    "xy_hrn","xy_sps","xy_tgu",
]

STALE_SECS          = 60
CB_FAIL_OPEN        = 5
CB_RESET_SECS       = 1800
RESTART_AFTER_FAILS = 3
SEG_DURATION        = 4
TGU = timezone(timedelta(hours=-6))
KEEP_SEG_HOURS = 8
KEEP_MP3_COUNT = 8

# ── S3 ────────────────────────────────────────────────────────────────────────
S3_BUCKET  = os.environ.get("S3_BUCKET",  "mediadev-recordings")
S3_REGION  = os.environ.get("S3_REGION",  "us-east-1")
PEER_ROLE  = os.environ.get("PEER_ROLE",  "primary")
BACKUP_PFX = "_backup"

def s3_upload(local_path: Path, stream_id: str) -> bool:
    try:
        s3 = boto3.client("s3", region_name=S3_REGION)
        date_part = local_path.name[:10]
        year, month = date_part[:4], date_part[5:7]
        canon = f"{stream_id}/{year}/{month}/{local_path.name}"
        key   = f"{BACKUP_PFX}/{canon}" if PEER_ROLE == "backup" else canon
        s3.upload_file(str(local_path), S3_BUCKET, key,
                       ExtraArgs={"ContentType": "audio/mpeg"})
        log.info(f"[{stream_id}] S3 OK [{PEER_ROLE}] → s3://{S3_BUCKET}/{key}")
        return True
    except Exception as e:
        log.error(f"[{stream_id}] S3 FAIL: {e}")
        return False

# Intervalos
INTERVAL_HEALTH = 15
INTERVAL_RECORD = 120
INTERVAL_CLEAN  = 1800
LOOP_SLEEP      = 2

# ── LOGGING ───────────────────────────────────────────────────────────────────
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("daemon")

# ── STATE (en memoria) ────────────────────────────────────────────────────────
def init_state():
    return {
        sid: {
            "status":        "UNKNOWN",
            "cb_state":      "CLOSED",
            "cb_fails":      0,
            "cb_since":      0,
            "last_down":     0,
            "last_up":       0,
            "restart_today": 0,
        }
        for sid in STREAMS
    }

# ── HEALTH CHECK ──────────────────────────────────────────────────────────────
def sup_statuses():
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
    try:
        return sum(1 for l in m3u8.read_text().splitlines() if l.endswith(".ts"))
    except:
        return 0

def restart_stream(state, sid):
    subprocess.Popen(
        ["supervisorctl","restart",f"stream_{sid}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    state[sid]["restart_today"] += 1

def do_health(state):
    now  = int(time.time())
    sups = sup_statuses()

    for sid in STREAMS:
        s    = state[sid]
        prev = s["status"]

        sup  = sups.get(sid, "UNKNOWN")
        m3u8 = STREAMS_ROOT / sid / "index.m3u8"

        if m3u8.exists():
            age  = now - int(m3u8.stat().st_mtime)
            segs = m3u8_seg_count(m3u8)
            empty_playlist = (segs == 0 and m3u8.stat().st_size > 50)
            ok   = age <= STALE_SECS and segs > 0
        else:
            age = segs = 0; ok = False; empty_playlist = False

        if s["cb_state"] == "OPEN":
            if now - s["cb_since"] >= CB_RESET_SECS:
                s["cb_state"] = "CLOSED"; s["cb_fails"] = 0
                log.info(f"[{sid}] CB → CLOSED (reset)")
                restart_stream(state, sid)
            else:
                s["status"] = "DISABLED"
                continue

        if ok:
            s["cb_fails"] = 0
            if prev not in ("OK", "UNKNOWN"):
                log.info(f"[{sid}] ↑ UP")
                s["last_up"] = now
            s["status"] = "OK"
        else:
            s["status"] = "STALE" if m3u8.exists() else "NO_M3U8"
            s["cb_fails"] += 1
            if prev == "OK":
                log.warning(f"[{sid}] ↓ DOWN age={age}s cb_fails={s['cb_fails']} empty={empty_playlist}")
                s["last_down"] = now
            if not empty_playlist and s["cb_fails"] >= RESTART_AFTER_FAILS and s["cb_fails"] <= CB_FAIL_OPEN:
                log.info(f"[{sid}] Reiniciando ffmpeg tras {s['cb_fails']} fallos")
                restart_stream(state, sid)
            if s["cb_fails"] >= CB_FAIL_OPEN and s["cb_state"] == "CLOSED":
                s["cb_state"] = "OPEN"; s["cb_since"] = now
                log.warning(f"[{sid}] CB → OPEN tras {s['cb_fails']} fallos")

# ── HOURLY RECORDINGS ─────────────────────────────────────────────────────────
def do_record(state):
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

        segs = sorted(
            [f for f in (STREAMS_ROOT / sid).glob("seg_*.ts")
             if f.exists() and h_start <= f.stat().st_mtime < h_end],
            key=lambda f: f.stat().st_mtime
        )
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
            s3_upload(out, sid)
            for old in sorted(rec_dir.glob("*.mp3"), key=lambda f: f.name, reverse=True)[KEEP_MP3_COUNT:]:
                old.unlink()
        else:
            out.unlink(missing_ok=True)
            log.error(f"[{sid}] Fallo grabando {h_label}")

# ── CLEANUP ───────────────────────────────────────────────────────────────────
def do_cleanup(state):
    cutoff  = time.time() - KEEP_SEG_HOURS * 3600
    deleted = 0
    for sid in STREAMS:
        for seg in (STREAMS_ROOT / sid).glob("seg_*.ts"):
            try:
                if seg.stat().st_mtime < cutoff:
                    seg.unlink()
                    deleted += 1
            except:
                pass
    if deleted:
        log.info(f"Cleanup: {deleted} segmentos eliminados")

# ── DAILY RESET ───────────────────────────────────────────────────────────────
_last_reset_day = None
def do_daily_reset(state):
    global _last_reset_day
    today = datetime.now(tz=TGU).date()
    if _last_reset_day == today:
        return
    _last_reset_day = today
    for sid in STREAMS:
        state[sid]["restart_today"] = 0
    log.info("Contadores diarios reseteados")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("MediaDEV Stream Daemon — sin SQLite")
    log.info(f"health={INTERVAL_HEALTH}s clean={INTERVAL_CLEAN}s loop={LOOP_SLEEP}s")

    state = init_state()

    running = [True]
    def _stop(sig, frame):
        running[0] = False
        log.info("Señal de parada recibida")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    last = {k: 0 for k in ("health","record","clean","daily")}
    log.info(f"Monitoreando {len(STREAMS)} streams")

    while running[0]:
        now = time.time()

        if now - last["health"] >= INTERVAL_HEALTH:
            do_health(state);        last["health"] = now

        if now - last["record"] >= INTERVAL_RECORD:
            do_record(state);        last["record"] = now

        if now - last["clean"] >= INTERVAL_CLEAN:
            do_cleanup(state);       last["clean"]  = now

        if now - last["daily"] >= 3600:
            do_daily_reset(state);   last["daily"]  = now

        time.sleep(LOOP_SLEEP)

    log.info("Daemon detenido")

if __name__ == "__main__":
    main()
