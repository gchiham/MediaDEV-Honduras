#!/usr/bin/python3 -I
"""mediacap-auditor-inventario — inventario de SOLO LECTURA de mediaCAP para el auditor de código.

Corre como root desde un timer de systemd con ProtectSystem=strict: el kernel deja todo el
sistema de archivos en solo lectura salvo SALIDA_DIR. No recibe entradas (ni argumentos ni red):
ejecuta una lista fija de comandos de lectura y escribe un único JSON saneado.

El usuario `auditor` no ejecuta nada en mediaCAP: su ForceCommand es `cat` de ese JSON.

Nunca lee el contenido de /etc/*.env ni de /etc/mediadev/ (solo dueño, modo, tamaño y fecha).
Crontabs y units sí se incluyen, redactados.

Uso local (pruebas):  python3 mediacap_inventario.py --salida /tmp/inv.json
"""
import argparse
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = 1
SALIDA = Path(os.environ.get("AUDITOR_INVENTARIO", "/var/lib/mediacap-auditor/inventario.json"))
GRUPO_LECTOR = "auditor"

REPOS_GIT = [Path("/opt/media-ai")]
# Código desplegado sin .git: se compara por hash con su repo (destroyer/cap).
SIN_GIT = [Path("/opt/destroyer")]
EXCLUIR_DIRS = {"venv", "__pycache__", ".git", "node_modules"}
EXT_CODIGO = {".py", ".sh", ".service", ".timer", ".conf", ".json", ".sql"}
MAX_ARCHIVOS, MAX_PROFUNDIDAD, MAX_BYTES_HASH = 500, 4, 5_000_000

UNIDADES = ["stream-daemon*", "video-segment-uploader*", "mediadev-*", "mediacap-*",
            "nginx.service", "privoxy.service", "wg-quick@*"]
CRON = [Path("/var/spool/cron/crontabs/root"), Path("/etc/crontab"), Path("/etc/cron.d")]
# Solo metadatos: contienen secretos y llaves privadas. (dir, ¿filtrar por PATRON_SECRETO?)
SECRETOS = [(Path("/etc"), True), (Path("/etc/mediadev"), False)]
PATRON_SECRETO = re.compile(r"\.(env|pem|key)$|^gateway\.conf$")
DISCO = [Path("/"), Path("/var/www/streams")]
DU = [Path("/var/www/streams"), Path("/var/log/streams")]
VENVS = ["/opt/*/venv", "/opt/*/*/venv"]

MAX_TEXTO = 20_000           # por unit/crontab
MAX_SALIDA = 2_000_000       # todo el JSON


# ── redacción ────────────────────────────────────────────────────────────────

_REDACCIONES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<llave privada>"),
    # Antes que los patrones específicos: así `X_TOKEN=<valor>` queda igual en el server y en el
    # repo (que lleva placeholders), sea cual sea el formato del valor.
    (re.compile(r"(\b[\w-]*(?:token|secret|passw(?:or)?d|pass|api[_-]?key|access[_-]?key)[\w-]*"
                r"\s*[=:]\s*)[\"']?[^\s\"'&]+", re.I), r"\1***"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "<token telegram>"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "<aws key id>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<api key>"),
    (re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
    (re.compile(r"(/live/)[^/\s]+/[^/\s]+/"), r"\1***/***/"),
    (re.compile(r"((?:authorization|cookie|x-api-key)\s*[:=]\s*)[^\r\n\"']+", re.I), r"\1***"),
    (re.compile(r"((?<![A-Za-z0-9_])(?:token|key|sig|signature|auth|hash)=)[^&\s\"']+", re.I), r"\1***"),
]


def redactar(texto: str) -> str:
    for patron, reemplazo in _REDACCIONES:
        texto = patron.sub(reemplazo, texto)
    return texto


# ── utilidades ───────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """Comando fijo, sin shell, entorno mínimo. Nunca lanza: devuelve (código, salida)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
                                "GIT_OPTIONAL_LOCKS": "0"})
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, f"timeout {timeout}s"
    except OSError as e:
        return 127, str(e)


def meta(p: Path) -> dict:
    st = p.lstat()
    try:
        import grp, pwd
        dueno, grupo = pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name
    except (ImportError, KeyError):
        dueno, grupo = str(st.st_uid), str(st.st_gid)
    return {"ruta": str(p), "dueno": dueno, "grupo": grupo, "modo": oct(stat.S_IMODE(st.st_mode))[2:],
            "bytes": st.st_size, "mtime_utc": _utc(st.st_mtime)}


def _utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def sha256(p: Path) -> str | None:
    if p.stat().st_size > MAX_BYTES_HASH:
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


def leer_texto(p: Path) -> str:
    with open(p, "rb") as f:
        datos = f.read(MAX_TEXTO + 1)
    texto = redactar(datos[:MAX_TEXTO].decode(errors="replace"))
    return texto + ("\n<truncado>" if len(datos) > MAX_TEXTO else "")


