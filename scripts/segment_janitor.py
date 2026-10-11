#!/usr/bin/env python3
"""segment_janitor.py — limpieza de segmentos HLS de mediaCAP según el espacio en disco.

Reemplaza al cron de root `find /var/www/streams -maxdepth 2 -name "seg_*.ts" ... -mmin +120 -delete`.
Ese cron borraba a las 2 h los segmentos TV de canal_5/6/10/tnh aunque no se hubieran subido
(audio TV irrecuperable en el incidente del 26-ago), porque su lista de excepciones era la del 14-jun.

Reparto de responsabilidades (un solo dueño por tipo de archivo, sin locks compartidos)
  radio  Este script borra seg_*.ts con más de RADIO_KEEP_HOURS (8 h). Ningún proceso lee
         segmentos de radio de esa edad: ffmpeg solo escribe el más nuevo y el daemon arma horas
         de las últimas AUTO_BACKFILL_HOURS (3 h). Las ventanas no se tocan, así que no puede
         haber un archivo en uso (se exige RADIO_KEEP_HOURS >= AUTO_BACKFILL_HOURS + 2 y se
         mantiene igual el chequeo de /proc como defensa extra).
  tv     Este script NO borra TV. Los TV pendientes son del uploader (los sube y borra, y en
         emergencia de disco los descarta él mismo: TV_SHED_MODE). Aquí solo se mide el margen y
         se alerta (TV_BACKLOG, DISK_LOW, DISK_EMERGENCY).

JANITOR_MODE=observe (default) registra lo que borraría sin borrar; enforce borra.

Reglas de seguridad (ante la duda, no borrar)
  - Solo archivos regulares <STREAMS_ROOT>/<slug>/seg_<n>.ts. Sin symlinks ni recursión. Los
    directorios que empiezan con "_" (_invalid, _tv_audio) no se tocan.
  - El tipo radio|tv de cada slug sale de media_sources (PostgreSQL). Si PG no responde, se usa
    solo la caché local escrita por este script, si es válida y tiene menos de CACHE_MAX_AGE_H. Si
    no hay clasificación fiable no se borra nada, se alerta y se sale con código 1.
  - Un directorio cuyo slug no está clasificado no se toca.
  - Nunca se borran los KEEP_NEWEST segmentos más nuevos de cada estación ni los referenciados
    por su index.m3u8.
  - Nunca se borra un archivo abierto por algún proceso (snapshot de /proc/*/fd, refrescado cada
    OPEN_RECHECK_EVERY borrados). Si /proc no se puede leer de forma completa, no se borra nada.

Modo simulación: --dry-run no borra, no escribe caché ni estado y no manda Telegram; imprime lo
que haría. --simulate-free-gb N (solo junto con --dry-run) fuerza el espacio libre para
ejercitar las alertas de disco.

Corre como oneshot desde mediadev-segment-janitor.timer (cada 10 min). Logs a stdout (journald).
"""
import argparse
import json
import logging
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GB = 1024 ** 3
SEG_RE = re.compile(r"^seg_(\d+)\.ts$")
VALID_TYPES = ("radio", "tv")
SEG_SECONDS = 4

log = logging.getLogger("segment_janitor")


def _env_num(name, default):
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else float(default)


