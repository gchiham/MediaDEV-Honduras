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
STATIONS       = Path(os.environ.get("STATIONS_JSON", "/opt/media-ai/config/stations.json"))
S3_BUCKET      = os.environ.get("S3_BUCKET",  "mediadev-recordings")
S3_REGION      = os.environ.get("S3_REGION",  "us-east-1")
S3_PREFIX      = "video_segments"
AUDIO_DIR      = Path(os.environ.get("TV_AUDIO_DIR", "/var/www/streams/_tv_audio"))
INVALID_DIR    = Path(os.environ.get("INVALID_SEGMENT_DIR", "/var/www/streams/_invalid"))
HLS_KEEP       = int(os.environ.get("HLS_KEEP", "12"))
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", "15"))
SEGMENT_DUR    = int(os.environ.get("SEGMENT_DUR", "4"))
S3_UPLOAD_RETRIES = int(os.environ.get("S3_UPLOAD_RETRIES", "3"))
VIDEO_VALIDATE_FFPROBE = os.environ.get("VIDEO_VALIDATE_FFPROBE", "1") != "0"
MIN_VIDEO_SECONDS = float(os.environ.get("MIN_VIDEO_SECONDS", "1.0"))
MIN_AUDIO_SECONDS = int(os.environ.get("MIN_AUDIO_SECONDS", "60"))
FULL_HOUR_MIN_SECONDS = int(os.environ.get("FULL_HOUR_MIN_SECONDS", "3300"))
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
_video_coverage: dict = {}


def get_tv_streams() -> list:
    try:
        data = json.load(open(STATIONS))
        return [s["id"] for s in data["stations"]
                if s.get("type") == "tv" and s.get("enabled", True)]
    except Exception as e:
        log.warning(f"No se pudo leer stations.json: {e}")
        return []

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

def _manifest_s3_key(stream_id: str, h_epoch: int) -> str:
    dt = datetime.fromtimestamp(h_epoch, tz=timezone.utc)
    return f"{stream_id}/{dt.year}/{dt.month:02d}/{_hour_label(h_epoch)}.manifest.json"