# ── secciones ────────────────────────────────────────────────────────────────

def sistema(run=run) -> dict:
    os_rel = {}
    try:
        for linea in Path("/etc/os-release").read_text().splitlines():
            k, _, v = linea.partition("=")
            if k in ("PRETTY_NAME", "VERSION_ID"):
                os_rel[k] = v.strip('"')
    except OSError:
        pass
    _, kernel = run(["uname", "-r"])
    _, mem = run(["free", "-m"])
    return {"host": socket.gethostname(), "os": os_rel, "kernel": kernel.strip(),
            "cpus": os.cpu_count(), "loadavg": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "memoria_mb": mem.strip().splitlines()[1:2]}


def versiones(run=run) -> dict:
    out = {}
    for nombre, cmd in (("python3", ["python3", "--version"]), ("ffmpeg", ["ffmpeg", "-version"]),
                        ("ffprobe", ["ffprobe", "-version"]), ("streamlink", ["streamlink", "--version"]),
                        ("git", ["git", "--version"])):
        rc, txt = run(cmd, timeout=10)
        out[nombre] = txt.strip().splitlines()[0][:200] if rc == 0 and txt.strip() else None
    return out


def git(repos=REPOS_GIT, run=run) -> list[dict]:
    out = []
    for r in repos:
        if not (r / ".git").exists():
            out.append({"ruta": str(r), "error": "sin .git"})
            continue
        base = ["git", "-C", str(r), "--no-optional-locks"]     # nunca escribe el índice
        _, head = run(base + ["rev-parse", "HEAD"])
        _, rama = run(base + ["rev-parse", "--abbrev-ref", "HEAD"])
        _, estado = run(base + ["status", "--porcelain=v1", "--untracked-files=normal"])
        _, filemode = run(base + ["config", "--get", "core.fileMode"])
        out.append({"ruta": str(r), "head": head.strip(), "rama": rama.strip(),
                    "core_filemode": filemode.strip() or None,
                    "cambios": [redactar(l) for l in estado.splitlines()][:MAX_ARCHIVOS]})
    return out


def arbol_hashes(raices=SIN_GIT) -> list[dict]:
    """sha256 del código desplegado sin .git. Recorrido acotado: profundidad, cantidad y tamaño."""
    out = []
    for raiz in raices:
        archivos, truncado = [], False
        if raiz.is_dir():
            for dirpath, dirnames, filenames in os.walk(raiz):
                prof = len(Path(dirpath).relative_to(raiz).parts)
                dirnames[:] = sorted(d for d in dirnames
                                     if d not in EXCLUIR_DIRS and prof < MAX_PROFUNDIDAD)
                for nombre in sorted(filenames):
                    p = Path(dirpath) / nombre
                    if p.suffix not in EXT_CODIGO or p.is_symlink() or PATRON_SECRETO.search(nombre):
                        continue
                    if len(archivos) >= MAX_ARCHIVOS:
                        truncado = True
                        break
                    st = p.stat()
                    archivos.append({"ruta": p.relative_to(raiz).as_posix(), "sha256": sha256(p),
                                     "bytes": st.st_size, "mtime_utc": _utc(st.st_mtime)})
        out.append({"raiz": str(raiz), "existe": raiz.is_dir(), "archivos": archivos, "truncado": truncado})
    return out


def systemd(run=run) -> list[dict]:
    _, lista = run(["systemctl", "list-unit-files", "--no-legend", "--plain"] + UNIDADES)
    _, activas = run(["systemctl", "list-units", "--all", "--no-legend", "--plain"] + UNIDADES)
    nombres = sorted({l.split()[0] for l in (lista + "\n" + activas).splitlines()
                      if l.strip() and "@." not in l.split()[0]})
    out = []
    for u in nombres:
        _, show = run(["systemctl", "show", u, "-p", "ActiveState,SubState,UnitFileState,FragmentPath,"
                       "DropInPaths,ExecMainStartTimestamp,NRestarts"])
        props = dict(l.split("=", 1) for l in show.splitlines() if "=" in l)
        archivos = [p for p in [props.get("FragmentPath", "")] + props.get("DropInPaths", "").split() if p]
        out.append({"unidad": u, **{k: v for k, v in props.items() if k not in ("FragmentPath", "DropInPaths")},
                    "archivos": [{**meta(Path(p)), "sha256": sha256(Path(p)), "texto": leer_texto(Path(p))}
                                 for p in archivos if Path(p).is_file()]})
    return out


def cron(rutas=CRON) -> list[dict]:
    out = []
    for r in rutas:
        for p in (sorted(r.iterdir()) if r.is_dir() else [r]):
            if p.is_file():
                out.append({**meta(p), "sha256": sha256(p), "texto": leer_texto(p)})
    return out