@dataclass
class Config:
    streams_root: Path = Path("/var/www/streams")
    radio_keep_hours: float = 8
    daemon_backfill_hours: float = 3   # AUTO_BACKFILL_HOURS del daemon: radio más nueva que esto se lee
    enforce: bool = False              # JANITOR_MODE=enforce para borrar; si no, solo registra
    emergency_free_gb: float = 15
    warn_hours: float = 6           # alerta si, al ritmo actual, faltan menos de N h para la emergencia
    backlog_warn_min: float = 30    # alerta si el pendiente TV más viejo supera N min (uploader no drena)
    keep_newest: int = 12
    open_recheck_every: int = 25
    cache_path: Path = Path("/var/lib/mediadev/segment_janitor_types.json")
    cache_max_age_h: float = 72
    state_path: Path = Path("/var/lib/mediadev/segment_janitor_state.json")
    lock_path: Path = Path("/run/lock/mediadev-segment-janitor.lock")
    crit_repeat_min: float = 60
    warn_repeat_min: float = 180
    daemon_tv_keep_hours: float = 24  # KEEP_SEG_HOURS_TV del daemon (solo para el reporte de margen)

    @classmethod
    def from_env(cls):
        c = cls()
        c.streams_root = Path(os.environ.get("STREAMS_ROOT", c.streams_root))
        c.radio_keep_hours = _env_num("RADIO_KEEP_HOURS", c.radio_keep_hours)
        c.daemon_backfill_hours = _env_num("AUTO_BACKFILL_HOURS", c.daemon_backfill_hours)
        c.enforce = os.environ.get("JANITOR_MODE", "observe").strip().lower() == "enforce"
        c.emergency_free_gb = _env_num("EMERGENCY_FREE_GB", c.emergency_free_gb)
        c.warn_hours = _env_num("WARN_HOURS_TO_EMERGENCY", c.warn_hours)
        c.backlog_warn_min = _env_num("TV_BACKLOG_WARN_MIN", c.backlog_warn_min)
        c.keep_newest = int(_env_num("KEEP_NEWEST", c.keep_newest))
        c.cache_path = Path(os.environ.get("JANITOR_CACHE", c.cache_path))
        c.cache_max_age_h = _env_num("JANITOR_CACHE_MAX_AGE_H", c.cache_max_age_h)
        c.state_path = Path(os.environ.get("JANITOR_STATE", c.state_path))
        c.daemon_tv_keep_hours = _env_num("KEEP_SEG_HOURS_TV", c.daemon_tv_keep_hours)
        return c


# ── Clasificación radio / tv ──────────────────────────────────────────────────
def build_types(rows):
    """[(slug, media_type)] -> {slug: type}. Un slug con tipo inválido o con dos tipos distintos
    se descarta (queda sin clasificar, así que no se toca)."""
    types, bad = {}, set()
    for slug, mtype in rows:
        if not slug:
            continue
        if mtype not in VALID_TYPES or (slug in types and types[slug] != mtype):
            bad.add(slug)
            continue
        types[slug] = mtype
    for slug in bad:
        types.pop(slug, None)
    return types


def load_types_from_db(timeout=5):
    if not os.environ.get("PG_HOST"):
        return None, "PG_HOST no definido"
    try:
        import psycopg2
    except ImportError:
        return None, "psycopg2 no disponible"
    try:
        conn = psycopg2.connect(
            host=os.environ["PG_HOST"], port=int(os.environ.get("PG_PORT", "25060")),
            dbname=os.environ.get("PG_DB"), user=os.environ.get("PG_USER"),
            password=os.environ.get("PG_PASS"), sslmode="require", connect_timeout=timeout)
    except Exception as e:
        return None, f"conexión PG falló: {e}"
    try:
        with conn.cursor() as cur:
            # Sin filtro is_enabled: las estaciones deshabilitadas también dejan segmentos.
            cur.execute("SELECT slug, media_type FROM media_sources WHERE slug IS NOT NULL")
            types = build_types(cur.fetchall())
    except Exception as e:
        return None, f"query PG falló: {e}"
    finally:
        conn.close()
    return (types or None), ("PostgreSQL" if types else "PostgreSQL sin filas válidas")


def read_cache(path, max_age_h, now):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None, "caché inexistente"
    except Exception as e:
        return None, f"caché ilegible: {e}"
    gen, types = data.get("generated_at"), data.get("types")
    if not isinstance(gen, (int, float)) or not isinstance(types, dict) or not types:
        return None, "caché con estructura inválida"
    if any(v not in VALID_TYPES for v in types.values()):
        return None, "caché con tipos inválidos"
    age_h = (now - gen) / 3600
    if age_h < -0.1 or age_h > max_age_h:
        return None, f"caché vencida ({age_h:.1f} h, máx {max_age_h:g} h)"
    return types, f"caché local de hace {age_h:.1f} h"


def write_cache(path, types, now):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generated_at": now, "types": types}, indent=1, sort_keys=True))
    os.replace(tmp, path)


# ── Archivos abiertos ─────────────────────────────────────────────────────────
def open_files_under(root):
    """Paths bajo root abiertos por algún proceso, o None si /proc no se puede leer completo."""
    prefix = str(root).rstrip("/") + "/"
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    found = set()
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except (FileNotFoundError, ProcessLookupError):
            continue                      # el proceso terminó entre listar y leer
        except PermissionError:
            return None                   # sin root no hay garantía: no borrar
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith(prefix):
                found.add(target[:-10] if target.endswith(" (deleted)") else target)
    return found


