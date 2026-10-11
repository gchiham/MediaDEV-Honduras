"""Herramientas del agente: definiciones (formato function tools de OpenAI) y ejecución.

Todas son de SOLO LECTURA. Dos backends con la misma interfaz:
- Real:   subcomandos de mediacap-ro por SSH + consultas SQL fijas con un rol read-only.
- Replay: respuestas grabadas de un incidente (pruebas sin tocar mediaCAP).
"""
import json, os, re
from pathlib import Path

MAX_SALIDA = 20000
SID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
SERVICIOS = ["stream-daemon", "video-segment-uploader", "mediadev-gateway-api",
             "mediadev-health-engine", "mediadev-monitor", "nginx", "privoxy", "wg-quick@wg0",
             "mediadev-segment-janitor.timer", "mediadev-ffmpeg-reaper.timer"]
CONSULTAS = ["cobertura", "eventos", "estado_cb", "s3_scan", "gateways", "config"]


def _fn(nombre: str, descripcion: str, props: dict) -> dict:
    return {"type": "function", "name": nombre, "description": descripcion, "strict": True,
            "parameters": {"type": "object", "properties": props,
                           "required": list(props), "additionalProperties": False}}


SID = {"type": "string", "description": "id de la estación (slug), p. ej. radio_america"}

TOOLS = [
    _fn("estado_general", "Foto de mediaCAP: servicios systemd, load, disco, y por estación: edad del "
        "index.m3u8, cantidad de ffmpeg y bytes del último segmento.", {}),
    _fn("estado_estacion", "Detalle de una estación: config de captura (route, URL redactada, banderas), "
        "edad del m3u8, tamaños de los últimos segmentos, ffmpeg corriendo (cmdline real) y últimas "
        "líneas de su .err.", {"sid": SID}),
    _fn("leer_err", "Últimas líneas del stderr de ffmpeg de la estación (/var/log/streams/ffmpeg/<sid>.err).",
        {"sid": SID, "lineas": {"type": "integer", "description": "1-200"}}),
    _fn("logs_servicio", "journalctl de un servicio de mediaCAP.",
        {"servicio": {"type": "string", "enum": SERVICIOS},
         "lineas": {"type": "integer", "description": "1-300"},
         "minutos": {"type": "integer", "description": "ventana hacia atrás, 1-1440"}}),
    _fn("medir_audio", "volumedetect sobre los últimos segmentos YA GRABADOS de la estación, más una "
        "radio sana como control en la misma corrida. No abre conexiones al origen.",
        {"sid": SID, "segmentos": {"type": "integer", "description": "1-5"}}),
    _fn("probar_origen", "Prueba la URL de origen (y la de respaldo si hay) desde mediaCAP por la misma "
        "ruta que la captura (socks5 o directo): código HTTP, TLS, throughput de UN segmento y su volumen "
        "(audio_origen: distingue origen mudo de pipeline nuestro). Para orígenes que son una página "
        "(Dailymotion, kick, mdstrm) trae en_vivo: si vivo=false la emisora NO está transmitiendo. "
        "Los orígenes de cupo limitado o cliente único se omiten solos.", {"sid": SID}),
    _fn("estado_uploader", "Uploader de video TV: estado del servicio, horas de audio abiertas por canal con "
        "tamaño de las últimas horas (archivos, MB, archivo más grande), archivos sueltos > 50 MB, "
        "bandeja de salida y últimas 40 líneas de log.", {}),
    _fn("cambios_recientes", "Últimas líneas de /opt/media-ai/CHANGES.log (deploys y cambios manuales).",
        {"lineas": {"type": "integer", "description": "1-50"}}),
    _fn("consulta", "Consultas SQL fijas de solo lectura sobre media-db. cobertura: recording_coverage por "
        "hora (cuenta segundos, es CIEGA al silencio). eventos: transiciones DOWN/UP/CB. estado_cb: fila "
        "del daemon. s3_scan: horas registradas para el Destroyer. gateways: estado de los gateways. "
        "config: capture_config de la estación.",
        {"nombre": {"type": "string", "enum": CONSULTAS},
         "sid": {"type": ["string", "null"], "description": "estación; null para gateways"},
         "horas": {"type": "integer", "description": "ventana hacia atrás, 1-48"}}),
    _fn("reportar", "Termina la investigación con el diagnóstico. Es la ÚNICA salida que ve el operador.",
        {"severidad": {"type": "string", "enum": ["critica", "alta", "media", "info"]},
         "estaciones": {"type": "array", "items": {"type": "string"}},
         "titulo": {"type": "string",
                    "description": "una línea de hasta 80 caracteres, p. ej. 'teleceiba no graba desde 06:11 HN'"},
         "causa_probable": {"type": "string", "description": "UNA frase, hasta 200 caracteres"},
         "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
         "evidencia": {"type": "array", "items": {"type": "string"},
                       "description": "2 o 3 hechos DECISIVOS observados con las herramientas, una línea "
                                      "corta cada uno (hasta 120 caracteres); sin repetir la causa"},
         "accion_sugerida": {"type": "string",
                             "description": "1 o 2 frases concretas. Si no hay nada de nuestro lado, decirlo "
                                            "(p. ej. 'Nada de nuestro lado; el vigía avisará cuando vuelva')"},
         "lado": {"type": "string", "enum": ["origen", "captura_mediacap", "gateway_red", "uploader_s3",
                                             "base_datos", "sin_problema", "desconocido"]}}),
]
NOMBRES = {t["name"] for t in TOOLS}