def secretos_meta(dirs=SECRETOS) -> list[dict]:
    """Dueño/modo/tamaño de archivos sensibles para auditar permisos. NUNCA el contenido."""
    out = []
    for d, filtrar in dirs:
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and (not filtrar or PATRON_SECRETO.search(p.name)):
                    out.append(meta(p))
    return out


def disco(run=run) -> dict:
    out = {"df": [], "du_kb": {}}
    for p in DISCO:
        if p.exists():
            st = os.statvfs(p)
            out["df"].append({"ruta": str(p), "libre_gb": round(st.f_bavail * st.f_frsize / 1e9, 1),
                              "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1),
                              "inodos_libres": st.f_favail, "inodos_total": st.f_files})
    for p in DU:
        if p.is_dir():
            rc, txt = run(["du", "-k", "--max-depth=1", str(p)], timeout=30)
            out["du_kb"][str(p)] = ({l.split("\t", 1)[1]: int(l.split("\t", 1)[0])
                                     for l in txt.splitlines() if "\t" in l} if rc == 0 else txt[:200])
    return out


def procesos(run=run) -> dict:
    """Por nombre de comando, sin argumentos (los args de ffmpeg traen URLs con credenciales)."""
    _, txt = run(["ps", "-eo", "pid=,ppid=,rss=,comm="])
    por_comando, huerfanos = {}, 0
    for l in txt.splitlines():
        partes = l.split(None, 3)
        if len(partes) != 4 or not partes[0].isdigit():
            continue
        _, ppid, rss, comm = partes
        c = por_comando.setdefault(comm, {"n": 0, "rss_mb": 0.0})
        c["n"] += 1
        c["rss_mb"] = round(c["rss_mb"] + int(rss) / 1024, 1)
        if comm == "ffmpeg" and ppid == "1":
            huerfanos += 1
    top = dict(sorted(por_comando.items(), key=lambda kv: -kv[1]["rss_mb"])[:25])
    return {"ffmpeg": por_comando.get("ffmpeg", {"n": 0}).get("n", 0), "ffmpeg_ppid1": huerfanos,
            "top_rss": top}


def venvs(patrones=VENVS) -> list[dict]:
    """Paquetes instalados por venv, leyendo nombres de *.dist-info (sin arrancar pip)."""
    import glob
    out = []
    for v in sorted({p for pat in patrones for p in glob.glob(pat)}):
        paquetes = sorted(Path(d).name[:-len(".dist-info")]
                          for d in glob.glob(f"{v}/lib/python3*/site-packages/*.dist-info"))
        out.append({"ruta": v, "paquetes": paquetes})
    return out


SECCIONES = {"sistema": sistema, "versiones": versiones, "git": git, "codigo_sin_git": arbol_hashes,
             "systemd": systemd, "cron": cron, "secretos_meta": secretos_meta, "disco": disco,
             "procesos": procesos, "venvs": venvs}


def recolectar(secciones=SECCIONES) -> dict:
    """Cada sección falla sola: un error queda registrado y el resto del inventario sigue."""
    inv = {"version": VERSION, "generado_utc": _utc(time.time()), "errores": {}}
    for nombre, fn in secciones.items():
        t0 = time.monotonic()
        try:
            inv[nombre] = fn()
        except Exception as e:                       # noqa: BLE001 — inventario parcial > ninguno
            inv[nombre] = None
            inv["errores"][nombre] = redactar(f"{type(e).__name__}: {e}")[:300]
        inv.setdefault("duracion_s", {})[nombre] = round(time.monotonic() - t0, 2)
    return inv


def escribir(inv: dict, salida: Path, grupo: str | None = GRUPO_LECTOR) -> None:
    """Atómico (tmp + rename en el mismo dir) y 0640: root escribe, solo el grupo lector lee."""
    datos = json.dumps(inv, ensure_ascii=False, indent=1)
    if len(datos) > MAX_SALIDA:
        inv = {k: inv[k] for k in ("version", "generado_utc", "errores")}
        inv["errores"]["_salida"] = f"inventario de {len(datos)} bytes > {MAX_SALIDA}: descartado"
        datos = json.dumps(inv, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=salida.parent, prefix=".inventario-")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o640)
        if grupo and hasattr(os, "fchown"):
            import grp
            os.fchown(fd, 0, grp.getgrnam(grupo).gr_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(datos)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, salida)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Inventario de solo lectura de mediaCAP.")
    ap.add_argument("--salida", type=Path, default=SALIDA)
    ap.add_argument("--sin-grupo", action="store_true", help="pruebas locales: no hace chown")
    a = ap.parse_args(argv)
    inv = recolectar()
    escribir(inv, a.salida, None if a.sin_grupo else GRUPO_LECTOR)
    print(f"inventario {a.salida}: {len(inv['errores'])} secciones con error "
          f"({', '.join(inv['errores']) or 'ninguna'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
