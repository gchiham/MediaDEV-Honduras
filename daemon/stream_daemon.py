#!/usr/bin/env python3
"""
MediaDEV Stream Daemon — estado en memoria + mtime, espejado a PostgreSQL
Health: 15s | Metrics: 60s | Record: 120s | Cleanup: 1800s

El estado operativo (salud, circuit breaker) vive en memoria y se recalcula
desde el filesystem. PostgreSQL (media-db) es un espejo de solo-lectura para el
dashboard; si la DB no está disponible el daemon sigue operando normalmente.

v2: config de streams desde DB (capture_config) con cache local de emergencia.
    procesos ffmpeg dueñados por el daemon — no supervisord.
"""
import os, sys, subprocess, time, signal, logging, boto3, json, tempfile, shlex
import psycopg2
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
STREAMS_ROOT = Path(os.environ.get("STREAMS_ROOT", "/var/www/streams"))
LOG_FILE     = os.environ.get("STREAMS_LOG",  "/var/log/streams/daemon.log")
STATIONS     = Path(os.environ.get("STATIONS_JSON", "/opt/media-ai/config/stations.json"))

# Nuevos: gateway activo y cache de config
STREAM_CACHE    = Path(os.environ.get("STREAM_CACHE", "/etc/mediadev/stream_config.cache.json"))
GW_PRIVOXY_PORT = int(os.environ.get("GW_PRIVOXY_PORT", "3128"))
GW_SOCKS5       = os.environ.get("GW_SOCKS5", "")

# Globals de runtime
STREAM_CFGS: dict[str, dict] = {}   # slug → config dict
_procs:      dict[str, dict] = {}   # slug → {main: Popen, aux: Popen|None}

def load_gateway_conf() -> None:
    """Lee GW_SOCKS5 y GW_PRIVOXY_PORT desde /etc/mediadev/gateway.conf.
    Expande referencias bash ${VAR} usando las variables ya parseadas en el mismo archivo."""
    global GW_SOCKS5, GW_PRIVOXY_PORT
    gw = Path("/etc/mediadev/gateway.conf")
    if not gw.exists():
        return
    vals: dict[str, str] = {}
    for line in gw.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip().strip('"').strip("'")
        for k, v in vals.items():
            raw = raw.replace(f"${{{k}}}", v)
        vals[key] = raw

    socks5 = vals.get("GW_SOCKS5", "")
    if not socks5 and "GW_SOCKS5_HOST" in vals and "GW_SOCKS5_PORT" in vals:
        socks5 = f"{vals['GW_SOCKS5_HOST']}:{vals['GW_SOCKS5_PORT']}"
    if socks5:
        GW_SOCKS5 = socks5

    port = vals.get("GW_PRIVOXY_PORT", "")
    if port:
        try:
            GW_PRIVOXY_PORT = int(port)
        except ValueError:
            pass

FALLBACK_STREAMS = ["fm_941", "hch_tv", "radio_america", "radio_choluteca", "radio_el_patio", "radio_globo", "radio_satelite", "suave_fm_teg", "teleceiba", "xy_hrn", "xy_sps", "xy_tgu", "canal_11", "canal_6", "canal_5", "tsi", "super_100", "radio_valle", "tnh"]
FALLBACK_TV_STREAMS = {"hch_tv", "teleceiba", "canal_11", "canal_6", "canal_5", "tsi", "tnh"}

# Plataformas cuyo stream_url es la pagina del canal, no un manifest directo -- ffmpeg no
# puede resolverlas solo (requieren negociar un token de reproduccion). streamlink si sabe.
#
# mdstrm.com (canal_5, Televicentro con inserción de anuncios de Google Ad Manager/DAI):
# el demuxer HLS nativo de ffmpeg no puede reusar la conexión HTTP cada vez que un
# segmento viene de un host distinto (el contenido regular sale de mdstrm.com, cada
# anuncio insertado sale de un edge de googlevideo.com distinto) -- eso generaba ~750
# reconexiones/10h y dejaba la cobertura real en ~49% aunque el proceso nunca se caía
# (por eso no disparaba el circuit breaker). streamlink maneja el pool de conexiones
# por su cuenta (via requests/urllib3) y no paga ese costo -- probado 2026-08-07:
# 170s pedidos = 170.03s reales capturados, 0 reconexiones, a 720p (misma calidad que
# el ffmpeg directo usaba). Ver CHANGES.log / memoria de canal_5 para el detalle.
_RESOLVER_PLATFORMS = ("kick.com", "mdstrm.com", "dailymotion.com")

def load_stream_catalog() -> tuple[list[str], set[str]]:
    """Bootstrap de módulo: lee stations.json o usa fallback hardcodeado."""
    try:
        data = json.loads(STATIONS.read_text())
        enabled = [s for s in data.get("stations", []) if s.get("enabled", True)]
        streams = [s["id"] for s in enabled if s.get("id")]
        tv_streams = {s["id"] for s in enabled if s.get("type") == "tv" and s.get("id")}
        if streams:
            return streams, tv_streams
    except Exception:
        pass
    return FALLBACK_STREAMS, FALLBACK_TV_STREAMS

STREAMS, TV_STREAMS = load_stream_catalog()

STALE_SECS          = 90
CB_FAIL_OPEN        = 8
CB_RESET_SECS       = 600
RESTART_AFTER_FAILS = 3
RESTART_GRACE_SECS  = int(os.environ.get("RESTART_GRACE_SECS", "45"))
DOWN_EVENT_AFTER_SECS = int(os.environ.get("DOWN_EVENT_AFTER_SECS", "180"))
SEG_DURATION        = 4
TGU = timezone(timedelta(hours=-6))
RECORDING_NAMING_MODE = os.environ.get("RECORDING_NAMING_MODE", "utc").strip().lower()
KEEP_SEG_HOURS = 8
KEEP_MP3_COUNT = 8
MIN_AUDIO_SECONDS = int(os.environ.get("MIN_AUDIO_SECONDS", "60"))
FULL_HOUR_MIN_SECONDS = int(os.environ.get("FULL_HOUR_MIN_SECONDS", "3300"))
RECORDING_ALERT_MIN_SECONDS = int(os.environ.get("RECORDING_ALERT_MIN_SECONDS", "900"))
S3_UPLOAD_RETRIES = int(os.environ.get("S3_UPLOAD_RETRIES", "3"))
PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "utc_v2")
TG_ENV_FILE = os.environ.get("TG_ENV_FILE", "/opt/destroyer/.env")

