#!/usr/bin/python3 -I
"""mediacap-ro — comandos de SOLO LECTURA para el agente vigía (corre en mediaCAP).

Se instala como forced command de la llave del usuario `vigia`:
    restrict,from="<IP privada de mediaAPP>",command="/usr/local/bin/mediacap-ro" ssh-ed25519 ...
OpenSSH pone lo que pidió el cliente en SSH_ORIGINAL_COMMAND; acá se parsea, se valida contra
una lista cerrada de subcomandos y se responde JSON por stdout. Nada de este archivo escribe
(salvo temporales propios en /tmp), reinicia ni borra.

Config de captura: SOLO la vista saneada que genera mediacap_vista.py (root). El usuario vigia
no necesita ni debe poder leer el cache del daemon, que trae URLs con credenciales.

Uso local (pruebas):  mediacap_ro.py estado_estacion radio_america
"""
import ipaddress, json, os, re, shlex, socket, stat, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

STREAMS_ROOT = Path(os.environ.get("STREAMS_ROOT", "/var/www/streams"))
ERR_DIR      = Path(os.environ.get("FFMPEG_ERR_DIR", "/var/log/streams/ffmpeg"))
VISTA        = Path(os.environ.get("VIGIA_VISTA", "/var/lib/mediadev-vigia/vista.json"))
GATEWAY_CONF = Path("/etc/mediadev/gateway.conf")
CHANGES_LOG  = Path("/opt/media-ai/CHANGES.log")
TV_AUDIO     = STREAMS_ROOT / "_tv_audio"
LOCK         = Path(os.environ.get("MEDIACAP_RO_LOCK", "/var/lib/mediacap-ro/lock"))   # único dir escribible de vigia
STALE_SECS   = 90

SERVICIOS = ["stream-daemon", "video-segment-uploader", "mediadev-gateway-api",
             "mediadev-health-engine", "mediadev-monitor", "nginx", "privoxy",
             "wg-quick@wg0", "mediadev-segment-janitor.timer", "mediadev-ffmpeg-reaper.timer"]

SID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
SEG_RE = re.compile(r"^seg_\d{1,9}\.ts$")
MAX_SALIDA = 20000
MAX_REDIRECCIONES = 3


# ── redacción ────────────────────────────────────────────────────────────────

_RE_LIVE = re.compile(r"(/live/)[^/\s]+/[^/\s]+/")
_RE_QS = re.compile(r"((?<![A-Za-z0-9_])(?:token|key|pass(?:word)?|auth|sig|signature|user|secret|hash|st|e)=)[^&\s\"']+", re.I)
_RE_USERINFO = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")
_RE_CABECERA = re.compile(r"((?:authorization|cookie|x-api-key|proxy-authorization)\s*[:=]\s*)[^\r\n\"']+", re.I)


def redactar(texto: str) -> str:
    """Quita credenciales de URLs y cabeceras (userinfo, /live/<u>/<p>/, tokens en query)."""
    texto = _RE_USERINFO.sub(r"\1***:***@", texto)
    texto = _RE_LIVE.sub(r"\1***/***/", texto)
    texto = _RE_CABECERA.sub(r"\1***", texto)
    return _RE_QS.sub(r"\1***", texto)


# ── utilidades ───────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    entorno = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           env=entorno, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out
    except FileNotFoundError as e:
        return 127, str(e)


