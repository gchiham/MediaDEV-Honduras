#!/usr/bin/python3 -I
"""mediacap-vista — vista SANEADA de la config de captura para el usuario `vigia` (mediaCAP).

Corre como root (timer cada 5 min). Es lo único que lee el cache del daemon
(/etc/mediadev/stream_config.cache.json, que trae URLs con credenciales) y escribe
/var/lib/mediadev-vigia/vista.json (0640 root:vigia) con:

- URLs resumidas y redactadas, solo para mostrar (sin userinfo, sin rutas con credenciales,
  sin query string);
- `probar_url`: la URL completa SOLO si no lleva nada que parezca credencial (ni userinfo, ni
  query, ni ruta /live/<u>/<p>/) y el host no está en la lista de bloqueo local. Si no, null con
  el motivo: `probar_origen` no la prueba.

La lista de bloqueo (orígenes de cupo limitado o cliente único) vive fuera del repo, en
/etc/mediadev/vigia-bloqueados.txt (un sufijo de host por línea).
"""
import ipaddress, json, os, re, stat, sys, tempfile
from pathlib import Path
from urllib.parse import urlsplit

CACHE = Path(os.environ.get("STREAM_CACHE", "/etc/mediadev/stream_config.cache.json"))
BLOQUEADOS = Path(os.environ.get("VIGIA_BLOQUEADOS", "/etc/mediadev/vigia-bloqueados.txt"))
SALIDA = Path(os.environ.get("VIGIA_VISTA", "/var/lib/mediadev-vigia/vista.json"))
GRUPO = os.environ.get("VIGIA_GRUPO", "vigia")

_RE_LIVE = re.compile(r"/live/[^/\s]+/[^/\s]+/")


def resumir_url(url: str | None) -> str | None:
    """scheme://host[:puerto]/ruta-redactada[?…] — para mostrar, nunca para conectarse."""
    if not url:
        return None
    try:
        p = urlsplit(url)
        host = p.hostname or "?"
        puerto = f":{p.port}" if p.port else ""
    except ValueError:
        return "(URL inválida)"
    ruta = _RE_LIVE.sub("/live/***/***/", p.path or "")
    return f"{p.scheme}://{host}{puerto}{ruta}{'?…' if p.query else ''}"


def inseguro(path: Path, es_dir: bool = False) -> str | None:
    """Corre como root: no lee ni escribe donde otro usuario (p. ej. vigia) pueda meter mano."""
    if os.name != "posix":
        return None
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"{path}: {e.strerror}"
    if stat.S_ISLNK(st.st_mode):
        return f"{path}: es un enlace simbólico"
    if es_dir != stat.S_ISDIR(st.st_mode):
        return f"{path}: tipo inesperado"
    if st.st_uid != 0:
        return f"{path}: el dueño no es root"
    if st.st_mode & 0o022:
        return f"{path}: escribible por grupo u otros"
    return None


def bloqueados() -> list[str]:
    if not BLOQUEADOS.exists():
        return []
    motivo = inseguro(BLOQUEADOS)
    if motivo:
        sys.exit(f"lista de bloqueo no confiable ({motivo}): no se publica la vista")
    try:
        return [l.strip().lower() for l in BLOQUEADOS.read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def motivo_no_probar(url: str | None, lista: list[str]) -> str | None:
    if not url:
        return "sin URL"
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return "URL inválida"
    if p.scheme not in ("http", "https"):
        return f"esquema {p.scheme} no se prueba"
    if not host:
        return "URL sin host"
    if p.username or p.password:
        return "URL con credenciales"
    if p.query:
        return "URL con parámetros (posible token)"
    if _RE_LIVE.search(p.path or ""):
        return "ruta con credenciales"
    if any(host == h or host.endswith("." + h) for h in lista):
        return "origen de cupo limitado o cliente único: no se prueba con el daemon corriendo"
    try:
        if not ipaddress.ip_address(host).is_global:
            return "origen en red privada (cliente único): no se prueba"
    except ValueError:
        pass
    return None


def construir(cfgs: list[dict], lista: list[str]) -> dict:
    est = {}
    for c in cfgs:
        slug = c.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", slug):
            continue
        entrada = {"media_type": c.get("media_type"), "route": c.get("route"),
                   "hls_live_restart": c.get("hls_live_restart"),
                   "tiene_ffmpeg_extra": bool(c.get("ffmpeg_extra"))}
        for campo in ("stream_url", "fallback_url"):
            url = c.get(campo)
            motivo = motivo_no_probar(url, lista)
            entrada[campo] = {"resumen": resumir_url(url),
                              "probar_url": url if url and motivo is None else None,
                              "motivo": motivo if url else None} if url else None
        est[slug] = entrada
    return {"estaciones": est}


def main() -> None:
    for p, es_dir in ((CACHE, False), (SALIDA.parent, True)):
        motivo = inseguro(p, es_dir)
        if motivo:
            sys.exit(f"no confiable ({motivo}): no se publica la vista")
    try:
        cfgs = json.loads(CACHE.read_text())
    except (OSError, ValueError) as e:
        print(f"no se pudo leer {CACHE}: {e}", file=sys.stderr)
        sys.exit(1)
    datos = construir(cfgs, bloqueados())
    fd, tmp = tempfile.mkstemp(dir=SALIDA.parent, prefix=".vista-")    # nace 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(datos, f, ensure_ascii=False)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            import grp
            try:
                gid = grp.getgrnam(GRUPO).gr_gid
            except KeyError:
                sys.exit(f"no existe el grupo {GRUPO!r}: crear el usuario vigia antes de activar la vista")
            os.chown(tmp, 0, gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, SALIDA)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    main()
