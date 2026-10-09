#!/usr/bin/env python3
"""
MediaDEV CloudWatch Log Shipper
Tail journal units + log files → CloudWatch Logs cada 30s.
"""
import os, json, time, subprocess, socket
import boto3
from botocore.exceptions import ClientError

SERVER    = os.environ.get("CW_SERVER_NAME", socket.gethostname())
REGION    = "us-east-1"
INTERVAL  = 30
STATE_FILE = f"/tmp/mediadev_logs_{SERVER}.json"

AWS_KEY    = os.environ["CW_AWS_KEY"]
AWS_SECRET = os.environ["CW_AWS_SECRET"]

logs = boto3.client("logs", region_name=REGION,
    aws_access_key_id=AWS_KEY, aws_secret_access_key=AWS_SECRET)

# ── Fuentes por servidor ───────────────────────────────────────────────────────
SOURCES_BY_SERVER = {
    "mediaCAP": [
        {"type": "journal", "unit": "stream-daemon",        "group": "MediaDEV/mediaCAP/stream-daemon"},
        {"type": "journal", "unit": "mediadev-health-engine","group": "MediaDEV/mediaCAP/health-engine"},
        {"type": "journal", "unit": "mediadev-gateway-api", "group": "MediaDEV/mediaCAP/gateway-api"},
        {"type": "file",    "path": "/var/log/supervisor/supervisord.log", "group": "MediaDEV/mediaCAP/supervisord"},
        {"type": "file",    "path": "/var/log/streams/video_uploader.log",  "group": "MediaDEV/mediaCAP/s3-uploader"},
    ],
    "mediaAPP": [
        {"type": "journal", "unit": "media-app",  "group": "MediaDEV/mediaAPP/media-app"},
        {"type": "journal", "unit": "chihambot",  "group": "MediaDEV/mediaAPP/chihambot"},
        {"type": "file",    "path": "/var/log/nginx/error.log", "group": "MediaDEV/mediaAPP/nginx-error"},
    ],
}

STREAM_NAME = SERVER

# ── Estado persistente (cursor por fuente) ────────────────────────────────────
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}

def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))

# ── CloudWatch Logs helpers ───────────────────────────────────────────────────
def ensure_group_stream(group):
    try:
        logs.create_log_group(logGroupName=group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    try:
        logs.create_log_stream(logGroupName=group, logStreamName=STREAM_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

def push_events(group, events):
    if not events:
        return
    # Ordenar por timestamp y truncar a 256KB por evento
    events = sorted(events, key=lambda e: e["timestamp"])
    events = [{"timestamp": e["timestamp"],
                "message": e["message"][:262000]} for e in events]
    # Lotes de 500 eventos o 1MB
    batch, batch_size = [], 0
    for ev in events:
        size = len(ev["message"].encode()) + 26
        if batch and (len(batch) >= 500 or batch_size + size > 900_000):
            _put(group, batch)
            batch, batch_size = [], 0
        batch.append(ev)
        batch_size += size
    if batch:
        _put(group, batch)

def _put(group, batch):
    try:
        logs.put_log_events(
            logGroupName=group,
            logStreamName=STREAM_NAME,
            logEvents=batch,
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("InvalidSequenceTokenException", "DataAlreadyAcceptedException"):
            pass  # ignorar — la API sin sequenceToken es idempotente en v2
        else:
            print(f"[WARN] put_log_events {group}: {e}", flush=True)

# ── Leer journal ──────────────────────────────────────────────────────────────
def read_journal(unit, cursor):
    args = ["journalctl", "-u", unit, "--output=json", "--no-pager", "-n", "500"]
    if cursor:
        args += ["--after-cursor", cursor]
    else:
        args += ["--since", "-10min"]   # primera vez: últimos 10 min
    try:
        out = subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return [], cursor

    events, new_cursor = [], cursor
    for line in out.strip().splitlines():
        try:
            entry = json.loads(line)
            ts_us  = int(entry.get("__REALTIME_TIMESTAMP", 0))
            ts_ms  = ts_us // 1000
            msg    = entry.get("MESSAGE", "")
            if isinstance(msg, list):          # bytes en algunos kernels
                msg = bytes(msg).decode("utf-8", errors="replace")
            if ts_ms and msg:
                events.append({"timestamp": ts_ms, "message": msg})
            new_cursor = entry.get("__CURSOR", new_cursor)
        except Exception:
            pass
    return events, new_cursor

# ── Leer archivo (tail por posición) ─────────────────────────────────────────
def read_file(path, offset):
    if not os.path.exists(path):
        return [], offset
    size = os.path.getsize(path)
    if offset > size:          # rotado
        offset = 0
    events = []
    with open(path, "r", errors="replace") as f:
        f.seek(offset)
        for line in f:
            line = line.rstrip()
            if line:
                ts_ms = int(time.time() * 1000)
                events.append({"timestamp": ts_ms, "message": line})
        offset = f.tell()
    return events, offset

# ── Loop principal ────────────────────────────────────────────────────────────
def main():
    sources = SOURCES_BY_SERVER.get(SERVER)
    if not sources:
        # Intentar match parcial (ej. mediaDEV → mediaCAP)
        for key in SOURCES_BY_SERVER:
            if key.lower() in SERVER.lower() or SERVER.lower() in key.lower():
                sources = SOURCES_BY_SERVER[key]
                break
    if not sources:
        print(f"[ERROR] No hay fuentes definidas para servidor '{SERVER}'", flush=True)
        return

    print(f"MediaDEV Log Shipper → server={SERVER} fuentes={len(sources)}", flush=True)

    # Crear grupos/streams en CloudWatch
    for src in sources:
        try:
            ensure_group_stream(src["group"])
            print(f"  ✓ {src['group']}", flush=True)
        except Exception as e:
            print(f"  ✗ {src['group']}: {e}", flush=True)

    state = load_state()

    while True:
        total = 0
        for src in sources:
            key = src.get("unit") or src.get("path")
            try:
                if src["type"] == "journal":
                    cursor = state.get(key)
                    events, new_cursor = read_journal(src["unit"], cursor)
                    state[key] = new_cursor
                else:
                    offset = state.get(key, 0)
                    events, new_offset = read_file(src["path"], offset)
                    state[key] = new_offset

                if events:
                    push_events(src["group"], events)
                    total += len(events)
            except Exception as e:
                print(f"[ERR] {key}: {e}", flush=True)

        save_state(state)
        if total:
            print(f"[OK] {total} líneas enviadas", flush=True)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