def disk_free_bytes(root):
    st = os.statvfs(root)
    return st.f_bavail * st.f_frsize


# ── Escaneo ───────────────────────────────────────────────────────────────────
def playlist_refs(stream_dir):
    try:
        lines = (Path(stream_dir) / "index.m3u8").read_text().splitlines()
    except OSError:
        return set()
    return {Path(l.strip()).name for l in lines if l.strip().endswith(".ts") and not l.startswith("#")}


def scan(root, types, keep_newest):
    """{slug: {type, files:[(idx, path, size, mtime)], protected:set(path)}} + dirs sin clasificar."""
    stations, unknown = {}, {}
    with os.scandir(root) as it:
        for entry in it:
            if entry.name.startswith("_") or not entry.is_dir(follow_symlinks=False):
                continue
            files = []
            try:
                with os.scandir(entry.path) as inner:
                    for f in inner:
                        m = SEG_RE.match(f.name)
                        if not m or not f.is_file(follow_symlinks=False):
                            continue
                        try:
                            st = f.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        files.append((int(m.group(1)), f.path, st.st_size, st.st_mtime))
            except OSError as e:
                log.warning(f"[{entry.name}] no se pudo listar: {e}")
                continue
            if not files:
                continue
            mtype = types.get(entry.name) if types else None
            if mtype is None:
                unknown[entry.name] = len(files)
                continue
            files.sort()
            refs = playlist_refs(entry.path)
            protected = {p for _, p, _, _ in files[-keep_newest:]} if keep_newest > 0 else set()
            protected |= {p for _, p, _, _ in files if os.path.basename(p) in refs}
            stations[entry.name] = {"type": mtype, "files": files, "protected": protected}
    return stations, unknown


def tv_ingest_bytes_per_hour(stations):
    """Consumo TV estimado: tamaño medio de los segmentos más nuevos ya cerrados × 900/h."""
    total = 0.0
    for st in stations.values():
        if st["type"] != "tv":
            continue
        done = [s for _, _, s, _ in st["files"][-7:-1]]  # el último puede estar escribiéndose
        if done:
            total += sum(done) / len(done) * (3600 / SEG_SECONDS)
    return total


# ── Alertas ───────────────────────────────────────────────────────────────────
def telegram_send(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        log.warning("alerta NO enviada (sin TG_TOKEN/TG_CHAT): " + text.replace("\n", " | "))
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception as e:
        log.warning(f"alerta Telegram falló: {e}")


class Alerts:
    """Dedupe por clave: repite cada N min mientras siga activa y avisa una vez al resolverse."""

    def __init__(self, cfg, now, dry_run, sink=telegram_send):
        self.cfg, self.now, self.dry, self.sink = cfg, now, dry_run, sink
        self.active, self.sent = {}, []
        try:
            self.state = json.loads(Path(cfg.state_path).read_text())
        except Exception:
            self.state = {}
        self.state.setdefault("alerts", {})

    def raise_(self, key, level, text):
        self.active[key] = (level, text)

    def flush(self):
        prev = self.state["alerts"]
        for key, (level, text) in self.active.items():
            repeat = (self.cfg.crit_repeat_min if level == "CRIT" else self.cfg.warn_repeat_min) * 60
            last = prev.get(key, {}).get("sent", 0)
            msg = f"{'🚨' if level == 'CRIT' else '⚠️'} mediaCAP segment-janitor: {text}"
            log.log(logging.ERROR if level == "CRIT" else logging.WARNING, f"ALERTA {key}: {text}")
            if self.now - last >= repeat:
                self._send(msg)
                prev[key] = {"sent": self.now, "level": level}
        for key in [k for k in prev if k not in self.active]:
            self._send(f"✅ mediaCAP segment-janitor: resuelto {key}")
            log.info(f"alerta resuelta: {key}")
            prev.pop(key)
        if not self.dry:
            try:
                path = Path(self.cfg.state_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state))
                os.replace(tmp, path)
            except Exception as e:
                log.warning(f"no se pudo guardar el estado de alertas: {e}")

    def _send(self, msg):
        self.sent.append(msg)
        if self.dry:
            log.info("[dry-run] enviaría alerta: " + msg.replace("\n", " | "))
        else:
            self.sink(msg)