# Offload de transcode al Destroyer: en vez de recodificar a MP3 (libmp3lame) en
# mediaCAP, concatenamos los segmentos AAC con -c copy (casi gratis) y subimos el
# .ts crudo. El worker del Destroyer ya tiene el path .ts→mp3 ("offloaded from
# mediaCAP") y deriva el airtime de hour_start_utc (DB), no del nombre. Kill-switch:
# RAW_AUDIO_OFFLOAD=0 + restart vuelve al MP3 local.
RAW_AUDIO_OFFLOAD = os.environ.get("RAW_AUDIO_OFFLOAD", "1") == "1"
RAW_AUDIO_EXT = "ts" if RAW_AUDIO_OFFLOAD else "mp3"

# ── S3 ────────────────────────────────────────────────────────────────────────
S3_BUCKET  = os.environ.get("S3_BUCKET",  "mediadev-recordings")
S3_REGION  = os.environ.get("S3_REGION",  "us-east-1")
PEER_ROLE  = os.environ.get("PEER_ROLE",  "primary")
BACKUP_PFX = "_backup"

def audio_s3_key(local_path: Path, stream_id: str) -> str:
    date_part = local_path.name[:10]
    year, month = date_part[:4], date_part[5:7]
    canon = f"{stream_id}/{year}/{month}/{local_path.name}"
    return f"{BACKUP_PFX}/{canon}" if PEER_ROLE == "backup" else canon

def s3_upload_verified(local_path: Path, stream_id: str) -> tuple[bool, str, str | None]:
    s3 = boto3.client("s3", region_name=S3_REGION)
    key = audio_s3_key(local_path, stream_id)
    size = local_path.stat().st_size
    ctype = "video/mp2t" if local_path.suffix == ".ts" else "audio/mpeg"
    last_error = None

    for attempt in range(1, S3_UPLOAD_RETRIES + 1):
        try:
            s3.upload_file(str(local_path), S3_BUCKET, key,
                           ExtraArgs={"ContentType": ctype})
            head = s3.head_object(Bucket=S3_BUCKET, Key=key)
            if int(head.get("ContentLength", -1)) != size:
                raise RuntimeError(f"size mismatch local={size} s3={head.get('ContentLength')}")
            log.info(f"[{stream_id}] S3 OK [{PEER_ROLE}] → s3://{S3_BUCKET}/{key}")
            return True, key, None
        except Exception as e:
            last_error = str(e)
            log.warning(f"[{stream_id}] S3 intento {attempt}/{S3_UPLOAD_RETRIES} falló: {e}")
            time.sleep(min(2 ** attempt, 15))

    log.error(f"[{stream_id}] S3 FAIL final: {last_error}")
    return False, key, last_error

def s3_object_matches(key: str, size: int) -> bool:
    try:
        s3 = boto3.client("s3", region_name=S3_REGION)
        head = s3.head_object(Bucket=S3_BUCKET, Key=key)
        if size <= 0:
            return True
        return int(head.get("ContentLength", -1)) == size
    except Exception:
        return False

# Intervalos
INTERVAL_HEALTH  = 15
INTERVAL_METRICS = 60
INTERVAL_RECORD  = 120
INTERVAL_CLEAN   = 1800
INTERVAL_CONFIG  = 300   # refrescar config de DB cada 5 min
LOOP_SLEEP       = 2
AUTO_BACKFILL_HOURS = int(os.environ.get("AUTO_BACKFILL_HOURS", "3"))

# ── PostgreSQL (estado para el dashboard) ──────────────────────────────────────
# El estado operativo vive en memoria; PostgreSQL es solo un espejo para el
# dashboard. Si la DB no está disponible el daemon sigue funcionando igual.
PG_HOST = os.environ.get("PG_HOST")
PG_PORT = int(os.environ.get("PG_PORT", "25060"))
PG_DB   = os.environ.get("PG_DB")
PG_USER = os.environ.get("PG_USER")
PG_PASS = os.environ.get("PG_PASS")
METRICS_RETENTION_DAYS = 7
EVENTS_RETENTION_DAYS  = 30

_pg = None
_schema_cols: dict[str, set[str]] = {}

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_epoch() -> int:
    return int(utc_now().timestamp())

def recording_hour_label(hour_epoch: int) -> str:
    if RECORDING_NAMING_MODE == "legacy_hn":
        return datetime.fromtimestamp(hour_epoch, tz=TGU).strftime("%Y-%m-%d_%Hh")
    return datetime.fromtimestamp(hour_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%HZ")

def pg():
    """Devuelve una conexión PG viva (autocommit) o None si no se puede conectar."""
    global _pg
    if not (PG_HOST and PG_PASS):
        return None
    try:
        if _pg is None or _pg.closed:
            _pg = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS,
                connect_timeout=5, sslmode="require",
            )
            _pg.autocommit = True
        return _pg
    except Exception as e:
        log.warning(f"[pg] sin conexión: {e}")
        _pg = None
        return None

def pg_write(sql, params=(), many=False):
    """Ejecuta una escritura PG tolerando fallos (nunca lanza)."""
    conn = pg()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            if many:
                cur.executemany(sql, params)
            else:
                cur.execute(sql, params)
    except Exception as e:
        log.warning(f"[pg] escritura falló: {e}")
        global _pg
        _pg = None

def table_columns(table: str) -> set[str]:
    cols = _schema_cols.get(table)
    if cols is not None:
        return cols

    conn = pg()
    if conn is None:
        return set()
    try:
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
    except Exception as e:
        log.warning(f"[pg] no se pudo leer schema de {table}: {e}")
        return set()