def _sid(args: dict) -> str:
    sid = args.get("sid") or ""
    if not SID_RE.match(sid):
        raise ValueError(f"sid inválido: {sid!r}")
    return sid


def _int(v, lo, hi, defecto):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return defecto


def comando_mediacap(nombre: str, args: dict) -> str:
    """Traduce una llamada de herramienta a la línea de mediacap-ro (validada de este lado también)."""
    if nombre in ("estado_general", "estado_uploader"):
        return nombre
    if nombre in ("estado_estacion", "probar_origen"):
        return f"{nombre} {_sid(args)}"
    if nombre == "leer_err":
        return f"leer_err {_sid(args)} {_int(args.get('lineas'), 1, 200, 80)}"
    if nombre == "medir_audio":
        return f"medir_audio {_sid(args)} {_int(args.get('segmentos'), 1, 5, 3)}"
    if nombre == "logs_servicio":
        if args.get("servicio") not in SERVICIOS:
            raise ValueError("servicio fuera de la lista")
        return (f"logs_servicio {args['servicio']} {_int(args.get('lineas'), 1, 300, 100)} "
                f"{_int(args.get('minutos'), 1, 1440, 120)}")
    if nombre == "cambios_recientes":
        return f"cambios_recientes {_int(args.get('lineas'), 1, 50, 30)}"
    raise ValueError(f"herramienta sin comando: {nombre}")


SQL = {
    "cobertura": ("""SELECT period_start_utc, status, round(actual_seconds) seg, expected_seconds esp,
                            reason, size_bytes FROM recording_coverage
                     WHERE stream_id = %(sid)s AND period_start_utc > now() - make_interval(hours => %(h)s)
                     ORDER BY period_start_utc DESC LIMIT 48""", True),
    "eventos": ("""SELECT to_timestamp(ts) ts, etype, left(detail, 200) detail FROM mediadev_events
                   WHERE stream_id = %(sid)s AND ts > extract(epoch FROM now()) - %(h)s * 3600
                   ORDER BY ts DESC LIMIT 60""", True),
    "estado_cb": ("""SELECT stream_id, status, segs, age, cb_state, cb_fails, to_timestamp(cb_since) cb_desde,
                            restart_today, to_timestamp(last_down) ultima_caida, to_timestamp(last_up) ultimo_up
                     FROM mediadev_stream_status WHERE stream_id = %(sid)s""", True),
    "s3_scan": ("""SELECT hour_start_utc, status, detections, left(error_msg, 200) error FROM s3_scan_log
                   WHERE stream = %(sid)s AND hour_start_utc > now() - make_interval(hours => %(h)s)
                   ORDER BY hour_start_utc DESC LIMIT 48""", True),
    "gateways": ("""SELECT gateway_id, name, status, score, is_active, maintenance, last_heartbeat
                    FROM gateways ORDER BY priority""", False),
    "config": ("""SELECT ms.slug, ms.media_type, ms.lifecycle_status, cc.is_enabled, cc.route,
                         cc.stream_url, cc.fallback_url, cc.hls_live_restart, cc.ffmpeg_extra
                  FROM capture_config cc JOIN media_sources ms ON ms.id = cc.media_source_id
                  WHERE ms.slug = %(sid)s""", True),
}