# ── Núcleo ────────────────────────────────────────────────────────────────────
def run(cfg, *, now=None, dry_run=False, simulate_free_gb=None,
        load_db=load_types_from_db, free_fn=disk_free_bytes, open_fn=open_files_under,
        unlink=os.unlink, alert_sink=telegram_send):
    now = time.time() if now is None else now
    # Ruta canónica: /proc/*/fd devuelve paths resueltos; si la raíz tuviera un symlink, la
    # comparación de "archivo en uso" fallaría en silencio.
    cfg.streams_root = Path(os.path.realpath(cfg.streams_root))
    alerts = Alerts(cfg, now, dry_run, alert_sink)
    res = {"dry_run": dry_run, "deleted": {"radio": 0, "tv": 0}, "freed_bytes": 0,
           "skipped_open": 0, "exit": 0}

    # 1) clasificación
    types, source = load_db()
    if types:
        if not dry_run:
            try:
                write_cache(cfg.cache_path, types, now)
            except Exception as e:
                log.warning(f"no se pudo escribir la caché: {e}")
    else:
        log.warning(f"clasificación desde PG no disponible ({source}); probando caché")
        types, source = read_cache(cfg.cache_path, cfg.cache_max_age_h, now)
    res["classification"] = source

    # 2) escaneo + métricas (también en fail-safe, para alertar con datos)
    stations, unknown = scan(cfg.streams_root, types or {}, cfg.keep_newest)
    for slug, n in sorted(unknown.items()):
        log.info(f"[{slug}] sin clasificar ({n} seg_*.ts): no se toca")
    res["unknown_dirs"] = unknown
    free = simulate_free_gb * GB if simulate_free_gb is not None else free_fn(cfg.streams_root)
    res["free_gb_start"] = round(free / GB, 2)

    if not types:
        alerts.raise_("FAILSAFE_NO_CLASSIFICATION", "CRIT",
                      f"sin clasificación radio/tv fiable ({source}). No se borró nada. "
                      f"Disco libre {free / GB:.1f} GB.")
        res["exit"] = 1
        _report(cfg, stations, free, now, res, alerts)
        alerts.flush()
        return res

    # Invariante de no-concurrencia: la radio que borramos es más vieja que cualquier hora
    # que el daemon todavía pueda armar. Si la config la rompe, no se borra nada.
    if cfg.radio_keep_hours < cfg.daemon_backfill_hours + 2:
        alerts.raise_("FAILSAFE_RADIO_WINDOW", "CRIT",
                      f"RADIO_KEEP_HOURS={cfg.radio_keep_hours:g} se solapa con la ventana del daemon "
                      f"(AUTO_BACKFILL_HOURS={cfg.daemon_backfill_hours:g} + 2). No se borró nada.")
        res["exit"] = 1
        _report(cfg, stations, free, now, res, alerts)
        alerts.flush()
        return res

    radio_cut = now - cfg.radio_keep_hours * 3600
    radio_cands = [(slug, f) for slug, st in stations.items() if st["type"] == "radio"
                   for f in st["files"] if f[3] < radio_cut and f[1] not in st["protected"]]
    really_delete = cfg.enforce and not dry_run
    tag = "DELETE" if really_delete else "WOULD DELETE"
    res["mode"] = "enforce" if really_delete else ("dry-run" if dry_run else "observe")

    if radio_cands:
        open_set = open_fn(cfg.streams_root)
        if open_set is None:
            alerts.raise_("FAILSAFE_OPEN_FILES", "CRIT",
                          "no se pudo leer /proc para detectar archivos en uso. No se borró nada.")
            res["exit"] = 1
            _report(cfg, stations, free, now, res, alerts)
            alerts.flush()
            return res
        try:
            for n, (slug, f) in enumerate(radio_cands, 1):
                idx, path, size, mtime = f
                if n % cfg.open_recheck_every == 0:
                    open_set = open_fn(cfg.streams_root)
                    if open_set is None:
                        raise RuntimeError("lectura de /proc falló a mitad de la limpieza")
                if path in open_set:
                    res["skipped_open"] += 1
                    log.info(f"SKIP en uso radio {slug} {os.path.basename(path)}")
                    continue
                log.info(f"{tag} radio {slug} {os.path.basename(path)} size={size} "
                         f"age={(now - mtime) / 60:.0f}min reason=>{cfg.radio_keep_hours:g}h")
                if really_delete:
                    try:
                        unlink(path)
                    except FileNotFoundError:
                        continue          # lo borró el daemon (misma regla de 8 h): OK
                    except OSError as e:
                        log.warning(f"no se pudo borrar {path}: {e}")
                        continue
                res["deleted"]["radio"] += 1
                res["freed_bytes"] += size
        except RuntimeError as e:
            alerts.raise_("FAILSAFE_OPEN_FILES", "CRIT", f"{e}. Limpieza interrumpida.")
            res["exit"] = 1
        if really_delete and simulate_free_gb is None:
            free = free_fn(cfg.streams_root)

    res["emergency"] = free < cfg.emergency_free_gb * GB
    if res["emergency"]:
        alerts.raise_("DISK_EMERGENCY", "CRIT",
                      f"disco libre {free / GB:.1f} GB < {cfg.emergency_free_gb:g} GB. Este limpiador no "
                      "borra TV: el descarte lo hace video-segment-uploader según TV_SHED_MODE. "
                      "Radios intactas.")

    res["free_gb_end"] = round(free / GB, 2)
    _report(cfg, stations, free, now, res, alerts)
    alerts.flush()
    res["alerts_sent"] = alerts.sent
    return res