def pg_event(sid, etype, detail=""):
    pg_write(
        "INSERT INTO mediadev_events (stream_id, ts, etype, detail) VALUES (%s,%s,%s,%s)",
        (sid, utc_epoch(), etype, detail),
    )

def coverage_upsert(
    stream_id: str,
    media_type: str,
    period_start: datetime,
    period_end: datetime,
    expected_seconds: int,
    status: str,
    *,
    actual_seconds: float | None = None,
    local_path: Path | None = None,
    s3_key: str | None = None,
    reason: str | None = None,
    size_bytes: int | None = None,
    upload_attempts: int = 0,
    last_error: str | None = None,
    source_service: str = "stream-daemon",
) -> None:
    if not table_columns("recording_coverage"):
        return

    pg_write(
        """
        INSERT INTO recording_coverage
          (stream_id, media_type, period_start_utc, period_end_utc,
           expected_seconds, actual_seconds, local_path, s3_key, status, reason,
           size_bytes, upload_attempts, last_error, source_service,
           pipeline_version, updated_at)
        VALUES
          (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (stream_id, media_type, period_start_utc) DO UPDATE SET
          period_end_utc=EXCLUDED.period_end_utc,
          expected_seconds=EXCLUDED.expected_seconds,
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
            stream_id, media_type, period_start, period_end, expected_seconds,
            actual_seconds, str(local_path) if local_path else None, s3_key,
            status, reason, size_bytes, upload_attempts, last_error,
            source_service, PIPELINE_VERSION,
        ),
    )

def s3_scan_register(s3_key: str, stream_id: str, hour_start_utc: datetime) -> None:
    cols = table_columns("s3_scan_log")
    if not cols:
        return

    insert_cols = ["s3_key", "stream", "recorded_date", "status", "updated_at"]
    values = [s3_key, stream_id, hour_start_utc.date().isoformat(), "pending", utc_now()]

    if "hour_start_utc" in cols:
        insert_cols.append("hour_start_utc")
        values.append(hour_start_utc)
    if "pipeline_version" in cols:
        insert_cols.append("pipeline_version")
        values.append(PIPELINE_VERSION)

    pg_write(
        f"""
        INSERT INTO s3_scan_log ({', '.join(insert_cols)})
        VALUES ({', '.join(['%s'] * len(values))})
        ON CONFLICT (s3_key) DO NOTHING
        """,
        values,
    )

def ffprobe_duration(path: Path, stream_selector: str = "a:0") -> tuple[float | None, str | None]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", stream_selector,
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

def validate_audio(path: Path) -> tuple[bool, float | None, str | None]:
    if not path.exists() or path.stat().st_size == 0:
        return False, None, "missing_or_empty"

    duration, err = ffprobe_duration(path, "a:0")
    if duration is None:
        return False, None, err or "no_audio_duration"
    if duration < MIN_AUDIO_SECONDS:
        return False, duration, f"too_short_{int(duration)}s"
    if duration < FULL_HOUR_MIN_SECONDS:
        return True, duration, f"partial_{int(duration)}s"
    return True, duration, None

def concat_file_line(path: Path) -> str:
    safe = str(path.resolve()).replace("'", "'\\''")
    return f"file '{safe}'"

def parse_recording_hour(name: str) -> datetime | None:
    stem = name.rsplit(".", 1)[0]  # quita .mp3 o .ts
    for fmt in ("%Y-%m-%dT%HZ", "%Y-%m-%d_%Hh"):
        try:
            dt = datetime.strptime(stem, fmt)
            tz = timezone.utc if fmt == "%Y-%m-%dT%HZ" else TGU
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    return None

def recover_pending_audio_uploads() -> None:
    for sid in STREAMS:
        if sid in TV_STREAMS:
            continue
        rec_dir = STREAMS_ROOT / sid / "recordings"
        if not rec_dir.exists():
            continue

        pending = sorted([*rec_dir.glob("*.mp3"), *rec_dir.glob("*.ts")],
                         key=lambda f: f.name)
        for mp3 in pending:
            hour_start = parse_recording_hour(mp3.name)
            if hour_start is None:
                continue
            hour_end = hour_start + timedelta(hours=1)
            valid, duration, reason = validate_audio(mp3)
            size = mp3.stat().st_size if mp3.exists() else 0
            if not valid:
                coverage_upsert(
                    sid, "audio", hour_start, hour_end, 3600, "invalid",
                    actual_seconds=duration,
                    local_path=mp3,
                    reason=reason,
                    size_bytes=size,
                    source_service="stream-daemon",
                )
                continue

            key = audio_s3_key(mp3, sid)
            if s3_object_matches(key, size):
                s3_scan_register(key, sid, hour_start)
                coverage_upsert(
                    sid, "audio", hour_start, hour_end, 3600, "uploaded",
                    actual_seconds=duration,
                    local_path=mp3,
                    s3_key=key,
                    reason=reason,
                    size_bytes=size,
                    source_service="stream-daemon",
                )
                continue

            ok, key, err = s3_upload_verified(mp3, sid)
            coverage_upsert(
                sid, "audio", hour_start, hour_end, 3600,
                "uploaded" if ok else "upload_failed",
                actual_seconds=duration,
                local_path=mp3,
                s3_key=key,
                reason=reason,
                size_bytes=size,
                upload_attempts=1 if ok else S3_UPLOAD_RETRIES,
                last_error=err,
                source_service="stream-daemon",
            )
            if ok:
                s3_scan_register(key, sid, hour_start)

# ── CONFIG DESDE DB (disponible después de que pg() esté definido) ─────────────
def load_config_from_db() -> list[dict] | None:
    conn = pg()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ms.slug, ms.name, ms.media_type,
                       cc.stream_url, cc.route,
                       cc.mp3_s3_prefix, cc.ts_s3_prefix, cc.ffmpeg_extra
                FROM capture_config cc
                JOIN media_sources ms ON ms.id = cc.media_source_id
                WHERE cc.is_enabled = true
                  AND ms.lifecycle_status = 'active'
                  AND ms.slug IS NOT NULL
                ORDER BY ms.media_type, ms.slug
            """)
            cfgs = [{"slug": r[0], "name": r[1], "media_type": r[2],
                     "stream_url": r[3], "route": r[4],
                     "mp3_s3_prefix": r[5], "ts_s3_prefix": r[6],
                     "ffmpeg_extra": r[7]}
                    for r in cur.fetchall()]
            return cfgs or None
    except Exception as e:
        log.warning(f"[config] DB query falló: {e}")
        return None

