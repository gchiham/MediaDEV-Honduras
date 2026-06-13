#!/usr/bin/env python3
"""
Video Segment Uploader — sube .ts de streams TV a S3 con nombre de epoch.
Usa mtime del archivo para calcular el epoch (sin SQLite).

S3 path: video_segments/{stream_id}/{YYYY}/{MM}/{DD}/{epoch_start}_{epoch_end}.ts

Además extrae audio por hora para alimentar el Destroyer:
  s3://{bucket}/{stream_id}/{YYYY}/{MM}/{YYYY-MM-DD_HHh}.mp3
"""
import os, time, json, logging, shutil, subprocess, boto3
from pathlib import Path
from datetime import datetime, timezone, timedelta

STREAMS_ROOT   = Path(os.environ.get("STREAMS_ROOT", "/var/www/streams"))
STATIONS       = Path("/opt/media-ai/config/stations.json")
S3_BUCKET      = os.environ.get("S3_BUCKET",  "mediadev-recordings")
S3_REGION      = os.environ.get("S3_REGION",  "us-east-1")
S3_PREFIX      = "video_segments"
AUDIO_DIR      = Path(os.environ.get("TV_AUDIO_DIR", "/var/www/streams/_tv_audio"))
HLS_KEEP       = 12
SCAN_INTERVAL  = 15
SEGMENT_DUR    = 4
TGU            = timezone(timedelta(hours=-6))
MP3_NAMING_MODE = os.environ.get("MP3_NAMING_MODE", "utc").strip().lower()

PG_HOST = os.environ.get("PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PG_PORT", "25060"))
PG_DB   = os.environ.get("PG_DB", "destroyer_db")
PG_USER = os.environ.get("PG_USER", "destroyer")
PG_PASS = os.environ.get("PG_PASS", "")
PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "utc_v2")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/streams/video_uploader.log"),
    ]
)
log = logging.getLogger("video-uploader")

# Per-stream audio accumulation state: {stream_id: {hour_epoch, segs_dir}}
_audio_state: dict = {}
_schema_cols: dict[str, set[str]] = {}


def get_tv_streams() -> list:
    data = json.load(open(STATIONS))
    return [s["id"] for s in data["stations"]
            if s.get("type") == "tv" and s.get("enabled", True)]

def get_s3():
    return boto3.client("s3", region_name=S3_REGION)

def s3_key(stream_id: str, epoch_start: int, epoch_end: int) -> str:
    dt = datetime.fromtimestamp(epoch_start, tz=timezone.utc)
    return f"{S3_PREFIX}/{stream_id}/{dt.strftime('%Y')}/{dt.strftime('%m')}/{dt.strftime('%d')}/{epoch_start}_{epoch_end}.ts"

def _hour_label(hour_epoch: int) -> str:
    if MP3_NAMING_MODE == "legacy_hn":
        dt = datetime.fromtimestamp(hour_epoch, tz=TGU)
        return dt.strftime("%Y-%m-%d_%Hh")

    dt = datetime.fromtimestamp(hour_epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%HZ")


# ── Audio hourly accumulation ─────────────────────────────────────────────────

def _audio_s3_key(stream_id: str, h_epoch: int) -> str:
    dt = datetime.fromtimestamp(h_epoch, tz=timezone.utc)
    return f"{stream_id}/{dt.year}/{dt.month:02d}/{_hour_label(h_epoch)}.mp3"

def _table_columns(conn, table: str) -> set[str]:
    cols = _schema_cols.get(table)
    if cols is not None:
        return cols

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        cols = {row[0] for row in cur.fetchall()}
    _schema_cols[table] = cols
    return cols

def _db_register(mp3_key: str, stream_id: str, recorded_date: str, hour_start_utc: datetime) -> None:
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            dbname=PG_DB, user=PG_USER, password=PG_PASS
        )
        with conn.cursor() as cur:
            cols = _table_columns(conn, "s3_scan_log")
            insert_cols = ["s3_key", "stream", "recorded_date", "status", "updated_at"]
            values = [mp3_key, stream_id, recorded_date, "pending", datetime.now(timezone.utc)]

            if "hour_start_utc" in cols:
                insert_cols.append("hour_start_utc")
                values.append(hour_start_utc.astimezone(timezone.utc))
            if "pipeline_version" in cols:
                insert_cols.append("pipeline_version")
                values.append(PIPELINE_VERSION)

            cur.execute(
                f"""
                INSERT INTO s3_scan_log ({', '.join(insert_cols)})
                VALUES ({', '.join(['%s'] * len(values))})
                ON CONFLICT (s3_key) DO NOTHING
                """,
                values,
            )
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[{stream_id}] DB register error: {e}")

