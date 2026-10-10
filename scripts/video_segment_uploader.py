#!/usr/bin/env python3
"""
Video Segment Uploader — sube .ts de streams TV a S3 con nombre de epoch.
Usa mtime del archivo para calcular el epoch (sin SQLite).

S3 path: video_segments/{stream_id}/{YYYY}/{MM}/{DD}/{epoch_start}_{epoch_end}.ts

Además arma el audio por hora para el Destroyer:
  s3://{bucket}/{stream_id}/{YYYY}/{MM}/{YYYY-MM-DDTHHZ}.ts (+ .manifest.json)

Audio: se extrae de cada segmento a _tv_audio/<sid>/<hora>/ (local); al cerrar la hora se sella
en la bandeja _tv_audio/<sid>/.outbox/<hora>/ (atómico) y un hilo aparte la sube a S3 con
reintentos. La vuelta principal nunca espera a S3 por el audio.
`--drain-outbox`: sube lo pendiente de la bandeja una vez y sale (rollback/recuperación).
"""
import os, sys, time, json, logging, queue, shutil, subprocess, threading, boto3
from botocore.config import Config
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
# Tope de cordura del concat: con -c copy la salida pesa aprox lo mismo que la suma
# de sus entradas. Ver build_audio_hour() y CHANGES.log (incidente 26 ago 2026).
CONCAT_MAX_RATIO = float(os.environ.get("CONCAT_MAX_RATIO", "3"))
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

_LOG_FILE = os.environ.get("UPLOADER_LOG", "/var/log/streams/video_uploader.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
    + ([logging.FileHandler(_LOG_FILE)] if os.path.isdir(os.path.dirname(_LOG_FILE)) else []),
)
log = logging.getLogger("video-uploader")

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

# Timeouts acotados: un S3 lento (no caído) no debe colgar una vuelta ni el hilo de subida.
_S3_CONFIG = Config(
    connect_timeout=int(os.environ.get("S3_CONNECT_TIMEOUT", "5")),
    read_timeout=int(os.environ.get("S3_READ_TIMEOUT", "60")),
    retries={"max_attempts": 2, "mode": "standard"},
)


def get_s3():
    return boto3.client("s3", region_name=S3_REGION, config=_S3_CONFIG)

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
    d = _probe_duration(path)
    return float(SEGMENT_DUR) if d is None else d