def apply_stream_configs(cfgs: list[dict]) -> tuple[list[str], set[str]]:
    global STREAM_CFGS
    STREAM_CFGS = {c["slug"]: c for c in cfgs}
    return ([c["slug"] for c in cfgs],
            {c["slug"] for c in cfgs if c["media_type"] == "tv"})

def refresh_config() -> list[dict] | None:
    """DB → escribe cache → devuelve configs. Si DB falla, lee cache."""
    cfgs = load_config_from_db()
    if cfgs:
        try:
            STREAM_CACHE.parent.mkdir(parents=True, exist_ok=True)
            STREAM_CACHE.write_text(json.dumps(cfgs, indent=2))
        except Exception:
            pass
        log.info(f"[config] DB: {len(cfgs)} streams activos")
        return cfgs
    try:
        if STREAM_CACHE.exists():
            cfgs = json.loads(STREAM_CACHE.read_text())
            if cfgs:
                log.warning(f"[config] DB no disponible — cache ({len(cfgs)} streams)")
                return cfgs
    except Exception as e:
        log.warning(f"[config] cache inválido: {e}")
    return None

# ── PROCESS MANAGEMENT ────────────────────────────────────────────────────────
_RECONNECT = [
    "-reconnect", "1", "-reconnect_at_eof", "0",
    "-reconnect_streamed", "1", "-reconnect_delay_max", "8",
    "-rw_timeout", "20000000", "-timeout", "15000000",
]