def sanear_filas(filas: list[dict]) -> list[dict]:
    """Las URLs de capture_config salen resumidas (sin credenciales ni query); el resto, redactado."""
    from mediacap_ro import redactar
    from mediacap_vista import resumir_url
    for f in filas:
        for k in ("stream_url", "fallback_url"):
            if k in f:
                f[k] = resumir_url(f[k])
        if f.get("ffmpeg_extra"):
            f["ffmpeg_extra"] = "(definido; no se muestra)"
    return json.loads(redactar(json.dumps(filas, default=str)))


def recortar(texto: str) -> str:
    if len(texto) <= MAX_SALIDA:
        return texto
    return texto[:MAX_SALIDA] + f"\n…(recortado: {len(texto) - MAX_SALIDA} caracteres más)"


class Real:
    """Ejecuta contra mediaCAP (SSH) y media-db (rol read-only)."""

    def __init__(self):
        from comun import ssh_mediacap
        self._ssh = ssh_mediacap

    def consulta(self, args: dict) -> object:
        nombre = args.get("nombre")
        if nombre not in SQL:
            raise ValueError("consulta desconocida")
        sql, necesita_sid = SQL[nombre]
        params = {"h": _int(args.get("horas"), 1, 48, 12)}
        if necesita_sid:
            params["sid"] = _sid(args)
        import psycopg2, psycopg2.extras
        c = psycopg2.connect(os.environ["DATABASE_URL_RO"], connect_timeout=8,
                             options="-c default_transaction_read_only=on -c statement_timeout=5000")
        try:
            c.set_session(readonly=True, autocommit=False)
            cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            filas = [dict(f) for f in cur.fetchall()]
        finally:
            c.close()
        return sanear_filas(filas)

    def ejecutar(self, nombre: str, args: dict) -> object:
        if nombre == "consulta":
            return self.consulta(args)
        return self._ssh(comando_mediacap(nombre, args))


class Replay:
    """Devuelve salidas grabadas. Busca 'nombre:arg1:arg2', luego 'nombre:sid', luego 'nombre'."""

    def __init__(self, respuestas: dict):
        self.respuestas = respuestas
        self.llamadas: list[str] = []

    def ejecutar(self, nombre: str, args: dict) -> object:
        valores = [str(v) for v in args.values() if v is not None]
        claves = [":".join([nombre] + valores)]
        if args.get("nombre"):                       # consulta: el nombre de la consulta manda
            claves.append(f"{nombre}:{args['nombre']}:{args.get('sid')}")
            claves.append(f"{nombre}:{args['nombre']}")
        elif args.get("sid"):
            claves.append(f"{nombre}:{args['sid']}")
        if args.get("servicio"):
            claves.append(f"{nombre}:{args['servicio']}")
        claves.append(nombre)
        self.llamadas.append(claves[0])
        for k in claves:
            if k in self.respuestas:
                return self.respuestas[k]
        return {"replay": "sin datos grabados para esta llamada", "clave": claves[0]}


def ejecutar(backend, nombre: str, args_json: str) -> str:
    """Corre una herramienta y devuelve el texto para function_call_output (errores incluidos)."""
    if nombre not in NOMBRES or nombre == "reportar":
        return json.dumps({"error": f"herramienta desconocida: {nombre}"})
    try:
        args = json.loads(args_json or "{}")
        res = backend.ejecutar(nombre, args)
    except Exception as e:                       # el modelo ve el error y puede corregir la llamada
        res = {"error": f"{type(e).__name__}: {e}"}
    return recortar(json.dumps(res, ensure_ascii=False, default=str))