def tail(path: Path, n: int, max_bytes: int = 256_000) -> list[str]:
    """Últimas n líneas sin leer el archivo entero (los .err pueden pesar cientos de MB)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode(errors="replace")
    except OSError as e:
        return [f"(no se pudo leer {path.name}: {e.strerror})"]
    return [redactar(l)[:500] for l in data.splitlines()[-n:]]


def archivo_confiable(path: Path) -> str | None:
    """None si el archivo y su directorio son de root y nadie más puede escribirlos; si no, el
    motivo. Así, ni vigia ni otro usuario pueden plantar una vista o una config a su gusto."""
    if os.name != "posix":
        return None
    for p in (path, path.parent):
        try:
            st = os.lstat(p)
        except OSError as e:
            return f"{p}: {e.strerror}"
        if p == path and not stat.S_ISREG(st.st_mode):
            return f"{p}: no es un archivo regular"
        if st.st_uid != 0:
            return f"{p}: el dueño no es root"
        if st.st_mode & 0o022:
            return f"{p}: escribible por grupo u otros"
    return None


def cargar_vista() -> dict[str, dict]:
    """Estaciones habilitadas según la vista saneada (no trae credenciales).
    Si la vista no es confiable (dueño/permisos) se ignora entera."""
    if archivo_confiable(VISTA) is not None:
        return {}
    try:
        return json.loads(VISTA.read_text()).get("estaciones", {})
    except (OSError, ValueError, AttributeError):
        return {}


def validar_sid(sid: str, vista: dict) -> str:
    if not SID_RE.match(sid or ""):
        raise ValueError(f"sid inválido: {sid!r}")
    if sid not in vista and not (STREAMS_ROOT / sid / "index.m3u8").is_file():
        raise ValueError(f"estación desconocida: {sid}")
    return sid


def entero(valor: str, minimo: int, maximo: int) -> int:
    return max(minimo, min(maximo, int(valor)))


def segmentos_m3u8(sid: str) -> list[str]:
    """Nombres de segmento del playlist local, del más viejo al más nuevo (sin glob en disco).
    Solo nombres `seg_<n>.ts`: nada de rutas que el playlist pudiera traer."""
    try:
        txt = (STREAMS_ROOT / sid / "index.m3u8").read_text(errors="replace")
    except OSError:
        return []
    return [l.strip() for l in txt.splitlines() if SEG_RE.match(l.strip())]


def edad_m3u8(sid: str) -> float | None:
    try:
        return round(time.time() - (STREAMS_ROOT / sid / "index.m3u8").stat().st_mtime, 1)
    except OSError:
        return None


def _segundos_corriendo(d: Path) -> int | None:
    """Edad del proceso: campo 22 de /proc/<pid>/stat (ticks desde el arranque) vs /proc/uptime."""
    try:
        campos = (d / "stat").read_text().rsplit(")", 1)[1].split()
        inicio = int(campos[19]) / os.sysconf("SC_CLK_TCK")
        return round(float(Path("/proc/uptime").read_text().split()[0]) - inicio)
    except (OSError, ValueError, IndexError):
        return None


def ffmpeg_por_estacion(con_cmd: bool = False) -> dict[str, list[dict]]:
    """Procesos ffmpeg que escriben el HLS de cada estación (1 por estación = normal)."""
    res: dict[str, list[dict]] = {}
    pat = re.compile(re.escape(str(STREAMS_ROOT)) + r"/([a-z0-9_]+)/index\.m3u8")
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0].endswith(b"ffmpeg"):
            continue
        cmd = " ".join(a.decode(errors="replace") for a in argv if a)
        m = pat.search(cmd)
        if not m:
            continue
        p = {"pid": int(d.name), "seg_corriendo": _segundos_corriendo(d)}
        if con_cmd:
            p["cmd"] = redactar(cmd)[:600]
        res.setdefault(m.group(1), []).append(p)
    return res


_RE_VOL = re.compile(r"(mean|max)_volume:\s*(-?[\d.]+|-inf) dB")


def parse_volumedetect(salida: str) -> dict:
    v = {k: (float(x) if x != "-inf" else -91.0) for k, x in _RE_VOL.findall(salida)}
    return {"mean_db": v.get("mean"), "max_db": v.get("max")}


def parece_texto(path: Path) -> bool:
    """Un 'segmento' que es un playlist o HTML no se le pasa a ffmpeg (podría abrir lo que nombre)."""
    try:
        inicio = path.read_bytes()[:2048]
    except OSError:
        return True
    if b"#EXT" in inicio or inicio.lstrip()[:1] == b"<":
        return True
    imprimibles = sum(32 <= b < 127 or b in (9, 10, 13) for b in inicio)
    return bool(inicio) and imprimibles / len(inicio) > 0.95


def comando_volumen(path: Path) -> list[str]:
    # protocol_whitelist=file: ffmpeg no abre red ni otros protocolos aunque el archivo los nombre
    return ["nice", "-n", "19", "timeout", "20", "ffmpeg", "-nostdin", "-hide_banner",
            "-protocol_whitelist", "file", "-i", str(path), "-vn", "-af", "volumedetect",
            "-f", "null", "-"]


def volumen(path: Path, externo: bool = False) -> dict:
    if externo and parece_texto(path):
        return {"mean_db": None, "max_db": None, "error": "el contenido descargado no es audio/video"}
    rc, out = run(comando_volumen(path), timeout=25)
    r = parse_volumedetect(out)
    if r["mean_db"] is None:
        ultima = out.strip().splitlines()[-1] if out.strip() else f"rc={rc}"
        r["error"] = redactar(ultima)[:200]
    return r


def servicio(nombre: str) -> dict:
    rc, out = run(["systemctl", "show", nombre, "-p", "ActiveState", "-p", "SubState",
                   "-p", "NRestarts", "-p", "ActiveEnterTimestamp"])
    d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return {"activo": d.get("ActiveState"), "sub": d.get("SubState"),
            "reinicios": int(d["NRestarts"]) if d.get("NRestarts", "").isdigit() else None,
            "desde": d.get("ActiveEnterTimestamp")}


def disco() -> dict:
    st = os.statvfs("/")
    total, libre = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    return {"usado_pct": round(100 * (1 - libre / total), 1), "libre_gb": round(libre / 1e9, 1)}


def carga() -> list[float]:
    return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]


# ── red: solo destinos públicos (anti-SSRF) ──────────────────────────────────

def resolver_publica(url: str) -> tuple[str | None, str | None]:
    """(ip, None) si TODAS las IPs del host son públicas; (None, motivo) si no.
    Evita que un origen (o su redirección, o un segmento de su playlist) nos haga pedir
    metadatos del droplet (169.254.169.254), la VPC, la WireGuard o localhost."""
    try:
        p = urlsplit(url)
        host, puerto = p.hostname, p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None, "URL inválida"
    if p.scheme not in ("http", "https") or not host:
        return None, "solo http/https con host"
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(host, puerto, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, UnicodeError) as e:
        return None, f"DNS: {e}"
    for ip in ips:
        if not ipaddress.ip_address(ip.split("%")[0]).is_global:
            return None, f"destino no público ({ip})"
    return sorted(ips)[0], None


def _curl(url: str, socks: str | None, max_time: int, cuerpo: bool = False,
          destino: str | None = None) -> dict:
    """Una petición con redirecciones seguidas a mano: cada salto se valida antes de pedirlo."""
    fmt = ("\n__M__ %{http_code} %{size_download} %{speed_download} %{time_total} "
           "%{ssl_verify_result} %{redirect_url} %{content_type}")
    r: dict = {}
    for salto in range(MAX_REDIRECCIONES + 1):
        ip, motivo = resolver_publica(url)
        if motivo:
            return {"url": redactar(url), "bloqueado": motivo}
        p = urlsplit(url)
        cmd = ["curl", "-sS", "--proto", "=http,https", "--max-time", str(max_time),
               "--max-filesize", "50000000", "-A", "Mozilla/5.0", "-w", fmt]
        if socks:
            # --socks5 (no -hostname): el DNS se resuelve acá y se fija la IP ya validada; el
            # gateway solo recibe una IP pública, nunca un nombre que resuelva a su propia LAN
            cmd += ["--socks5", socks]
        ip_curl = f"[{ip}]" if ":" in ip else ip
        cmd += ["--resolve", f"{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}:{ip_curl}"]
        cmd += ["-o", "-" if cuerpo else (destino or "/dev/null"), url]
        rc, out = run(cmd, timeout=max_time + 5)
        body, _, m = out.rpartition("__M__ ")
        partes = m.split(" ", 6)
        r = {"rc": rc, "url": redactar(url)}
        if len(partes) >= 6 and partes[0].isdigit():
            redir = partes[5].strip()
            r.update(http=int(partes[0]), bytes=int(float(partes[1])), kbps=round(float(partes[2]) * 8 / 1000),
                     seg=round(float(partes[3]), 2), tls_ok=partes[4] == "0",
                     content_type=(partes[6].strip() if len(partes) > 6 else "") or None)
            if 300 <= r["http"] < 400 and redir:
                r["redireccion"] = redactar(redir)
                url = redir
                continue
        else:
            r["error"] = redactar(out.strip()[-300:])
        if cuerpo:
            r["_body"] = body[:64_000]
        r["saltos"] = salto
        return r
    r["error"] = f"más de {MAX_REDIRECCIONES} redirecciones"
    return r


def _curl_con_audio(url: str, socks: str | None, max_time: int) -> dict:
    """Baja un trozo del origen a /tmp, mide su volumen y lo borra: separa 'origen mudo' de
    'nuestro pipeline lo enmudece'. Es un archivo temporal del usuario vigia, no una grabación."""
    fd, tmp = tempfile.mkstemp(prefix="vigia-", suffix=".bin")
    os.close(fd)
    try:
        r = _curl(url, socks, max_time, destino=tmp)
        if os.path.getsize(tmp) > 2000:
            r["audio_origen"] = volumen(Path(tmp), externo=True)
        return r
    finally:
        os.unlink(tmp)


def _segmento_de(base: str, body: str) -> str | None:
    lineas = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
    return urljoin(base, lineas[-1]) if lineas else None


def _gateway_socks5() -> str | None:
    try:
        vals: dict[str, str] = {}
        for l in GATEWAY_CONF.read_text().splitlines():
            if "=" in l and not l.strip().startswith("#"):
                k, _, v = l.partition("=")
                v = v.strip().strip("\"'")
                for kk, vv in vals.items():
                    v = v.replace(f"${{{kk}}}", vv)
                vals[k.strip()] = v
        s = vals.get("GW_SOCKS5") or (f"{vals['GW_SOCKS5_HOST']}:{vals['GW_SOCKS5_PORT']}"
                                      if "GW_SOCKS5_HOST" in vals else None)
        return s if s and re.fullmatch(r"[\w.-]+:\d{1,5}", s) else None
    except (OSError, KeyError):
        return None


def probar_url(entrada: dict | None, route: str | None, media_type: str | None) -> dict:
    if not entrada:
        return {"omitido": "sin URL"}
    url = entrada.get("probar_url")
    if not url:
        return {"url": entrada.get("resumen"), "omitido": entrada.get("motivo") or "no se prueba"}
    socks = _gateway_socks5() if route == "socks5" else None
    res: dict = {"via": "socks5 (gateway activo)" if socks else "directo"}
    if ".m3u8" in urlsplit(url).path:
        pl = _curl(url, socks, 10, cuerpo=True)
        body = pl.pop("_body", "")
        res["playlist"] = pl
        res["playlist_inicio"] = [redactar(l)[:200] for l in body.splitlines()[:8]]
        seg = _segmento_de(url, body) if pl.get("http") == 200 else None
        if seg and ".m3u8" in urlsplit(seg).path:          # master → una variante
            sub = _curl(seg, socks, 10, cuerpo=True)
            seg = _segmento_de(seg, sub.pop("_body", "")) if sub.get("http") == 200 else None
            res["variante"] = sub
        if seg:
            res["segmento"] = _curl_con_audio(seg, socks, 15)
    elif media_type == "radio":
        r = _curl_con_audio(url, socks, 8)              # Icecast no termina: rc 28 + bytes = vivo
        r["vivo"] = r.get("bytes", 0) > 20_000
        res["stream"] = r
    else:
        res["pagina"] = _curl(url, socks, 10)
        res["nota"] = "origen vía resolver (streamlink); solo se prueba que la página responda"
    return res


# ── subcomandos ──────────────────────────────────────────────────────────────

def cmd_chequeo(args: list[str]) -> dict:
    """Foto barata de todo el nodo para el vigía. --audio mide el último segmento de cada estación."""
    con_audio = "--audio" in args
    vista = cargar_vista()
    procs = ffmpeg_por_estacion()
    estaciones = {}
    for sid in sorted(set(vista) | set(procs)):
        segs = segmentos_m3u8(sid)
        e = {"tipo": vista.get(sid, {}).get("media_type"), "habilitada": sid in vista,
             "edad_m3u8_s": edad_m3u8(sid), "ffmpeg": len(procs.get(sid, []))}
        if segs:
            try:
                e["bytes_ultimo_seg"] = (STREAMS_ROOT / sid / segs[-1]).stat().st_size
            except OSError:
                e["bytes_ultimo_seg"] = None
        if con_audio and segs and (e["edad_m3u8_s"] or 1e9) < STALE_SECS:
            e["audio"] = volumen(STREAMS_ROOT / sid / segs[-1])
        estaciones[sid] = e
    return {"ts": int(time.time()), "carga": carga(), "disco": disco(),
            "servicios": {s: servicio(s) for s in SERVICIOS}, "estaciones": estaciones}


def cmd_estado_general(args: list[str]) -> dict:
    return cmd_chequeo([])


def cmd_estado_estacion(args: list[str]) -> dict:
    vista = cargar_vista()
    sid = validar_sid(args[0], vista)
    c = vista.get(sid)
    tam = {}
    for s in segmentos_m3u8(sid)[-5:]:
        try:
            tam[s] = (STREAMS_ROOT / sid / s).stat().st_size
        except OSError:
            tam[s] = None
    config = None
    if c:
        config = {k: c.get(k) for k in ("media_type", "route", "hls_live_restart", "tiene_ffmpeg_extra")}
        for campo in ("stream_url", "fallback_url"):
            config[campo] = (c.get(campo) or {}).get("resumen")
    return {"sid": sid, "habilitada": c is not None, "config": config,
            "edad_m3u8_s": edad_m3u8(sid), "ultimos_segmentos_bytes": tam,
            "ffmpeg": ffmpeg_por_estacion(con_cmd=True).get(sid, []),
            "err_ultimas": tail(ERR_DIR / f"{sid}.err", 15)}


def cmd_leer_err(args: list[str]) -> dict:
    sid = validar_sid(args[0], cargar_vista())
    n = entero(args[1] if len(args) > 1 else "80", 1, 200)
    return {"sid": sid, "lineas": tail(ERR_DIR / f"{sid}.err", n)}


def cmd_logs_servicio(args: list[str]) -> dict:
    nombre = args[0]
    if nombre not in SERVICIOS:
        raise ValueError(f"servicio fuera de la lista: {nombre}")
    n = entero(args[1] if len(args) > 1 else "100", 1, 300)
    minutos = entero(args[2] if len(args) > 2 else "120", 1, 1440)
    rc, out = run(["journalctl", "-u", nombre, "--since", f"-{minutos}min", "-n", str(n),
                   "--no-pager", "-o", "short-iso"], timeout=20)
    return {"servicio": nombre, "lineas": [redactar(l)[:500] for l in out.splitlines()[-n:]]}


def cmd_medir_audio(args: list[str]) -> dict:
    vista = cargar_vista()
    sid = validar_sid(args[0], vista)
    n = entero(args[1] if len(args) > 1 else "3", 1, 5)
    res: dict = {"sid": sid, "segmentos": {s: volumen(STREAMS_ROOT / sid / s)
                                           for s in segmentos_m3u8(sid)[-n:]}}
    # control: una radio sana medida en la misma corrida (sin control, -91 no prueba nada)
    for ctrl in sorted(vista):
        segs = segmentos_m3u8(ctrl)
        if ctrl != sid and vista[ctrl].get("media_type") == "radio" and segs and \
                (edad_m3u8(ctrl) or 1e9) < STALE_SECS:
            res["control"] = {"sid": ctrl, **volumen(STREAMS_ROOT / ctrl / segs[-1])}
            break
    res["referencia"] = "sano -14..-16 dB radio, TV baja ~-37 dB; silencio digital -91 dB; > -60 hay audio"
    return res


def cmd_probar_origen(args: list[str]) -> dict:
    vista = cargar_vista()
    sid = validar_sid(args[0], vista)
    c = vista.get(sid)
    if not c:
        return {"sid": sid, "error": "estación no habilitada en capture_config: no hay URL que probar"}
    res = {"sid": sid, "route": c.get("route"),
           "principal": probar_url(c.get("stream_url"), c.get("route"), c.get("media_type"))}
    if c.get("fallback_url"):
        res["respaldo"] = probar_url(c["fallback_url"], c.get("route"), c.get("media_type"))
    return res


def _tam_dir(d: Path) -> dict:
    """Cantidad de archivos y MB de una hora de audio TV (un nivel, ~900 entradas como mucho)."""
    n, total, mayor = 0, 0, 0
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    s = e.stat(follow_symlinks=False).st_size
                    n, total, mayor = n + 1, total + s, max(mayor, s)
    except OSError:
        pass
    return {"archivos": n, "mb": round(total / 1e6, 1), "mayor_mb": round(mayor / 1e6, 1)}


def cmd_estado_uploader(args: list[str]) -> dict:
    canales = {}
    if TV_AUDIO.is_dir():
        for d in sorted(p for p in TV_AUDIO.iterdir() if p.is_dir() and not p.is_symlink()):
            horas = sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
            outbox = d / ".outbox"
            canales[d.name] = {
                "horas_abiertas": {h: _tam_dir(d / h) for h in horas[-6:]}, "n_horas_abiertas": len(horas),
                "archivos_sueltos_mb": {p.name: round(p.stat().st_size / 1e6, 1) for p in d.iterdir()
                                        if p.is_file() and p.stat().st_size > 50_000_000},
                "bandeja": sorted(p.name for p in outbox.iterdir())[-6:] if outbox.is_dir() else None}
    rc, out = run(["journalctl", "-u", "video-segment-uploader", "-n", "40", "--no-pager",
                   "-o", "short-iso"], timeout=20)
    return {"servicio": servicio("video-segment-uploader"), "canales": canales,
            "log": [redactar(l)[:500] for l in out.splitlines()[-40:]]}


def cmd_cambios_recientes(args: list[str]) -> dict:
    n = entero(args[0] if args else "30", 1, 50)
    return {"lineas": tail(CHANGES_LOG, n)}


COMANDOS = {
    "chequeo": cmd_chequeo, "estado_general": cmd_estado_general,
    "estado_estacion": cmd_estado_estacion, "leer_err": cmd_leer_err,
    "logs_servicio": cmd_logs_servicio, "medir_audio": cmd_medir_audio,
    "probar_origen": cmd_probar_origen, "estado_uploader": cmd_estado_uploader,
    "cambios_recientes": cmd_cambios_recientes,
}


def despachar(linea: str) -> dict:
    if len(linea) > 200:
        return {"error": "comando demasiado largo"}
    try:
        partes = shlex.split(linea)
    except ValueError as e:
        return {"error": f"comando mal formado: {e}"}
    if not partes or partes[0] not in COMANDOS:
        return {"error": f"comando no permitido: {partes[0][:40] if partes else ''}",
                "permitidos": sorted(COMANDOS)}
    if len(partes) > 4 or any(len(p) > 64 for p in partes):
        return {"error": "demasiados argumentos o argumento demasiado largo"}
    if partes[0] == "chequeo" and any(a != "--audio" for a in partes[1:]):
        return {"error": "chequeo solo acepta --audio"}
    try:
        return COMANDOS[partes[0]](partes[1:])
    except (ValueError, IndexError) as e:
        return {"error": str(e) or "faltan argumentos"}


def con_candado(fn, espera_s: int = 90):
    """Un comando a la vez: aunque alguien tuviera la llave, no puede apilar ffmpeg/curl en el nodo."""
    import fcntl                                  # solo Linux; importarlo acá deja probar en Windows
    with open(LOCK, "a") as f:
        limite = time.time() + espera_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > limite:
                    return {"error": "ocupado: otro comando de mediacap-ro sigue corriendo"}
                time.sleep(0.5)
        return fn()


def main() -> None:
    os.umask(0o077)                               # temporales de /tmp solo para vigia
    os.nice(10)
    linea = os.environ.get("SSH_ORIGINAL_COMMAND")
    if linea is None:
        linea = shlex.join(sys.argv[1:])
    resultado = con_candado(lambda: despachar(linea))
    salida = json.dumps(resultado, ensure_ascii=False, default=str)
    if len(salida) > MAX_SALIDA and not linea.startswith("chequeo"):
        salida = json.dumps({"truncado": True, "inicio": salida[:MAX_SALIDA]}, ensure_ascii=False)
    print(salida)


if __name__ == "__main__":
    main()