def _extra_ffmpeg_args(cfg: dict) -> list[str]:
    """capture_config.ffmpeg_extra → lista de args, insertados antes de -i.
    Pensado para overrides puntuales por canal (ej. canal_5/mdstrm.com: fuente
    con segmentos de 10s vs los 4s habituales -- por defecto el demuxer HLS de
    ffmpeg no reintenta un segmento que falla (seg_max_retry=0), lo descarta y
    sigue; con segmentos de 10s eso pierde bloques grandes de contenido sin
    generar ningún error visible (la conexión sigue viva). ffmpeg_extra permite
    setear '-seg_max_retry 3' u otros ajustes por canal sin tocar código de nuevo."""
    raw = (cfg.get("ffmpeg_extra") or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError as e:
        log.warning(f"[{cfg.get('slug')}] ffmpeg_extra inválido ({raw!r}): {e}")
        return []

FFMPEG_ERR_DIR = Path(os.environ.get("FFMPEG_ERR_DIR", "/var/log/streams/ffmpeg"))
FFMPEG_ERR_MAX_BYTES = int(os.environ.get("FFMPEG_ERR_MAX_BYTES", str(20 * 1024 * 1024)))

def _ffmpeg_stderr_file(sid: str):
    """Archivo de stderr de ffmpeg para sid (trunca si excede el límite).

    Antes stderr iba a un subprocess.PIPE que el daemon nunca leía. Si ffmpeg
    escribía suficientes warnings (típico en streams con discontinuidades de
    inserción de anuncios), el pipe (buffer de 64KB del kernel) podía llenarse
    y el write() de ffmpeg se bloqueaba en silencio -- sin ese log no había
    forma de diagnosticar nada de esto salvo inferirlo por mtimes de archivos."""
    FFMPEG_ERR_DIR.mkdir(parents=True, exist_ok=True)
    path = FFMPEG_ERR_DIR / f"{sid}.err"
    try:
        if path.exists() and path.stat().st_size > FFMPEG_ERR_MAX_BYTES:
            path.write_text("")
    except OSError:
        pass
    return open(path, "a")

def _next_seg_number(sid: str) -> int:
    """Mayor índice de seg_NNNNN.ts existente + 1 (0 si no hay).

    Para que un respawn de ffmpeg CONTINÚE la numeración en vez de reiniciar a 0 y
    SOBRESCRIBIR los segmentos de la hora en curso. do_record selecciona segmentos
    por mtime; pisar archivos = perder esa parte de la hora (corrupción del filo de
    hora documentada). Continuar la numeración la elimina para TODO respawn (crash,
    health-restart, restart del daemon, gateway switch).

    '%05d' es ancho MÍNIMO (printf): índices > 99999 usan más dígitos, sin overflow.
    Glob de un solo directorio por stream — barato, no viola el constraint de 2 vCPU.
    """
    mx = -1
    try:
        for f in (STREAMS_ROOT / sid).glob("seg_*.ts"):
            n = f.stem[4:]  # 'seg_00042' → '00042'
            if n.isdigit():
                v = int(n)
                if v > mx:
                    mx = v
    except OSError:
        pass
    return mx + 1

def _hls_args(sid: str) -> list[str]:
    d = str(STREAMS_ROOT / sid)
    return ["-f", "hls", "-hls_time", "4", "-hls_list_size", "10",
            "-hls_flags", "append_list+omit_endlist",
            "-start_number", str(_next_seg_number(sid)),
            "-hls_segment_filename", f"{d}/seg_%05d.ts", f"{d}/index.m3u8"]

def spawn_stream(sid: str) -> bool:
    cfg = STREAM_CFGS.get(sid)
    if not cfg:
        log.warning(f"[{sid}] spawn: sin config en STREAM_CFGS")
        return False
    # Matar SIEMPRE el proceso previo de este sid antes de respawnear (vivo o muerto).
    # BUG raíz del leak: antes solo se hacía wait() de los YA-muertos; los ffmpeg STALLED
    # (vivos pero colgados en el source flaky) quedaban huérfanos al sobrescribir _procs[sid]
    # y se acumulaban (load-96 jun-16 / teleceiba 16 ffmpeg jun-29). Ahora se reapean siempre.
    old = _procs.pop(sid, None)
    if old:
        for p in [old.get("main"), old.get("aux")]:
            if not p:
                continue
            if p.poll() is None:                       # vivo (stalled) → matar el grupo
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    pass
            try:
                p.wait(timeout=3)                      # esperar a que muera
            except Exception:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # terco → SIGKILL al grupo
                    p.wait(timeout=2)
                except Exception:
                    pass
    (STREAMS_ROOT / sid).mkdir(parents=True, exist_ok=True)
    url    = cfg["stream_url"]
    route  = cfg.get("route", "direct")
    mtype  = cfg.get("media_type", "radio")
    socks5 = (route == "socks5")
    is_ice = mtype == "radio" and ".m3u8" not in url and ".m3u" not in url
    needs_resolver = any(p in url for p in _RESOLVER_PLATFORMS)
    proxy  = ["-http_proxy", f"http://127.0.0.1:{GW_PRIVOXY_PORT}"] if socks5 else []
    try:
        if mtype == "tv" and needs_resolver:
            # Canal cuya URL es la pagina del stream (no un .m3u8 directo) -- streamlink
            # resuelve el manifest firmado (token corto, ~10min) y lo mantiene refrescado.
            quality = cfg.get("ffmpeg_extra") or "720p,best"
            sc = ["streamlink", "--stdout", "--stream-timeout", "20"]
            # --hls-live-restart re-descarga la ventana DVR completa al arrancar. Con
            # fuentes de ventana corta recupera huecos; con DVR de HORAS (Dailymotion,
            # canal_10) provoca captura a 4x con contenido duplicado y air_time rotos
            # (visto 23 ago 2026). Configurable por estacion; default true = como antes.
            if cfg.get("hls_live_restart", True):
                sc.append("--hls-live-restart")
            sc += [url, quality]
            fc = (["ffmpeg", "-hide_banner", "-loglevel", "warning",
                   "-i", "pipe:0",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
                  + _hls_args(sid))
            cp = subprocess.Popen(sc, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  preexec_fn=os.setsid)
            errf = _ffmpeg_stderr_file(sid)
            fp = subprocess.Popen(fc, stdin=cp.stdout,
                                  stdout=subprocess.DEVNULL,
                                  stderr=errf,
                                  preexec_fn=os.setsid)
            errf.close()
            cp.stdout.close()
            _procs[sid] = {"main": fp, "aux": cp}
        elif is_ice:
            cc = ["curl", "-s", "--max-time", "0", "--retry", "0",
                  "-A", "MediaDEV/1.0", "-L"]
            if socks5 and GW_SOCKS5:
                cc += ["--socks5-hostname", GW_SOCKS5.replace("socks5://", "")]
            cc.append(url)
            fc = (["ffmpeg", "-hide_banner", "-loglevel", "warning",
                   "-i", "pipe:0",
                   "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "22050"]
                  + _hls_args(sid))
            cp = subprocess.Popen(cc, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  preexec_fn=os.setsid)
            errf = _ffmpeg_stderr_file(sid)
            fp = subprocess.Popen(fc, stdin=cp.stdout,
                                  stdout=subprocess.DEVNULL,
                                  stderr=errf,
                                  preexec_fn=os.setsid)
            errf.close()
            cp.stdout.close()
            _procs[sid] = {"main": fp, "aux": cp}
        elif mtype == "tv":
            # teleceiba: source con VIDEO corrupto (H264) pero AUDIO limpio. Re-encodear el
            # audio (-c:a aac) degradaba el fingerprint (doble compresión AAC → scores ~357).
            # -c:a copy pasa el audio original tal cual → mejor detección. Video se copia igual
            # para la evidencia. PRUEBA teleceiba-only; si mejora, generalizar a TV.
            audio_args = (["-c:a", "copy"] if sid in ("teleceiba", "canal_5")
                          else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"])
            cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning"]
                   + proxy + _RECONNECT + _extra_ffmpeg_args(cfg)
                   + ["-i", url, "-c:v", "copy"] + audio_args
                   + _hls_args(sid))
            errf = _ffmpeg_stderr_file(sid)
            _procs[sid] = {"main": subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=errf,
                preexec_fn=os.setsid), "aux": None}
            errf.close()
        else:  # radio HLS / m3u8
            cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning"]
                   + proxy + _RECONNECT + _extra_ffmpeg_args(cfg)
                   + ["-i", url,
                      "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "22050"]
                   + _hls_args(sid))
            errf = _ffmpeg_stderr_file(sid)
            _procs[sid] = {"main": subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=errf,
                preexec_fn=os.setsid), "aux": None}
            errf.close()
        log.info(f"[{sid}] spawned pid={_procs[sid]['main'].pid} "
                 f"route={route} type={mtype} ice={is_ice} resolver={needs_resolver}")
        return True
    except Exception as e:
        log.error(f"[{sid}] spawn error: {e}")
        return False

def stop_stream(sid: str) -> None:
    info = _procs.pop(sid, None)
    if not info:
        return
    for p in [info.get("main"), info.get("aux")]:
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
    log.info(f"[{sid}] proceso detenido")

# ── LOGGING ───────────────────────────────────────────────────────────────────
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("daemon")

# ── TELEGRAM (alertas agregadas de cobertura) ────────────────────────────────
_tg_creds = None
_recording_alert_sent_hours: set[int] = set()

def _tg_get_creds():
    global _tg_creds
    if _tg_creds is not None:
        return _tg_creds

    tok = os.environ.get("TG_TOKEN")
    chat = os.environ.get("TG_CHAT")
    if not (tok and chat):
        try:
            for line in Path(TG_ENV_FILE).read_text().splitlines():
                line = line.strip()
                if line.startswith("TG_TOKEN=") and not tok:
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TG_CHAT=") and not chat:
                    chat = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    _tg_creds = (tok, chat)
    return _tg_creds

def tg_send(text: str) -> None:
    tok, chat = _tg_get_creds()
    if not (tok and chat):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"tg_send error: {e}")