def flush_audio_hour(s3_client, stream_id: str, hour_epoch: int, segs_dir: Path) -> None:
    """Concatena mini-segs de audio acumulados, crea MP3, sube a S3, registra en DB."""
    segs    = sorted(segs_dir.glob("*.ts"))
    h_label = _hour_label(hour_epoch)
    rec_day = datetime.fromtimestamp(hour_epoch, tz=timezone.utc).strftime("%Y-%m-%d")

    if len(segs) < 10:
        log.warning(f"[{stream_id}] audio flush {h_label}: {len(segs)} segs — omitiendo")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return

    mp3_path   = segs_dir / f"{h_label}.mp3"
    concat_txt = segs_dir / "list.txt"
    concat_txt.write_text("\n".join(f"file '{p}'" for p in segs) + "\n")

    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(concat_txt),
         "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", "-ar", "22050",
         str(mp3_path)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        log.error(f"[{stream_id}] audio flush ffmpeg error: {r.stderr[-300:]}")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return

    key = _audio_s3_key(stream_id, hour_epoch)
    hour_start_utc = datetime.fromtimestamp(hour_epoch, tz=timezone.utc)
    try:
        s3_client.upload_file(str(mp3_path), S3_BUCKET, key)
        log.info(f"[{stream_id}] {h_label}.mp3 → s3://{S3_BUCKET}/{key}  ({len(segs)} segs)")
    except Exception as e:
        log.error(f"[{stream_id}] audio upload error: {e}")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return

    _db_register(key, stream_id, rec_day, hour_start_utc)
    shutil.rmtree(segs_dir, ignore_errors=True)


def save_audio_seg(s3_client, stream_id: str, seg_path: Path, epoch_start: int) -> None:
    """Extrae audio del segmento de video y lo acumula en el directorio de la hora actual."""
    hour_epoch = (epoch_start // 3600) * 3600
    state      = _audio_state.get(stream_id, {})

    # Si cambió la hora, flush de la hora anterior
    if state.get("hour_epoch") is not None and state["hour_epoch"] != hour_epoch:
        flush_audio_hour(s3_client, stream_id, state["hour_epoch"], state["segs_dir"])
        state = {}

    # Inicializar estado para la hora actual
    if state.get("hour_epoch") != hour_epoch:
        h_label  = _hour_label(hour_epoch)
        new_dir  = AUDIO_DIR / stream_id / h_label
        new_dir.mkdir(parents=True, exist_ok=True)
        state    = {"hour_epoch": hour_epoch, "segs_dir": new_dir}
        _audio_state[stream_id] = state

    # Extraer audio como TS audio-only (AAC copy, rápido sin re-encode)
    audio_out = state["segs_dir"] / f"{epoch_start:010d}.ts"
    if audio_out.exists():
        return

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(seg_path), "-vn", "-c:a", "copy", str(audio_out)],
        capture_output=True
    )


# ── Video upload ──────────────────────────────────────────────────────────────

def upload_segment(s3_client, seg_path: Path, stream_id: str) -> bool:
    mtime       = int(seg_path.stat().st_mtime)
    epoch_start = mtime - SEGMENT_DUR
    epoch_end   = mtime
    key = s3_key(stream_id, epoch_start, epoch_end)
    try:
        # Extraer audio ANTES de borrar el .ts de video
        save_audio_seg(s3_client, stream_id, seg_path, epoch_start)
        s3_client.upload_file(str(seg_path), S3_BUCKET, key,
                              ExtraArgs={"ContentType": "video/mp2t"})
        seg_path.unlink()
        return True
    except Exception as e:
        log.error(f"[{stream_id}] Error subiendo {seg_path.name}: {e}")
        return False

def process_stream(s3_client, stream_id: str):
    stream_dir = STREAMS_ROOT / stream_id
    if not stream_dir.exists():
        return 0

    segs = sorted(stream_dir.glob("seg_*.ts"), key=lambda f: f.name)
    if len(segs) <= HLS_KEEP:
        return 0

    to_upload = segs[:-HLS_KEEP]
    uploaded = 0
    for seg in to_upload:
        if upload_segment(s3_client, seg, stream_id):
            uploaded += 1

    if uploaded:
        log.info(f"[{stream_id}] {uploaded} segmentos subidos")
    return uploaded


def run():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tv_streams = get_tv_streams()
    log.info(f"Video uploader iniciado — TV streams: {tv_streams}")
    s3_client = get_s3()

    while True:
        for stream_id in tv_streams:
            try:
                process_stream(s3_client, stream_id)
            except Exception as e:
                log.error(f"[{stream_id}] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
