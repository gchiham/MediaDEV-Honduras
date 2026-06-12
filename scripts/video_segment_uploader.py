#!/usr/bin/env python3
"""
Video Segment Uploader — sube .ts de streams TV a S3 con nombre de epoch.
Usa mtime del archivo para calcular el epoch (sin SQLite).

S3 path: video_segments/{stream_id}/{YYYY}/{MM}/{DD}/{epoch_start}_{epoch_end}.ts
"""
import os, time, json, logging, boto3
from pathlib import Path
from datetime import datetime, timezone

STREAMS_ROOT   = Path(os.environ.get("STREAMS_ROOT", "/var/www/streams"))
STATIONS       = Path("/opt/media-ai/config/stations.json")
S3_BUCKET      = os.environ.get("S3_BUCKET",  "mediadev-recordings")
S3_REGION      = os.environ.get("S3_REGION",  "us-east-1")
S3_PREFIX      = "video_segments"
HLS_KEEP       = 12
SCAN_INTERVAL  = 15
SEGMENT_DUR    = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/streams/video_uploader.log"),
    ]
)
log = logging.getLogger("video-uploader")

def get_tv_streams() -> list:
    data = json.load(open(STATIONS))
    return [s["id"] for s in data["stations"]
            if s.get("type") == "tv" and s.get("enabled", True)]

def get_s3():
    return boto3.client("s3", region_name=S3_REGION)

def s3_key(stream_id: str, epoch_start: int, epoch_end: int) -> str:
    dt = datetime.fromtimestamp(epoch_start, tz=timezone.utc)
    return f"{S3_PREFIX}/{stream_id}/{dt.strftime('%Y')}/{dt.strftime('%m')}/{dt.strftime('%d')}/{epoch_start}_{epoch_end}.ts"

def upload_segment(s3, seg_path: Path, stream_id: str) -> bool:
    mtime       = int(seg_path.stat().st_mtime)
    epoch_start = mtime - SEGMENT_DUR
    epoch_end   = mtime
    key = s3_key(stream_id, epoch_start, epoch_end)
    try:
        s3.upload_file(str(seg_path), S3_BUCKET, key,
                       ExtraArgs={"ContentType": "video/mp2t"})
        seg_path.unlink()
        return True
    except Exception as e:
        log.error(f"[{stream_id}] Error subiendo {seg_path.name}: {e}")
        return False

def process_stream(s3, stream_id: str):
    stream_dir = STREAMS_ROOT / stream_id
    if not stream_dir.exists():
        return 0

    segs = sorted(stream_dir.glob("seg_*.ts"), key=lambda f: f.name)
    if len(segs) <= HLS_KEEP:
        return 0

    to_upload = segs[:-HLS_KEEP]
    uploaded = 0
    for seg in to_upload:
        if upload_segment(s3, seg, stream_id):
            uploaded += 1

    if uploaded:
        log.info(f"[{stream_id}] {uploaded} segmentos subidos")
    return uploaded

def run():
    tv_streams = get_tv_streams()
    log.info(f"Video uploader iniciado — TV streams: {tv_streams}")
    s3 = get_s3()

    while True:
        for stream_id in tv_streams:
            try:
                process_stream(s3, stream_id)
            except Exception as e:
                log.error(f"[{stream_id}] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