def send_recording_coverage_alert(hour_epoch: int, low_coverage: list[tuple[str, int]]) -> None:
    if not low_coverage or hour_epoch in _recording_alert_sent_hours:
        return

    _recording_alert_sent_hours.add(hour_epoch)
    label = recording_hour_label(hour_epoch)
    lines = "\n".join(
        f"- {sid}: {seconds // 60} min aprox" for sid, seconds in low_coverage
    )
    log.warning(f"[recording] ALERTA cobertura baja {label}: {low_coverage}")
    tg_send(
        "⚠️ COBERTURA BAJA DE GRABACIÓN\n"
        f"Hora {label}\n"
        f"Umbral: {RECORDING_ALERT_MIN_SECONDS // 60} min\n\n"
        f"{lines}\n\n"
        "No significa necesariamente que el stream cayó completo; revisar "
        "recording_coverage y logs de mediaCAP."
    )

# ── STATE (en memoria) ────────────────────────────────────────────────────────
def init_state():
    return {
        sid: {
            "status":        "UNKNOWN",
            "sup":           "UNKNOWN",
            "segs":          0,
            "age":           0,
            "cb_state":      "CLOSED",
            "cb_fails":      0,
            "cb_since":      0,
            "last_down":     0,
            "last_up":       0,
            "restart_today": 0,
            "restart_grace_until": 0,
            "first_bad_since": 0,
            "down_event_sent": False,
        }
        for sid in STREAMS
    }

_last_config_refresh = 0.0

def refresh_catalog_state(state):
    global STREAMS, TV_STREAMS, _last_config_refresh
    now_float = time.time()
    if now_float - _last_config_refresh < INTERVAL_CONFIG:
        return
    _last_config_refresh = now_float
    load_gateway_conf()

    cfgs = refresh_config()
    if not cfgs:
        return

    streams, tv_streams = apply_stream_configs(cfgs)
    if streams == STREAMS and tv_streams == TV_STREAMS:
        return

    previous = set(STREAMS)
    new_set  = set(streams)
    STREAMS    = streams
    TV_STREAMS = tv_streams

    for sid in previous - new_set:
        log.info(f"[{sid}] removido del catálogo — deteniendo")
        stop_stream(sid)
        state.pop(sid, None)

    for sid in new_set - previous:
        log.info(f"[{sid}] nuevo en catálogo — spawneando")
        state[sid] = {
            "status": "UNKNOWN", "sup": "UNKNOWN", "segs": 0, "age": 0,
            "cb_state": "CLOSED", "cb_fails": 0, "cb_since": 0,
            "last_down": 0, "last_up": 0, "restart_today": 0,
            "restart_grace_until": 0, "first_bad_since": 0, "down_event_sent": False,
        }
        spawn_stream(sid)

    log.info(f"Catálogo actualizado: {len(STREAMS)} streams ({len(TV_STREAMS)} TV)")

# ── HEALTH CHECK ──────────────────────────────────────────────────────────────
def m3u8_seg_count(m3u8: Path) -> int:
    try:
        return sum(1 for l in m3u8.read_text().splitlines() if l.endswith(".ts"))
    except:
        return 0

def restart_stream(state, sid):
    stop_stream(sid)
    time.sleep(1)
    spawn_stream(sid)
    state[sid]["restart_today"] += 1
    state[sid]["restart_grace_until"] = utc_epoch() + RESTART_GRACE_SECS

def do_health(state):
    refresh_catalog_state(state)
    now  = utc_epoch()

    for sid in STREAMS:
        s    = state[sid]
        prev = s["status"]

        # Liveness check del proceso (reemplaza supervisorctl status)
        pinfo = _procs.get(sid)
        main  = pinfo.get("main") if pinfo else None
        alive = main is not None and main.poll() is None
        sup   = "RUNNING" if alive else "STOPPED"

        # Si el proceso murió y no estamos en CB OPEN ni en grace, respawnear
        if not alive and s["cb_state"] != "OPEN" and s["restart_grace_until"] < now:
            log.warning(f"[{sid}] proceso muerto — respawneando automáticamente")
            spawn_stream(sid)

        m3u8 = STREAMS_ROOT / sid / "index.m3u8"

        if m3u8.exists():
            age  = now - int(m3u8.stat().st_mtime)
            segs = m3u8_seg_count(m3u8)
            empty_playlist = (segs == 0 and m3u8.stat().st_size > 50)
            ok   = age <= STALE_SECS and segs > 0
        else:
            age = segs = 0; ok = False; empty_playlist = False

        s["sup"]  = sup
        s["age"]  = age
        s["segs"] = segs

        if s["cb_state"] == "OPEN":
            if now - s["cb_since"] >= CB_RESET_SECS:
                s["cb_state"] = "CLOSED"; s["cb_fails"] = 0
                log.info(f"[{sid}] CB → CLOSED (reset)")
                pg_event(sid, "CB_CLOSE", "reset automático")
                restart_stream(state, sid)
            else:
                s["status"] = "DISABLED"
                continue

        if ok:
            s["cb_fails"] = 0
            s["first_bad_since"] = 0
            if prev not in ("OK", "UNKNOWN"):
                log.info(f"[{sid}] ↑ UP")
                s["last_up"] = now
                if s.get("down_event_sent"):
                    pg_event(sid, "UP")
                s["down_event_sent"] = False
            s["status"] = "OK"
        else:
            s["status"] = "STALE" if m3u8.exists() else "NO_M3U8"
            if not s.get("first_bad_since"):
                s["first_bad_since"] = now
            if s["restart_grace_until"] > now:
                continue
            s["cb_fails"] += 1
            if prev == "OK":
                log.warning(f"[{sid}] ↓ DOWN age={age}s cb_fails={s['cb_fails']} empty={empty_playlist}")
                s["last_down"] = now
            down_for = now - (s.get("first_bad_since") or now)
            if not s.get("down_event_sent") and down_for >= DOWN_EVENT_AFTER_SECS:
                pg_event(sid, "DOWN", f"age={age}s down_for={down_for}s empty={empty_playlist}")
                s["down_event_sent"] = True
            if not empty_playlist and s["cb_fails"] >= RESTART_AFTER_FAILS and s["cb_fails"] <= CB_FAIL_OPEN:
                log.info(f"[{sid}] Reiniciando ffmpeg tras {s['cb_fails']} fallos")
                restart_stream(state, sid)
            if s["cb_fails"] >= CB_FAIL_OPEN and s["cb_state"] == "CLOSED":
                s["cb_state"] = "OPEN"; s["cb_since"] = now
                log.warning(f"[{sid}] CB → OPEN tras {s['cb_fails']} fallos")
                pg_event(sid, "CB_OPEN", f"{s['cb_fails']} fallos")

    pg_sync_status(state)