def _seg_real_duration(path: Path) -> float:
    """Duracion real del segmento (ffprobe), NO el SEGMENT_DUR nominal fijo.
    Los segmentos HLS de origen rara vez duran EXACTAMENTE 4s (keyframe-aligned,
    +-0.5s tipico); usar el nominal fijo para construir el manifiesto de tiempo
    real reintroduce el mismo error que este manifiesto existe para eliminar."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return float(SEGMENT_DUR)


def _audio_s3_key(stream_id: str, h_epoch: int) -> str:
    dt = datetime.fromtimestamp(h_epoch, tz=timezone.utc)
    return f"{stream_id}/{dt.year}/{dt.month:02d}/{_hour_label(h_epoch)}.ts"

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

def _pg_write(sql: str, values: list | tuple) -> None:
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            dbname=PG_DB, user=PG_USER, password=PG_PASS,
            connect_timeout=5, sslmode="require",
        )
        with conn.cursor() as cur:
            cur.execute(sql, values)
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[pg] coverage write error: {e}")

def _coverage_table_exists() -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            dbname=PG_DB, user=PG_USER, password=PG_PASS,
            connect_timeout=5, sslmode="require",
        )
        exists = bool(_table_columns(conn, "recording_coverage"))
        conn.close()
        return exists
    except Exception:
        return False

def _coverage_upsert_audio(
    stream_id: str,
    hour_epoch: int,
    status: str,
    *,
    actual_seconds: float | None = None,
    local_path: Path | None = None,
    s3_key_value: str | None = None,
    reason: str | None = None,
    size_bytes: int | None = None,
    upload_attempts: int = 0,
    last_error: str | None = None,
) -> None:
    if not _coverage_table_exists():
        return
    start = datetime.fromtimestamp(hour_epoch, tz=timezone.utc)
    end = start + timedelta(hours=1)
    _pg_write(
        """
        INSERT INTO recording_coverage
          (stream_id, media_type, period_start_utc, period_end_utc,
           expected_seconds, actual_seconds, local_path, s3_key, status, reason,
           size_bytes, upload_attempts, last_error, source_service,
           pipeline_version, updated_at)
        VALUES (%s,'audio',%s,%s,3600,%s,%s,%s,%s,%s,%s,%s,%s,
                'video-segment-uploader',%s,NOW())
        ON CONFLICT (stream_id, media_type, period_start_utc) DO UPDATE SET
          period_end_utc=EXCLUDED.period_end_utc,
          actual_seconds=EXCLUDED.actual_seconds,
          local_path=EXCLUDED.local_path,
          s3_key=EXCLUDED.s3_key,
          status=EXCLUDED.status,
          reason=EXCLUDED.reason,
          size_bytes=EXCLUDED.size_bytes,
          upload_attempts=recording_coverage.upload_attempts + EXCLUDED.upload_attempts,
          last_error=EXCLUDED.last_error,
          source_service=EXCLUDED.source_service,
          pipeline_version=EXCLUDED.pipeline_version,
          updated_at=NOW()
        """,
        (
            stream_id, start, end, actual_seconds,
            str(local_path) if local_path else None, s3_key_value, status,
            reason, size_bytes, upload_attempts, last_error, PIPELINE_VERSION,
        ),
    )

def _status_priority(status: str) -> int:
    return {"uploaded": 1, "upload_failed": 2, "invalid": 3}.get(status, 0)

def _video_coverage_add(
    stream_id: str,
    epoch_start: int,
    status: str,
    *,
    seconds: float = 0.0,
    size_bytes: int = 0,
    upload_attempts: int = 0,
    reason: str | None = None,
    last_error: str | None = None,
) -> None:
    hour_epoch = (epoch_start // 3600) * 3600
    key = (stream_id, hour_epoch)
    cur = _video_coverage.get(key)
    if cur is None:
        cur = {
            "actual_seconds": 0.0,
            "segments": 0,
            "size_bytes": 0,
            "upload_attempts": 0,
            "status": "uploaded",
            "reason": None,
            "last_error": None,
        }
        _video_coverage[key] = cur

    cur["actual_seconds"] += seconds if status == "uploaded" else 0
    cur["segments"] += 1 if status == "uploaded" else 0
    cur["size_bytes"] += size_bytes if status == "uploaded" else 0
    cur["upload_attempts"] += upload_attempts
    if _status_priority(status) > _status_priority(cur["status"]):
        cur["status"] = status
    if reason:
        cur["reason"] = reason
    if last_error:
        cur["last_error"] = last_error

def _video_coverage_flush(stream_id: str | None = None) -> None:
    if not _video_coverage or not _coverage_table_exists():
        return

    current_hour = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
    keys = list(_video_coverage.keys())
    for key in keys:
        sid, hour_epoch = key
        if stream_id is not None and sid != stream_id:
            continue
        cur = _video_coverage[key]
        start = datetime.fromtimestamp(hour_epoch, tz=timezone.utc)
        end = start + timedelta(hours=1)
        _pg_write(
            """
            INSERT INTO recording_coverage
              (stream_id, media_type, period_start_utc, period_end_utc,
               expected_seconds, actual_seconds, status, reason, size_bytes,
               upload_attempts, last_error, source_service, pipeline_version, updated_at)
            VALUES (%s,'video',%s,%s,3600,%s,%s,%s,%s,%s,%s,
                    'video-segment-uploader',%s,NOW())
            ON CONFLICT (stream_id, media_type, period_start_utc) DO UPDATE SET
              actual_seconds=GREATEST(COALESCE(recording_coverage.actual_seconds,0), EXCLUDED.actual_seconds),
              status=CASE
                WHEN recording_coverage.status IN ('invalid','upload_failed') THEN recording_coverage.status
                ELSE EXCLUDED.status
              END,
              reason=COALESCE(EXCLUDED.reason, recording_coverage.reason),
              size_bytes=GREATEST(COALESCE(recording_coverage.size_bytes,0), EXCLUDED.size_bytes),
              upload_attempts=GREATEST(recording_coverage.upload_attempts, EXCLUDED.upload_attempts),
              last_error=COALESCE(EXCLUDED.last_error, recording_coverage.last_error),
              source_service=EXCLUDED.source_service,
              pipeline_version=EXCLUDED.pipeline_version,
              updated_at=NOW()
            """,
            (
                sid, start, end, cur["actual_seconds"], cur["status"],
                cur["reason"], cur["size_bytes"], cur["upload_attempts"],
                cur["last_error"], PIPELINE_VERSION,
            ),
        )
        _legacy_video_coverage_upsert(sid, start, cur["segments"], cur["size_bytes"])
        if hour_epoch < current_hour:
            _video_coverage.pop(key, None)

def _legacy_video_coverage_upsert(
    stream_id: str,
    hour_utc: datetime,
    segments: int,
    size_bytes: int,
) -> None:
    if segments <= 0:
        return
    _pg_write(
        """
        INSERT INTO mediadev_video_coverage (stream, hour_utc, segs, bytes, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (stream, hour_utc) DO UPDATE SET
          segs = GREATEST(mediadev_video_coverage.segs, EXCLUDED.segs),
          bytes = GREATEST(mediadev_video_coverage.bytes, EXCLUDED.bytes),
          updated_at = NOW()
        """,
        (stream_id, hour_utc, segments, size_bytes),
    )

def ffprobe_duration(path: Path, selector: str) -> tuple[float | None, str | None]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", selector,
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, (result.stderr or "ffprobe failed")[-300:]
    try:
        return float(result.stdout.strip()), None
    except ValueError:
        return None, f"duration parse failed: {result.stdout.strip()}"

def has_stream(path: Path, selector: str) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", selector,
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())

def validate_video_segment(seg_path: Path) -> tuple[bool, float, str | None]:
    if not seg_path.exists() or seg_path.stat().st_size < 1024:
        return False, 0.0, "missing_or_tiny"
    if not VIDEO_VALIDATE_FFPROBE:
        return True, float(SEGMENT_DUR), None
    if not has_stream(seg_path, "v:0"):
        return False, 0.0, "no_video_stream"
    duration, err = ffprobe_duration(seg_path, "v:0")
    if duration is None:
        return False, 0.0, err or "no_video_duration"
    if duration < MIN_VIDEO_SECONDS:
        return False, duration, f"too_short_{duration:.1f}s"
    return True, duration, None

def validate_audio_file(mp3_path: Path) -> tuple[bool, float | None, str | None]:
    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        return False, None, "missing_or_empty"
    duration, err = ffprobe_duration(mp3_path, "a:0")
    if duration is None:
        return False, None, err or "no_audio_duration"
    if duration < MIN_AUDIO_SECONDS:
        return False, duration, f"too_short_{int(duration)}s"
    if duration < FULL_HOUR_MIN_SECONDS:
        return True, duration, f"partial_{int(duration)}s"
    return True, duration, None

def upload_file_verified(s3_client, local_path: Path, key: str, content_type: str) -> tuple[bool, str | None]:
    size = local_path.stat().st_size
    last_error = None
    for attempt in range(1, S3_UPLOAD_RETRIES + 1):
        try:
            s3_client.upload_file(str(local_path), S3_BUCKET, key,
                                  ExtraArgs={"ContentType": content_type})
            head = s3_client.head_object(Bucket=S3_BUCKET, Key=key)
            if int(head.get("ContentLength", -1)) != size:
                raise RuntimeError(f"size mismatch local={size} s3={head.get('ContentLength')}")
            return True, None
        except Exception as e:
            last_error = str(e)
            time.sleep(min(2 ** attempt, 15))
    return False, last_error

def concat_file_line(path: Path) -> str:
    safe = str(path.resolve()).replace("'", "'\\''")
    return f"file '{safe}'"

def parse_hour_label(label: str) -> int | None:
    for fmt, tz in (("%Y-%m-%dT%HZ", timezone.utc), ("%Y-%m-%d_%Hh", TGU)):
        try:
            dt = datetime.strptime(label, fmt).replace(tzinfo=tz)
            return int(dt.astimezone(timezone.utc).timestamp())
        except ValueError:
            continue
    return None

def flush_audio_hour(s3_client, stream_id: str, hour_epoch: int, segs_dir: Path) -> None:
    """Concatena mini-segs de audio acumulados, sube TS raw a S3 para que Destroyer encode."""
    segs    = sorted(segs_dir.glob("*.ts"))
    h_label = _hour_label(hour_epoch)
    rec_day = datetime.fromtimestamp(hour_epoch, tz=timezone.utc).strftime("%Y-%m-%d")

    if len(segs) < 10:
        log.warning(f"[{stream_id}] audio flush {h_label}: {len(segs)} segs — omitiendo")
        _coverage_upsert_audio(
            stream_id, hour_epoch, "skipped",
            actual_seconds=len(segs) * SEGMENT_DUR,
            local_path=segs_dir,
            reason=f"insufficient_segments_{len(segs)}",
        )
        shutil.rmtree(segs_dir, ignore_errors=True)
        return

    ts_path    = segs_dir / f"{h_label}.ts"
    concat_txt = segs_dir / "list.txt"
    concat_txt.write_text("\n".join(concat_file_line(p) for p in segs) + "\n")

    # Manifiesto: mapeo (posicion acumulada real en el .ts concatenado -> epoch real
    # de inicio de ESE segmento). Corrige el drift que se acumula cuando el calculo
    # ingenuo hour_start_utc + ts_seconds asume que cada segundo de audio grabado
    # equivale a un segundo de reloj real -- eso se rompe cuando faltan segmentos
    # (reconexiones) y el tiempo perdido se salta silenciosamente del concat.
    # Ver CHANGES.log (fix drift audio/video TV, 15 jul 2026) para el analisis
    # que valido este mecanismo con ~2-3% de error contra casos reales.
    manifest = []
    cum = 0.0
    for p in segs:
        try:
            epoch_start = int(p.stem)
        except ValueError:
            epoch_start = None
        dur = _seg_real_duration(p)
        manifest.append({"cum_start": round(cum, 3), "epoch_start": epoch_start, "duration": round(dur, 3)})
        cum += dur
    _coverage_upsert_audio(
        stream_id, hour_epoch, "pending",
        actual_seconds=len(segs) * SEGMENT_DUR,
        local_path=ts_path,
        reason="building_ts",
    )

    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(concat_txt),
         "-c", "copy", str(ts_path)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        log.error(f"[{stream_id}] audio flush ffmpeg error: {r.stderr[-300:]}")
        _coverage_upsert_audio(
            stream_id, hour_epoch, "invalid",
            actual_seconds=len(segs) * SEGMENT_DUR,
            local_path=ts_path,
            reason="ffmpeg_failed",
            last_error=r.stderr[-300:],
        )
        return

    key = _audio_s3_key(stream_id, hour_epoch)
    hour_start_utc = datetime.fromtimestamp(hour_epoch, tz=timezone.utc)
    size = ts_path.stat().st_size if ts_path.exists() else 0
    actual_secs = len(segs) * SEGMENT_DUR

    ok, err = upload_file_verified(s3_client, ts_path, key, "video/mp2t")
    if not ok:
        log.error(f"[{stream_id}] audio upload error: {err}")
        _coverage_upsert_audio(
            stream_id, hour_epoch, "upload_failed",
            actual_seconds=actual_secs,
            local_path=ts_path,
            s3_key_value=key,
            reason="upload_failed",
            size_bytes=size,
            upload_attempts=S3_UPLOAD_RETRIES,
            last_error=err,
        )
        return

    log.info(f"[{stream_id}] {h_label}.ts → s3://{S3_BUCKET}/{key}  ({len(segs)} segs, {size//1024//1024}MB raw)")
    try:
        import json as _json
        manifest_key = _manifest_s3_key(stream_id, hour_epoch)
        manifest_path = segs_dir / "manifest.json"
        manifest_path.write_text(_json.dumps(manifest))
        s3_client.upload_file(str(manifest_path), S3_BUCKET, manifest_key,
                               ExtraArgs={"ContentType": "application/json"})
        log.info(f"[{stream_id}] manifest -> s3://{S3_BUCKET}/{manifest_key} ({len(manifest)} entradas)")
    except Exception as e:
        log.warning(f"[{stream_id}] manifest upload fallo (no bloqueante): {e}")
    _db_register(key, stream_id, rec_day, hour_start_utc)
    _coverage_upsert_audio(
        stream_id, hour_epoch, "uploaded",
        actual_seconds=actual_secs,
        local_path=ts_path,
        s3_key_value=key,
        reason=None,
        size_bytes=size,
        upload_attempts=1,
    )
    shutil.rmtree(segs_dir, ignore_errors=True)

def recover_stale_audio_dirs(s3_client, tv_streams: list[str]) -> None:
    current_hour = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
    for stream_id in tv_streams:
        root = AUDIO_DIR / stream_id
        if not root.exists():
            continue
        for hour_dir in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name):
            hour_epoch = parse_hour_label(hour_dir.name)
            if hour_epoch is None or hour_epoch >= current_hour:
                continue
            flush_audio_hour(s3_client, stream_id, hour_epoch, hour_dir)


def save_audio_seg(s3_client, stream_id: str, seg_path: Path, epoch_start: int) -> bool:
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
        return True

    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(seg_path), "-vn", "-c:a", "copy", str(audio_out)],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not audio_out.exists():
        log.warning(f"[{stream_id}] audio extract error {seg_path.name}: {(result.stderr or '')[-200:]}")
        return False
    return True


# ── Video upload ──────────────────────────────────────────────────────────────

def quarantine_segment(seg_path: Path, stream_id: str) -> None:
    target_dir = INVALID_DIR / stream_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / seg_path.name
    try:
        if target.exists():
            target.unlink()
        shutil.move(str(seg_path), str(target))
    except Exception as e:
        log.warning(f"[{stream_id}] no se pudo mover segmento inválido {seg_path.name}: {e}")

def upload_segment(s3_client, seg_path: Path, stream_id: str) -> bool:
    mtime       = int(seg_path.stat().st_mtime)
    epoch_start = mtime - SEGMENT_DUR
    epoch_end   = mtime
    key = s3_key(stream_id, epoch_start, epoch_end)
    valid, duration, reason = validate_video_segment(seg_path)
    size = seg_path.stat().st_size if seg_path.exists() else 0

    if not valid:
        log.error(f"[{stream_id}] segmento inválido {seg_path.name}: {reason}")
        _video_coverage_add(
            stream_id, epoch_start, "invalid",
            seconds=0, size_bytes=0, reason=reason, last_error=reason,
        )
        quarantine_segment(seg_path, stream_id)
        return False

    try:
        # Extraer audio ANTES de borrar el .ts de video
        save_audio_seg(s3_client, stream_id, seg_path, epoch_start)
        ok, err = upload_file_verified(s3_client, seg_path, key, "video/mp2t")
        if not ok:
            raise RuntimeError(err or "upload failed")
        _video_coverage_add(
            stream_id, epoch_start, "uploaded",
            seconds=duration or SEGMENT_DUR,
            size_bytes=size,
            upload_attempts=1,
            reason=reason,
        )
        seg_path.unlink()
        return True
    except Exception as e:
        log.error(f"[{stream_id}] Error subiendo {seg_path.name}: {e}")
        _video_coverage_add(
            stream_id, epoch_start, "upload_failed",
            seconds=0,
            size_bytes=0,
            upload_attempts=S3_UPLOAD_RETRIES,
            reason=reason,
            last_error=str(e),
        )
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

    _video_coverage_flush(stream_id)
    if uploaded:
        log.info(f"[{stream_id}] {uploaded} segmentos subidos")
    return uploaded


def run():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    INVALID_DIR.mkdir(parents=True, exist_ok=True)
    tv_streams = get_tv_streams()
    log.info(f"Video uploader iniciado — TV streams: {tv_streams}")
    s3_client = get_s3()
    recover_stale_audio_dirs(s3_client, tv_streams)

    while True:
        refreshed = get_tv_streams()
        if refreshed and refreshed != tv_streams:
            tv_streams = refreshed
            log.info(f"TV streams actualizados: {tv_streams}")
            recover_stale_audio_dirs(s3_client, tv_streams)
        for stream_id in tv_streams:
            try:
                process_stream(s3_client, stream_id)
            except Exception as e:
                log.error(f"[{stream_id}] {e}")
        _video_coverage_flush()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