def _report(cfg, stations, free, now, res, alerts):
    """Margen de operación + alertas preventivas + línea de resumen."""
    ingest = tv_ingest_bytes_per_hour(stations)
    headroom = max(0.0, free - cfg.emergency_free_gb * GB)
    hours_to_emergency = headroom / ingest if ingest else math.inf
    oldest_tv_min, tv_backlog = 0.0, 0
    for st in stations.values():
        if st["type"] == "tv":
            pend = [f for f in st["files"] if f[1] not in st["protected"]]
            tv_backlog += sum(f[2] for f in pend)
            if pend:
                oldest_tv_min = max(oldest_tv_min, (now - min(f[3] for f in pend)) / 60)
    margin = {
        "tv_ingest_gb_per_h": round(ingest / GB, 2),
        "tv_backlog_gb": round(tv_backlog / GB, 2),
        "oldest_tv_pending_min": round(oldest_tv_min, 1),
        "headroom_gb": round(headroom / GB, 1),
        "hours_to_emergency_if_s3_down": None if math.isinf(hours_to_emergency) else round(hours_to_emergency, 1),
        "daemon_tv_cap_need_gb": round(ingest * cfg.daemon_tv_keep_hours / GB, 1),
    }
    res["margin"] = margin
    if oldest_tv_min > cfg.backlog_warn_min:
        alerts.raise_("TV_BACKLOG", "WARN",
                      f"el uploader no drena: pendiente TV más viejo de {oldest_tv_min:.0f} min "
                      f"({tv_backlog / GB:.1f} GB). ¿S3 caído?")
        if hours_to_emergency < cfg.warn_hours:
            alerts.raise_("DISK_LOW", "WARN",
                          f"al ritmo TV actual ({ingest / GB:.1f} GB/h) faltan ~{hours_to_emergency:.1f} h "
                          f"para la emergencia ({cfg.emergency_free_gb:g} GB). Libre {free / GB:.1f} GB.")
    log.info("RESUMEN " + json.dumps(res, ensure_ascii=False, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="no borra ni escribe nada; muestra lo que haría")
    ap.add_argument("--simulate-free-gb", type=float, help="fuerza el espacio libre (solo con --dry-run)")
    args = ap.parse_args(argv)
    if args.simulate_free_gb is not None and not args.dry_run:
        ap.error("--simulate-free-gb solo se permite junto con --dry-run")
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    lock_fh = None
    if not args.dry_run:
        import fcntl  # solo Linux; importado aquí para que los tests corran en cualquier SO
        cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(cfg.lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("otra instancia en curso; salgo")
            return 0
    res = run(cfg, dry_run=args.dry_run, simulate_free_gb=args.simulate_free_gb)
    return res["exit"]


if __name__ == "__main__":
    sys.exit(main())