# ── SYNC ESTADO A POSTGRES ─────────────────────────────────────────────────────
def pg_sync_status(state):
    now = utc_epoch()
    rows = [
        (sid, s["status"], s["sup"], s["segs"], s["age"], s["cb_state"],
         s["cb_fails"], s["cb_since"], s["restart_today"],
         s["last_down"], s["last_up"], now)
        for sid, s in state.items()
    ]
    pg_write(
        """INSERT INTO mediadev_stream_status
             (stream_id,status,sup,segs,age,cb_state,cb_fails,cb_since,
              restart_today,last_down,last_up,updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (stream_id) DO UPDATE SET
             status=EXCLUDED.status, sup=EXCLUDED.sup, segs=EXCLUDED.segs,
             age=EXCLUDED.age, cb_state=EXCLUDED.cb_state, cb_fails=EXCLUDED.cb_fails,
             cb_since=EXCLUDED.cb_since, restart_today=EXCLUDED.restart_today,
             last_down=EXCLUDED.last_down, last_up=EXCLUDED.last_up,
             updated_at=EXCLUDED.updated_at""",
        rows, many=True,
    )

# ── METRICS (snapshot por minuto) ───────────────────────────────────────────────
def do_metrics(state):
    now    = utc_epoch()
    window = now - INTERVAL_METRICS
    rows   = []
    for sid in STREAMS:
        b = 0
        for seg in (STREAMS_ROOT / sid).glob("seg_*.ts"):
            try:
                if seg.stat().st_mtime >= window:
                    b += seg.stat().st_size
            except:
                pass
        s = state[sid]
        rows.append((sid, now, s["status"], s["segs"], b))
    pg_write(
        "INSERT INTO mediadev_metrics (stream_id,ts,status,segs,bytes) VALUES (%s,%s,%s,%s,%s)",
        rows, many=True,
    )