def _probe_duration(path: Path) -> float | None:
    """ffprobe format=duration; None si falla (no se inventa un valor)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def _cached_duration(path: Path) -> float:
    """Duración guardada al extraer (<epoch>.dur, mismo ffprobe); si falta o es ilegible,
    ffprobe ahora. Así el armado de la hora no lanza ~900 ffprobe seguidos (~3 min con la
    vuelta del uploader detenida). El .dur no entra al concat: el glob toma solo *.ts."""
    try:
        return float(path.with_suffix(".dur").read_text())
    except (OSError, ValueError):
        return _seg_real_duration(path)


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

def upload_file_verified(s3_client, local_path: Path, key: str, content_type: str,
                         attempts: int | None = None) -> tuple[bool, str | None]:
    size = local_path.stat().st_size
    last_error = None
    attempts = attempts or S3_UPLOAD_RETRIES
    for attempt in range(1, attempts + 1):
        try:
            s3_client.upload_file(str(local_path), S3_BUCKET, key,
                                  ExtraArgs={"ContentType": content_type})
            head = s3_client.head_object(Bucket=S3_BUCKET, Key=key)
            if int(head.get("ContentLength", -1)) != size:
                raise RuntimeError(f"size mismatch local={size} s3={head.get('ContentLength')}")
            return True, None
        except Exception as e:
            last_error = str(e)
            if attempt < attempts:          # antes dormía también tras el último intento
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

def _ffmpeg_concat(list_txt: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_txt),
         "-c", "copy", str(out)],
        capture_output=True, text=True)


def build_audio_hour(stream_id: str, hour_epoch: int, segs_dir: Path, out_dir: Path) -> tuple[str, dict]:
    """Arma la hora de audio SOLO en local (sin S3): <hora>.ts + manifest.json + meta.json en
    out_dir. meta.json se escribe al final: su presencia es la marca de "completo".
    Devuelve ('sealed'|'skipped'|'invalid', meta)."""
    # Solo segmentos legitimos: se llaman {epoch:010d}.ts. Excluye explicitamente el
    # <hora>.ts de salida y los .part.ts: incluirlos realimenta el concat (incidente
    # 26 ago 2026: 126MB -> 41.8GB, disco lleno). Ver CHANGES.log.
    segs    = sorted(q for q in segs_dir.glob("*.ts") if q.stem.isdigit())
    h_label = _hour_label(hour_epoch)

    if len(segs) < 10:
        log.warning(f"[{stream_id}] audio {h_label}: {len(segs)} segs — omitiendo")
        _coverage_upsert_audio(
            stream_id, hour_epoch, "skipped",
            actual_seconds=len(segs) * SEGMENT_DUR,
            local_path=segs_dir,
            reason=f"insufficient_segments_{len(segs)}",
        )
        return "skipped", {"segments": len(segs)}

    out_dir.mkdir(parents=True, exist_ok=True)
    ts_path    = out_dir / f"{h_label}.ts"
    concat_txt = out_dir / "list.txt"
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
        dur = _cached_duration(p)
        manifest.append({"cum_start": round(cum, 3), "epoch_start": epoch_start, "duration": round(dur, 3)})
        cum += dur
    _coverage_upsert_audio(
        stream_id, hour_epoch, "pending",
        actual_seconds=len(segs) * SEGMENT_DUR,
        local_path=ts_path,
        reason="building_ts",
    )

    r = _ffmpeg_concat(concat_txt, ts_path)
    if r.returncode != 0:
        log.error(f"[{stream_id}] audio {h_label} ffmpeg error: {(r.stderr or '')[-300:]}")
        _coverage_upsert_audio(
            stream_id, hour_epoch, "invalid",
            actual_seconds=len(segs) * SEGMENT_DUR,
            local_path=ts_path,
            reason="ffmpeg_failed",
            last_error=(r.stderr or "")[-300:],
        )
        return "invalid", {}

    # Guarda de cordura: con -c copy la salida debe pesar aproximadamente lo mismo que la
    # suma de sus entradas. Un desborde grosero significa que el concat se realimento (el
    # 26 ago 2026: 126 MB de segmentos produjeron 41.8 GB y llenaron el disco del nodo) o
    # que el muxer se descarrilo. Abortar aqui cuesta una hora de audio de una estacion;
    # no abortar costo la grabacion de las 7 estaciones de TV durante medio dia.
    entrada = sum(q.stat().st_size for q in segs if q.exists())
    salida  = ts_path.stat().st_size if ts_path.exists() else 0
    if entrada and salida > CONCAT_MAX_RATIO * entrada:
        ratio = salida / entrada
        log.error(f"[{stream_id}] audio {h_label} ABORTADO: salida {salida // 1048576}MB "
                  f"vs entrada {entrada // 1048576}MB (ratio {ratio:.1f}x, tope "
                  f"{CONCAT_MAX_RATIO}x) - posible concat realimentado")
        ts_path.unlink(missing_ok=True)
        _coverage_upsert_audio(
            stream_id, hour_epoch, "invalid",
            actual_seconds=len(segs) * SEGMENT_DUR,
            local_path=ts_path,
            reason="concat_runaway",
            last_error=f"ratio={ratio:.1f}x entrada={entrada} salida={salida}",
        )
        return "invalid", {}
    if not salida:
        _coverage_upsert_audio(stream_id, hour_epoch, "invalid", local_path=ts_path,
                               reason="empty_output")
        return "invalid", {}

    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    concat_txt.unlink(missing_ok=True)
    meta = {
        "stream_id": stream_id, "hour_epoch": hour_epoch, "label": h_label,
        "segments": len(segs), "actual_seconds": len(segs) * SEGMENT_DUR,
        "size": salida, "manifest_size": (out_dir / "manifest.json").stat().st_size,
        "sealed_at": _now(), "attempts": 0,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta))     # último: marca de completo
    return "sealed", meta

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

# ── v2 (oct 2026): bucle sin bloqueo entre canales ────────────────────────────
# Antes (v1), cada segmento se procesaba "extraer audio -> subir (3 intentos con sleep) ->
# borrar" y el canal se recorría entero antes de pasar al siguiente. Con S3 caído, el primer
# canal con backlog acaparaba el bucle (~14 s por segmento) y los demás canales ni siquiera
# extraían su audio. Ahora cada vuelta hace, en orden:
#   1. extracción de audio de TODOS los canales (local, no depende de S3)
#   2. flush de horas de audio completas (solo si S3 está disponible, con presupuesto)
#   3. subida de video round-robin entre canales, 1 intento por segmento, sin sleep
#   4. descarte de TV por emergencia de disco (TV_SHED_MODE: off | observe | enforce)
# Un circuit breaker pausa TODAS las subidas tras un fallo (backoff 60 s -> 600 s); audio y
# video quedan en disco y se suben al volver S3.
GB = 1024 ** 3
S3_BACKOFF_MIN = int(os.environ.get("S3_BACKOFF_MIN", "60"))
S3_BACKOFF_MAX = int(os.environ.get("S3_BACKOFF_MAX", "600"))
UPLOAD_MAX_PER_CHANNEL = int(os.environ.get("UPLOAD_MAX_PER_CHANNEL", "40"))   # por vuelta
EXTRACT_MAX_PER_LOOP = int(os.environ.get("EXTRACT_MAX_PER_LOOP", "300"))      # por canal y vuelta
UPLOAD_TIME_BUDGET_S = float(os.environ.get("UPLOAD_TIME_BUDGET_S", "20"))     # tope de la subida de video por vuelta
AUDIO_FLUSH_GRACE = int(os.environ.get("AUDIO_FLUSH_GRACE", "120"))            # s tras el fin de la hora
AUDIO_EXTRACT_MAX_FAILS = int(os.environ.get("AUDIO_EXTRACT_MAX_FAILS", "3"))
FLUSHED_MARKER_KEEP_H = int(os.environ.get("FLUSHED_MARKER_KEEP_H", "48"))
TV_SHED_MODE = os.environ.get("TV_SHED_MODE", "observe").strip().lower()      # off | observe | enforce
EMERGENCY_FREE_GB = float(os.environ.get("EMERGENCY_FREE_GB", "15"))
TARGET_FREE_GB = float(os.environ.get("TARGET_FREE_GB", "20"))
TV_SHED_MIN_AGE_MIN = float(os.environ.get("TV_SHED_MIN_AGE_MIN", "30"))
STALE_WRITER_S = int(os.environ.get("STALE_WRITER_S", "120"))   # segmento más nuevo sin cambios = canal detenido

_s3_down_until = 0.0
_s3_backoff = S3_BACKOFF_MIN
_extract_fails: dict[str, int] = {}
_loop_n = 0
_last_marker_gc = 0.0


def _now() -> float:
    return time.time()


def s3_available() -> bool:
    return _now() >= _s3_down_until


def s3_failed(err: str | None) -> None:
    global _s3_down_until, _s3_backoff
    _s3_down_until = _now() + _s3_backoff
    log.warning(f"S3 no disponible ({(err or '')[:200]}); subidas en pausa {_s3_backoff}s "
                "(audio y video siguen en disco)")
    _s3_backoff = min(_s3_backoff * 2, S3_BACKOFF_MAX)


def s3_ok() -> None:
    global _s3_backoff
    _s3_backoff = S3_BACKOFF_MIN


def _seg_index(p: Path) -> int:
    try:
        return int(p.stem[4:])
    except ValueError:
        return -1


def _seg_epoch(seg: Path) -> int | None:
    try:
        return int(seg.stat().st_mtime) - SEGMENT_DUR
    except FileNotFoundError:
        return None


def _hour_of(epoch: int) -> int:
    return (epoch // 3600) * 3600


def _audio_seg_path(stream_id: str, epoch: int) -> Path:
    return AUDIO_DIR / stream_id / _hour_label(_hour_of(epoch)) / f"{epoch:010d}.ts"


def _flushed_marker(stream_id: str, hour_epoch: int) -> Path:
    return AUDIO_DIR / stream_id / ".flushed" / _hour_label(hour_epoch)


def _mark_flushed(stream_id: str, hour_epoch: int, status: str) -> None:
    m = _flushed_marker(stream_id, hour_epoch)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(status)


def audio_safe(stream_id: str, seg: Path, epoch: int) -> bool:
    """True si el audio de este segmento ya está a salvo (extraído localmente, o su hora ya se
    cerró), o si no tiene audio recuperable (extracción falló AUDIO_EXTRACT_MAX_FAILS veces).
    Solo un segmento audio_safe puede subirse-y-borrarse o descartarse."""
    return (_audio_seg_path(stream_id, epoch).exists()
            or _flushed_marker(stream_id, _hour_of(epoch)).exists()
            or _extract_fails.get(str(seg), 0) >= AUDIO_EXTRACT_MAX_FAILS)


def _ffmpeg_extract(src: Path, dst: Path) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-vn", "-c:a", "copy", "-f", "mpegts", str(dst)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning(f"audio extract error {src.name}: {(r.stderr or '')[-200:]}")
    return r.returncode == 0


def extract_audio(stream_id: str, seg: Path, epoch: int) -> bool:
    """Extrae el audio del segmento a _tv_audio/<sid>/<hora>/<epoch>.ts. Idempotente y atómica
    (escribe .part y renombra): un corte a mitad no deja un archivo que parezca completo."""
    if _flushed_marker(stream_id, _hour_of(epoch)).exists() or _is_sealing(stream_id, _hour_of(epoch)):
        return True                                   # hora cerrada o en sellado: no recrear su dir
    out = _audio_seg_path(stream_id, epoch)
    if out.exists():
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.part.ts")      # stem no numérico: el flush lo ignora
    ok = _ffmpeg_extract(seg, tmp) and tmp.exists() and tmp.stat().st_size > 0
    if ok:
        os.replace(tmp, out)
        _extract_fails.pop(str(seg), None)
        dur = _probe_duration(out)                  # se mide ahora, repartido en la hora
        if dur is not None:
            dtmp = out.with_name(f"{out.stem}.dur.tmp")
            dtmp.write_text(f"{dur:.6f}")
            os.replace(dtmp, out.with_suffix(".dur"))
        return True
    tmp.unlink(missing_ok=True)
    _extract_fails[str(seg)] = _extract_fails.get(str(seg), 0) + 1
    return False


def _s3_size(s3_client, key: str) -> int | None:
    """Tamaño del objeto, None si no existe. Cualquier otro error se propaga."""
    try:
        return int(s3_client.head_object(Bucket=S3_BUCKET, Key=key).get("ContentLength", 0))
    except Exception as e:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _outbox_root(stream_id: str) -> Path:
    return AUDIO_DIR / stream_id / ".outbox"


def seal_hour(stream_id: str, hour_epoch: int, segs_dir: Path) -> str:
    """Sella la hora en la bandeja de salida local (sin S3). Atómico: se arma en
    .outbox/.tmp-<hora>/ y se renombra a .outbox/<hora>/ con meta.json ya escrito.
    Tras sellar, marcador 'sealed' (el audio está a salvo en disco) y se borran los
    mini-segmentos de la hora. Idempotente ante cortes en cualquier punto."""
    label = _hour_label(hour_epoch)
    root = _outbox_root(stream_id)
    final = root / label
    if (final / "meta.json").exists():
        # Corte entre el rename y el marcador: ya está sellada, solo completar.
        _mark_flushed(stream_id, hour_epoch, "sealed")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return "sealed"
    tmp = root / f".tmp-{label}"
    shutil.rmtree(tmp, ignore_errors=True)          # resto de un sellado interrumpido
    status, meta = build_audio_hour(stream_id, hour_epoch, segs_dir, tmp)
    if status == "sealed":
        shutil.rmtree(final, ignore_errors=True)     # dir sin meta.json = basura, nunca se sube
        os.rename(tmp, final)
        _mark_flushed(stream_id, hour_epoch, "sealed")
        shutil.rmtree(segs_dir, ignore_errors=True)
        _coverage_upsert_audio(stream_id, hour_epoch, "pending",
                               actual_seconds=meta["actual_seconds"], local_path=final,
                               reason="sealed_waiting_upload", size_bytes=meta["size"])
        log.info(f"[{stream_id}] {label} sellada ({meta['segments']} segs, "
                 f"{meta['size'] // 1048576}MB) -> bandeja de salida")
    elif status == "skipped":
        _mark_flushed(stream_id, hour_epoch, "skipped")
        shutil.rmtree(segs_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    else:                                            # invalid: se conserva el dir para revisión
        _mark_flushed(stream_id, hour_epoch, "invalid")
        shutil.rmtree(tmp, ignore_errors=True)
    return status


# Sellado fuera de la vuelta principal: armar una hora de ~900 segmentos tarda decenas de
# segundos (concat + manifest). La vuelta solo detecta las horas listas y las encola; un hilo
# las sella. Mientras una hora está en _sealing, la extracción no recrea su directorio.
_seal_q: "queue.Queue[tuple[str, int, Path]]" = queue.Queue()
_sealing: set = set()
_sealing_lock = threading.Lock()


def _is_sealing(stream_id: str, hour_epoch: int) -> bool:
    with _sealing_lock:
        return (stream_id, hour_epoch) in _sealing


def enqueue_ready_hours(stream_id: str, hold_hours: set) -> int:
    """Encola las horas de audio terminadas (fin + AUDIO_FLUSH_GRACE) cuyos segmentos ya se
    extrajeron todos. No sella ni toca S3: es barato y no frena la vuelta."""
    root = AUDIO_DIR / stream_id
    if not root.is_dir():
        return 0
    n = 0
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        h = parse_hour_label(d.name)
        if h is None or h in hold_hours or _now() < h + 3600 + AUDIO_FLUSH_GRACE:
            continue
        if _flushed_marker(stream_id, h).exists():
            continue                                   # 'invalid': dir conservado a propósito
        with _sealing_lock:
            if (stream_id, h) in _sealing:
                continue
            _sealing.add((stream_id, h))
        _seal_q.put((stream_id, h, d))
        n += 1
    return n


def seal_pending(max_items: int | None = None) -> int:
    """Sella lo encolado (lo corre el hilo de sellado). Un error deja la hora sin marcador:
    la vuelta la vuelve a encolar y el sellado es idempotente."""
    n = 0
    while max_items is None or n < max_items:
        try:
            sid, h, d = _seal_q.get_nowait()
        except queue.Empty:
            break
        try:
            seal_hour(sid, h, d)
        except Exception as e:
            log.error(f"[{sid}] sellado de {_hour_label(h)} falló, se reintenta: {e}")
        finally:
            with _sealing_lock:
                _sealing.discard((sid, h))
        n += 1
    return n


# ── Bandeja de salida: subida de horas de audio, desacoplada de la extracción ──
# Un hilo aparte sube lo sellado. La vuelta principal nunca espera a S3 por el audio.
OUTBOX_INTERVAL = float(os.environ.get("OUTBOX_INTERVAL", "5"))
_ob_down_until = 0.0
_ob_backoff = S3_BACKOFF_MIN
_ob_lock = threading.Lock()                          # una sola subida de bandeja a la vez


def outbox_entries() -> list[tuple[int, str, Path, dict]]:
    """Horas selladas pendientes de subir, la más vieja primero. Un dir sin meta.json
    (incompleto) o con meta ilegible no se sube."""
    out = []
    for mf in AUDIO_DIR.glob("*/.outbox/*/meta.json"):
        d = mf.parent
        if d.name.startswith("."):
            continue
        try:
            m = json.loads(mf.read_text())
            out.append((int(m["hour_epoch"]), m["stream_id"], d, m))
        except Exception as e:
            log.warning(f"bandeja: meta ilegible en {d}: {e}")
    return sorted(out, key=lambda x: (x[0], x[1]))


def _write_meta(d: Path, meta: dict) -> None:
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, d / "meta.json")


def upload_outbox_entry(s3_client, d: Path, meta: dict) -> str:
    """Sube una hora sellada: audio y después manifest; registra en s3_scan_log solo cuando
    ambos están verificados en S3. Idempotente: lo que ya está en S3 con el mismo tamaño no
    se vuelve a subir. Nunca reescribe una hora que ya está en S3 con un archivo distinto e
    igual o mayor (>= 90 %). 'uploaded' | 'already_in_s3' | 'upload_failed' | 'corrupt'."""
    sid, h, label = meta["stream_id"], int(meta["hour_epoch"]), meta["label"]
    ts, man = d / f"{label}.ts", d / "manifest.json"
    if (not ts.exists() or not man.exists() or ts.stat().st_size != meta["size"]
            or man.stat().st_size != meta.get("manifest_size", man.stat().st_size)):
        bad = d.with_name(f".corrupt-{label}")
        shutil.rmtree(bad, ignore_errors=True)
        os.rename(d, bad)
        log.error(f"[{sid}] {label}: entrada de bandeja incompleta/alterada -> {bad.name} (no se sube)")
        return "corrupt"
    key, mkey = _audio_s3_key(sid, h), _manifest_s3_key(sid, h)
    hour_start_utc = datetime.fromtimestamp(h, tz=timezone.utc)
    try:
        remote = _s3_size(s3_client, key)
        if remote is not None and remote != meta["size"] and remote >= 0.9 * meta["size"]:
            log.info(f"[{sid}] {label}: ya en S3 ({remote} B vs local {meta['size']} B); no se reescribe")
            _mark_flushed(sid, h, "already_in_s3")
            shutil.rmtree(d, ignore_errors=True)
            return "already_in_s3"
        if remote != meta["size"]:
            ok, err = upload_file_verified(s3_client, ts, key, "video/mp2t", attempts=1)
            if not ok:
                raise RuntimeError(err or "upload audio")
        if _s3_size(s3_client, mkey) != man.stat().st_size:
            ok, err = upload_file_verified(s3_client, man, mkey, "application/json", attempts=1)
            if not ok:
                raise RuntimeError(err or "upload manifest")
    except Exception as e:
        meta["attempts"] = int(meta.get("attempts", 0)) + 1
        _write_meta(d, meta)
        _coverage_upsert_audio(sid, h, "upload_failed", actual_seconds=meta["actual_seconds"],
                               local_path=ts, s3_key_value=key, reason="upload_failed",
                               size_bytes=meta["size"], upload_attempts=1, last_error=str(e)[:300])
        log.warning(f"[{sid}] {label}: subida fallida (intento {meta['attempts']}), queda en bandeja: {str(e)[:200]}")
        return "upload_failed"
    _db_register(key, sid, hour_start_utc.strftime("%Y-%m-%d"), hour_start_utc)
    _coverage_upsert_audio(sid, h, "uploaded", actual_seconds=meta["actual_seconds"],
                           local_path=ts, s3_key_value=key, reason=None,
                           size_bytes=meta["size"], upload_attempts=1)
    _mark_flushed(sid, h, "uploaded")
    shutil.rmtree(d, ignore_errors=True)
    log.info(f"[{sid}] {label}.ts → s3://{S3_BUCKET}/{key}  ({meta['segments']} segs, "
             f"{meta['size'] // 1048576}MB raw) + manifest ({meta['segments']} entradas)")
    return "uploaded"


def process_outbox_once(s3_client, ignore_backoff: bool = False) -> dict:
    """Sube lo sellado (más viejo primero). Al primer fallo corta y aplica backoff propio
    (60 -> 600 s), independiente del breaker de la subida de video."""
    global _ob_down_until, _ob_backoff
    res = {"uploaded": 0, "already_in_s3": 0, "corrupt": 0, "failed": 0}
    if not ignore_backoff and _now() < _ob_down_until:
        res["pending"] = len(outbox_entries())
        return res
    with _ob_lock:
        for _h, _sid, d, meta in outbox_entries():
            st = upload_outbox_entry(s3_client, d, meta)
            if st == "upload_failed":
                res["failed"] += 1
                _ob_down_until = _now() + _ob_backoff
                log.warning(f"bandeja: S3 no disponible; reintento en {_ob_backoff}s")
                _ob_backoff = min(_ob_backoff * 2, S3_BACKOFF_MAX)
                break
            _ob_backoff = S3_BACKOFF_MIN
            res[st] = res.get(st, 0) + 1
        res["pending"] = len(outbox_entries())
    return res


class OutboxWorker(threading.Thread):
    """Hilo que vacía la bandeja de salida. Recupera solo tras reinicios: la bandeja vive en
    disco. Usa su propio cliente S3."""

    def __init__(self, s3_client=None, interval: float = OUTBOX_INTERVAL):
        super().__init__(name="outbox-uploader", daemon=True)
        self.s3_client, self.interval, self.stop_evt = s3_client, interval, threading.Event()

    def run(self):
        s3 = self.s3_client or get_s3()
        while not self.stop_evt.is_set():
            try:
                process_outbox_once(s3)
            except Exception as e:
                log.error(f"bandeja: vuelta falló: {e}")
            self.stop_evt.wait(self.interval)


class SealWorker(threading.Thread):
    """Hilo que sella las horas encoladas por la vuelta principal (trabajo local, sin S3)."""

    def __init__(self, interval: float = 1.0):
        super().__init__(name="audio-sealer", daemon=True)
        self.interval, self.stop_evt = interval, threading.Event()

    def run(self):
        while not self.stop_evt.is_set():
            try:
                seal_pending()
            except Exception as e:
                log.error(f"sellado: vuelta falló: {e}")
            self.stop_evt.wait(self.interval)


def drain_outbox() -> int:
    """--drain-outbox: intenta subir toda la bandeja una vez (ignora el backoff) y devuelve
    cuántas horas quedan pendientes. Para rollback o recuperación manual."""
    res = process_outbox_once(get_s3(), ignore_backoff=True)
    log.info(f"drain: {res}")
    return res.get("pending", 0)


def upload_segment(s3_client, seg_path: Path, stream_id: str) -> str:
    """Un intento de subida. 'uploaded' | 'failed' | 'invalid' | 'no_audio' | 'gone'."""
    try:
        mtime = int(seg_path.stat().st_mtime)
        epoch_start = mtime - SEGMENT_DUR
        if not audio_safe(stream_id, seg_path, epoch_start):
            return "no_audio"                     # nunca borrar video cuyo audio no está a salvo
        key = s3_key(stream_id, epoch_start, mtime)
        valid, duration, reason = validate_video_segment(seg_path)
        size = seg_path.stat().st_size
    except FileNotFoundError:
        return "gone"
    if not valid:
        log.error(f"[{stream_id}] segmento inválido {seg_path.name}: {reason}")
        _video_coverage_add(stream_id, epoch_start, "invalid",
                            seconds=0, size_bytes=0, reason=reason, last_error=reason)
        quarantine_segment(seg_path, stream_id)
        return "invalid"
    try:
        ok, err = upload_file_verified(s3_client, seg_path, key, "video/mp2t", attempts=1)
    except FileNotFoundError:
        return "gone"
    if not ok:
        _video_coverage_add(stream_id, epoch_start, "upload_failed", seconds=0, size_bytes=0,
                            upload_attempts=1, reason=reason, last_error=err)
        s3_failed(err)
        return "failed"
    s3_ok()
    _video_coverage_add(stream_id, epoch_start, "uploaded", seconds=duration or SEGMENT_DUR,
                        size_bytes=size, upload_attempts=1, reason=reason)
    seg_path.unlink(missing_ok=True)
    return "uploaded"


def disk_free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def shed_tv_segments(work: dict, free_fn=disk_free_bytes) -> dict:
    """Emergencia de disco: descarta los segmentos TV más viejos de TODOS los canales hasta
    TARGET_FREE_GB. Solo segmentos con audio a salvo, de más de TV_SHED_MIN_AGE_MIN, fuera de
    los HLS_KEEP más nuevos. Lo hace el uploader (único dueño de los TV pendientes): no hay
    otro proceso que los abra en paralelo, así que comprobar-y-borrar no compite."""
    res = {"mode": TV_SHED_MODE, "emergency": False, "shed": [], "kept_no_audio": 0, "kept_young": 0}
    if TV_SHED_MODE == "off":
        return res
    free = free_fn(STREAMS_ROOT)
    res["free_gb"] = round(free / GB, 2)
    if free >= EMERGENCY_FREE_GB * GB:
        return res
    res["emergency"] = True
    now, cands = _now(), []
    for sid, (eligible, _hold) in work.items():
        for seg in eligible:
            try:
                st = seg.stat()
            except FileNotFoundError:
                continue
            epoch = int(st.st_mtime) - SEGMENT_DUR
            if now - st.st_mtime < TV_SHED_MIN_AGE_MIN * 60:
                res["kept_young"] += 1
            elif not audio_safe(sid, seg, epoch):
                res["kept_no_audio"] += 1
            else:
                cands.append((st.st_mtime, sid, seg, st.st_size, epoch))
    cands.sort(key=lambda c: c[0])
    enforce = TV_SHED_MODE == "enforce"
    for mtime, sid, seg, size, epoch in cands:
        if free >= TARGET_FREE_GB * GB:
            break
        log.warning(f"{'SHED' if enforce else 'WOULD SHED'} tv {sid} {seg.name} size={size} "
                    f"age={(now - mtime) / 60:.0f}min reason=disco<{EMERGENCY_FREE_GB:g}GB")
        if enforce:
            try:
                seg.unlink()
            except FileNotFoundError:
                continue
            _video_coverage_add(sid, epoch, "upload_failed", reason="shed_disk_emergency",
                                last_error=f"descartado por disco < {EMERGENCY_FREE_GB:g} GB")
        free += size
        res["shed"].append(str(seg))
    res["free_gb_after"] = round(free / GB, 2)
    log.warning(f"emergencia de disco [{TV_SHED_MODE}]: {len(res['shed'])} segmentos "
                f"{'descartados' if enforce else 'se descartarían'}; conservados sin audio="
                f"{res['kept_no_audio']} jóvenes={res['kept_young']}; libre≈{res['free_gb_after']} GB")
    return res


def _gc_flushed_markers() -> None:
    global _last_marker_gc
    if _now() - _last_marker_gc < 3600:
        return
    _last_marker_gc = _now()
    cutoff = _now() - FLUSHED_MARKER_KEEP_H * 3600   # por la hora que nombra, no por mtime
    for m in AUDIO_DIR.glob("*/.flushed/*"):
        h = parse_hour_label(m.name)
        if h is not None and h < cutoff:
            m.unlink(missing_ok=True)


def run_once(s3_client, tv_streams: list[str], free_fn=disk_free_bytes) -> dict:
    global _loop_n
    _loop_n += 1
    work: dict = {}
    # 1) extracción de audio, todos los canales, sin depender de S3
    for sid in tv_streams:
        d = STREAMS_ROOT / sid
        if not d.is_dir():
            continue
        segs = sorted(d.glob("seg_*.ts"), key=_seg_index)
        # Subida: nunca los HLS_KEEP más nuevos (el playlist los sirve). Audio: se extrae de todos
        # menos el que ffmpeg está escribiendo; si el canal está detenido, también ese. Si no, un
        # canal que se cae justo después de una hora dejaría esa hora de audio retenida para siempre.
        eligible = segs[:-HLS_KEEP] if len(segs) > HLS_KEEP else []
        to_extract = segs[:-1]
        if segs:
            last_ep = _seg_epoch(segs[-1])
            if last_ep is not None and _now() - (last_ep + SEGMENT_DUR) > STALE_WRITER_S:
                to_extract = segs
        hold, n = set(), 0
        for seg in segs:
            ep = _seg_epoch(seg)
            if ep is None or audio_safe(sid, seg, ep):
                continue
            if seg in to_extract and n < EXTRACT_MAX_PER_LOOP:
                n += 1
                if extract_audio(sid, seg, ep) or audio_safe(sid, seg, ep):
                    continue
            hold.add(_hour_of(ep))                   # su hora sigue abierta hasta tener el audio
        work[sid] = (eligible, hold)

    sids = list(work)
    if sids:                                         # rotación: ningún canal va siempre primero
        k = _loop_n % len(sids)
        sids = sids[k:] + sids[:k]

    # 2) encolar horas de audio terminadas: las sella el hilo de sellado y las sube el hilo de
    #    la bandeja. Esta vuelta nunca espera ni al concat ni a S3 por el audio.
    flushed = sum(enqueue_ready_hours(sid, work[sid][1]) for sid in sids)

    # 3) subida de video round-robin, 1 intento por segmento, corta al primer fallo
    uploaded = {sid: 0 for sid in sids}
    order = [(sid, work[sid][0][i]) for i in range(UPLOAD_MAX_PER_CHANNEL)
             for sid in sids if i < len(work[sid][0])]
    t_up = _now()
    for sid, seg in order:
        if not s3_available() or _now() - t_up > UPLOAD_TIME_BUDGET_S:
            break                                    # S3 caído o lento: la extracción no espera
        r = upload_segment(s3_client, seg, sid)
        if r == "uploaded":
            uploaded[sid] += 1
        elif r == "failed":
            break
    for sid, n in uploaded.items():
        if n:
            log.info(f"[{sid}] {n} segmentos subidos")
    _video_coverage_flush()

    # 4) emergencia de disco
    shed = shed_tv_segments(work, free_fn)
    _gc_flushed_markers()
    return {"uploaded": uploaded, "flushed": flushed, "shed": shed}


def run():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    INVALID_DIR.mkdir(parents=True, exist_ok=True)
    tv_streams = get_tv_streams()
    log.info(f"Video uploader v2 iniciado — TV streams: {tv_streams} shed={TV_SHED_MODE} "
             f"bandeja_pendiente={len(outbox_entries())}")
    s3_client = get_s3()
    workers = {"sellado": SealWorker, "bandeja": OutboxWorker}
    alive = {name: cls() for name, cls in workers.items()}
    for t in alive.values():
        t.start()
    while True:
        for name, t in list(alive.items()):          # los hilos no deberían morir; si mueren, se relanzan
            if not t.is_alive():
                log.error(f"hilo de {name} murió; relanzando")
                alive[name] = workers[name]()
                alive[name].start()
        refreshed = get_tv_streams()
        if refreshed and refreshed != tv_streams:
            tv_streams = refreshed
            log.info(f"TV streams actualizados: {tv_streams}")
        try:
            run_once(s3_client, tv_streams)
        except Exception as e:
            log.error(f"vuelta del uploader falló: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    if "--drain-outbox" in sys.argv[1:]:
        sys.exit(1 if drain_outbox() else 0)
    run()