# ── HOURLY RECORDINGS ─────────────────────────────────────────────────────────
def do_record(state):
    now = utc_epoch()
    cur_h = now - now % 3600
    targets = [cur_h - 3600 * k for k in range(1, AUTO_BACKFILL_HOURS + 1)]

    recover_pending_audio_uploads()
    low_coverage_by_hour: dict[int, list[tuple[str, int]]] = {h: [] for h in targets}

    for sid in STREAMS:
        if sid in TV_STREAMS:
            continue

        rec_dir = STREAMS_ROOT / sid / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)

        seg_mtimes = None
        for h_start in targets:
            h_end   = h_start + 3600
            h_label = recording_hour_label(h_start)
            period_start = datetime.fromtimestamp(h_start, tz=timezone.utc)
            period_end = datetime.fromtimestamp(h_end, tz=timezone.utc)
            out = rec_dir / f"{h_label}.{RAW_AUDIO_EXT}"
            key = audio_s3_key(out, sid)
            mp3_key = audio_s3_key(rec_dir / f"{h_label}.mp3", sid)  # legacy, para dedup

            if out.exists():
                continue
            if s3_object_matches(key, 0):
                s3_scan_register(key, sid, period_start)
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "uploaded",
                    s3_key=key,
                    reason="already_in_s3",
                    source_service="stream-daemon",
                )
                continue
            # Si esa hora ya fue procesada históricamente como MP3, no reprocesar
            # como .ts (evita detecciones duplicadas en el cutover del offload).
            if RAW_AUDIO_OFFLOAD and s3_object_matches(mp3_key, 0):
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "uploaded",
                    s3_key=mp3_key,
                    reason="already_in_s3_mp3",
                    source_service="stream-daemon",
                )
                continue

            if seg_mtimes is None:
                seg_mtimes = []
                for f in (STREAMS_ROOT / sid).glob("seg_*.ts"):
                    try:
                        if f.exists():
                            seg_mtimes.append((f, f.stat().st_mtime))
                    except OSError:
                        pass

            segs = [
                f for f, mt in sorted(seg_mtimes, key=lambda item: item[1])
                if h_start <= mt < h_end
            ]
            if len(segs) < 10:
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "skipped",
                    actual_seconds=len(segs) * SEG_DURATION,
                    reason=f"insufficient_segments_{len(segs)}",
                    source_service="stream-daemon",
                )
                if h_start == targets[0]:
                    low_coverage_by_hour[h_start].append((sid, len(segs) * SEG_DURATION))
                continue

            log.info(f"[{sid}] Grabando {h_label} ({len(segs)} segs)")
            coverage_upsert(
                sid, "audio", period_start, period_end, 3600, "pending",
                actual_seconds=len(segs) * SEG_DURATION,
                local_path=out,
                reason="building_mp3",
                source_service="stream-daemon",
            )

            with tempfile.TemporaryDirectory(prefix=f"mediadev_{sid}_") as tmpdir:
                concat = Path(tmpdir) / "segments.txt"
                concat.write_text("\n".join(concat_file_line(f) for f in segs) + "\n")
                # RAW_AUDIO_OFFLOAD: concat -c copy (sin recodificar) → .ts crudo.
                # El Destroyer hace el .ts→mp3. Si no, MP3 local con libmp3lame.
                codec_args = (["-c", "copy"] if RAW_AUDIO_OFFLOAD
                              else ["-c:a", "libmp3lame", "-b:a", "64k",
                                    "-ac", "1", "-ar", "22050"])
                result = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(concat)]
                    + codec_args + [str(out)],
                    capture_output=True, text=True,
                )

            if result.returncode != 0:
                out.unlink(missing_ok=True)
                err = (result.stderr or "ffmpeg failed")[-300:]
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "invalid",
                    actual_seconds=len(segs) * SEG_DURATION,
                    local_path=out,
                    reason="ffmpeg_failed",
                    last_error=err,
                    source_service="stream-daemon",
                )
                log.error(f"[{sid}] Fallo grabando {h_label}: {err}")
                if h_start == targets[0] and len(segs) * SEG_DURATION < RECORDING_ALERT_MIN_SECONDS:
                    low_coverage_by_hour[h_start].append((sid, len(segs) * SEG_DURATION))
                continue

            valid, duration, reason = validate_audio(out)
            size = out.stat().st_size if out.exists() else 0
            if not valid:
                out.unlink(missing_ok=True)
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "invalid",
                    actual_seconds=duration,
                    local_path=out,
                    reason=reason,
                    size_bytes=size,
                    source_service="stream-daemon",
                )
                log.error(f"[{sid}] {out.name} inválido: {reason}")
                if h_start == targets[0] and (duration or 0) < RECORDING_ALERT_MIN_SECONDS:
                    low_coverage_by_hour[h_start].append((sid, int(duration or 0)))
                continue

            coverage_upsert(
                sid, "audio", period_start, period_end, 3600, "validated",
                actual_seconds=duration,
                local_path=out,
                reason=reason,
                size_bytes=size,
                source_service="stream-daemon",
            )
            log.info(f"[{sid}] {out.name} OK ({size//1024}KB, {int(duration or 0)}s)")
            if h_start == targets[0] and (duration or 0) < RECORDING_ALERT_MIN_SECONDS:
                low_coverage_by_hour[h_start].append((sid, int(duration or 0)))

            ok, key, err = s3_upload_verified(out, sid)
            if ok:
                s3_scan_register(key, sid, period_start)
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "uploaded",
                    actual_seconds=duration,
                    local_path=out,
                    s3_key=key,
                    reason=reason,
                    size_bytes=size,
                    upload_attempts=1,
                    source_service="stream-daemon",
                )
            else:
                coverage_upsert(
                    sid, "audio", period_start, period_end, 3600, "upload_failed",
                    actual_seconds=duration,
                    local_path=out,
                    s3_key=key,
                    reason=reason,
                    size_bytes=size,
                    upload_attempts=S3_UPLOAD_RETRIES,
                    last_error=err,
                    source_service="stream-daemon",
                )
                continue

            if ok:
                old_files = sorted([*rec_dir.glob("*.mp3"), *rec_dir.glob("*.ts")],
                                   key=lambda f: f.name, reverse=True)[KEEP_MP3_COUNT:]
                for old in old_files:
                    old.unlink()

    for h_start, low_coverage in low_coverage_by_hour.items():
        send_recording_coverage_alert(h_start, low_coverage)

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

    now = utc_epoch()
    pg_write("DELETE FROM mediadev_metrics WHERE ts < %s",
             (now - METRICS_RETENTION_DAYS * 86400,))
    pg_write("DELETE FROM mediadev_events WHERE ts < %s",
             (now - EVENTS_RETENTION_DAYS * 86400,))

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
    log.info("MediaDEV Stream Daemon v2 (DB config + owned ffmpeg)")
    log.info(f"health={INTERVAL_HEALTH}s metrics={INTERVAL_METRICS}s "
             f"config_refresh={INTERVAL_CONFIG}s clean={INTERVAL_CLEAN}s loop={LOOP_SLEEP}s")

    # Cargar gateway activo
    load_gateway_conf()
    log.info(f"[gateway] socks5={GW_SOCKS5 or 'no configurado'} privoxy_port={GW_PRIVOXY_PORT}")

    # Cargar config de streams desde DB (→ cache si DB no disponible)
    cfgs = refresh_config()
    if cfgs:
        global STREAMS, TV_STREAMS
        STREAMS, TV_STREAMS = apply_stream_configs(cfgs)
    log.info(f"[config] {len(STREAMS)} streams ({len(TV_STREAMS)} TV)")

    state = init_state()

    # Spawn de todos los procesos ffmpeg
    log.info(f"Spawneando {len(STREAMS)} procesos ffmpeg")
    for sid in STREAMS:
        spawn_stream(sid)
    time.sleep(3)  # grace period inicial

    recover_pending_audio_uploads()

    running = [True]
    def _stop(sig, frame):
        running[0] = False
        log.info("Señal de parada — terminando procesos ffmpeg")
        for sid in list(_procs.keys()):
            stop_stream(sid)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    last = {k: 0 for k in ("health", "metrics", "record", "clean", "daily")}
    log.info(f"Monitoreando {len(STREAMS)} streams")
    log.info(f"PostgreSQL: {'configurado' if (PG_HOST and PG_PASS) else 'NO configurado (solo memoria)'}")

    while running[0]:
        now = time.time()

        if now - last["health"]  >= INTERVAL_HEALTH:
            do_health(state);        last["health"]  = now

        if now - last["metrics"] >= INTERVAL_METRICS:
            do_metrics(state);       last["metrics"] = now

        if now - last["record"]  >= INTERVAL_RECORD:
            do_record(state);        last["record"]  = now

        if now - last["clean"]   >= INTERVAL_CLEAN:
            do_cleanup(state);       last["clean"]   = now

        if now - last["daily"]   >= 3600:
            do_daily_reset(state);   last["daily"]   = now

        time.sleep(LOOP_SLEEP)

    log.info("Daemon detenido")

if __name__ == "__main__":
    main()
