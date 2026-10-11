#!/usr/bin/env python3
"""vigia — chequeos deterministas de mediaCAP cada 5 min (corre en mediaAPP, sin IA).

- Todo sano → no manda nada.
- Condición nueva (sostenida N chequeos) → alerta simple por Telegram + encola un incidente
  para agente.py, que lo investiga y responde en el mismo hilo.
- La alerta NO depende del agente: si la API falla o se pasa el tope, la alerta ya salió.

Estado en $VIGIA_ESTADO_DIR/estado.json; lista de exclusiones en $VIGIA_ESTADO_DIR/ignorar.json:
    {"estaciones": ["canal_5"], "ventanas_hn": [["06:18", "06:30"]]}
"""
import os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comun import (COLA_DIR, ESTADO_DIR, ahora_hn, en_pausa, escribir_json, hora_hn, ia_detenida,
                   leer_json, ssh_mediacap, telegram, telegram_getme)

INTERVALO_AUDIO_MIN = 15          # el chequeo con volumedetect corre cada 15 min, no cada 5
STALE_SECS = 90
SILENCIO_DB = -80.0
DISCO_PCT = 85.0
CARGA_MAX = 6.0
REALERTA_S = 2 * 3600
COOLDOWN_AGENTE_S = 30 * 60
RESUMEN_HORA_HN = int(os.environ.get("VIGIA_RESUMEN_HORA", "7"))

# chequeos seguidos con la condición antes de alertar (filtra blips y restarts del daemon)
UMBRAL = {"sin_grabar": 2, "ffmpeg_duplicado": 2, "silencio": 3, "carga": 3, "disco": 1,
          "servicio_caido": 1, "mediacap_inalcanzable": 2, "db_inalcanzable": 3}
EVENTOS = {"servicio_reinicio"}   # se avisan una vez; no tienen "recuperado"


def leer_db() -> dict | None:
    """Estaciones habilitadas (fuente de verdad) y estado del CB, con un rol de solo lectura."""
    dsn = os.environ.get("DATABASE_URL_RO")
    if not dsn:
        return None
    import psycopg2
    c = None
    try:
        c = psycopg2.connect(dsn, connect_timeout=8,
                             options="-c default_transaction_read_only=on -c statement_timeout=5000")
        c.set_session(readonly=True)
        cur = c.cursor()
        cur.execute("""SELECT ms.slug, ms.media_type FROM capture_config cc
                       JOIN media_sources ms ON ms.id = cc.media_source_id
                       WHERE cc.is_enabled AND ms.lifecycle_status = 'active' AND ms.slug IS NOT NULL""")
        hab = dict(cur.fetchall())
        cur.execute("SELECT stream_id, cb_state, cb_fails FROM mediadev_stream_status")
        cb = {r[0]: {"cb": r[1], "fallos": r[2]} for r in cur.fetchall()}
        return {"habilitadas": hab, "cb": cb}
    except Exception as e:
        print(f"db: {type(e).__name__}")               # sin el mensaje: puede traer el DSN
        return None
    finally:
        if c is not None:
            c.close()


def condiciones(foto: dict, db: dict | None, ignorar: dict, con_audio: bool,
                reinicios_previos: dict) -> tuple[dict, dict, set]:
    """Devuelve (condiciones presentes {clave: detalle}, reinicios actuales, claves evaluadas).

    'evaluadas' son las claves que este chequeo pudo medir: una clave activa que no está ahí
    (p. ej. silencio en un chequeo sin audio) no se da por recuperada."""
    cond, evaluadas = {}, set()
    if "error" in foto:
        evaluadas.add("mediacap_inalcanzable")
        cond["mediacap_inalcanzable"] = {"tipo": "mediacap_inalcanzable", "sid": None,
                                         "detalle": foto["error"]}
        if db is None:
            evaluadas.add("db_inalcanzable")
            cond["db_inalcanzable"] = {"tipo": "db_inalcanzable", "sid": None, "detalle": "sin respuesta"}
        return cond, reinicios_previos, evaluadas
    evaluadas.add("mediacap_inalcanzable")
    evaluadas.add("db_inalcanzable")
    if db is None and os.environ.get("DATABASE_URL_RO"):
        cond["db_inalcanzable"] = {"tipo": "db_inalcanzable", "sid": None, "detalle": "sin respuesta"}

    est = foto.get("estaciones", {})
    habilitadas = (db or {}).get("habilitadas") or {s: e.get("tipo") for s, e in est.items()
                                                     if e.get("habilitada")}
    excluidas = set(ignorar.get("estaciones", []))
    for sid in sorted(habilitadas):
        if sid in excluidas:
            continue
        e = est.get(sid, {})
        k = f"sin_grabar:{sid}"
        evaluadas.add(k)
        edad = e.get("edad_m3u8_s")
        if edad is None or edad > STALE_SECS:
            cb = (db or {}).get("cb", {}).get(sid, {})
            cond[k] = {"tipo": "sin_grabar", "sid": sid,
                       "detalle": f"m3u8 {'inexistente' if edad is None else f'de hace {int(edad)} s'}, "
                                  f"{e.get('ffmpeg', 0)} ffmpeg, CB {cb.get('cb', '?')}"}
        k = f"ffmpeg_duplicado:{sid}"
        evaluadas.add(k)
        if e.get("ffmpeg", 0) > 1:
            cond[k] = {"tipo": "ffmpeg_duplicado", "sid": sid, "detalle": f"{e['ffmpeg']} procesos ffmpeg"}
        k = f"silencio:{sid}"
        if con_audio and "audio" in e:
            evaluadas.add(k)
            mean = e["audio"].get("mean_db")
            if mean is not None and mean < SILENCIO_DB:
                cond[k] = {"tipo": "silencio", "sid": sid,
                           "detalle": f"mean {mean} dB / max {e['audio'].get('max_db')} dB en el último segmento"}

    evaluadas.update({"disco", "carga"})
    d = foto.get("disco", {})
    if d.get("usado_pct", 0) > DISCO_PCT:
        cond["disco"] = {"tipo": "disco", "sid": None,
                         "detalle": f"/ al {d['usado_pct']} % ({d.get('libre_gb')} GB libres)"}
    carga = foto.get("carga", [0])
    if carga and carga[0] > CARGA_MAX:
        cond["carga"] = {"tipo": "carga", "sid": None, "detalle": f"load1 {carga[0]}"}

    reinicios = {}
    for nombre, s in foto.get("servicios", {}).items():
        if s.get("instalado") is False:               # unidad que no existe en el nodo: no es una caída
            continue
        k = f"servicio_caido:{nombre}"
        evaluadas.add(k)
        if s.get("activo") not in ("active", None):
            cond[k] = {"tipo": "servicio_caido", "sid": None, "servicio": nombre,
                       "detalle": f"{nombre}: {s.get('activo')}/{s.get('sub')}"}
        n = s.get("reinicios")
        if n is not None:
            reinicios[nombre] = n
            prev = reinicios_previos.get(nombre)
            if prev is not None and n > prev:
                cond[f"servicio_reinicio:{nombre}"] = {
                    "tipo": "servicio_reinicio", "sid": None, "servicio": nombre,
                    "detalle": f"{nombre}: systemd lo reinició {n - prev} vez/veces (NRestarts {n})"}
    return cond, reinicios, evaluadas


def en_ventana_ignorada(ignorar: dict, ahora_ts: float) -> bool:
    hm = hora_hn(ahora_ts)
    return any(a <= hm <= b for a, b in ignorar.get("ventanas_hn", []))


def evaluar(cond: dict, evaluadas: set, estado: dict, ahora: float, silenciar: bool):
    """Aplica umbrales y memoria. Devuelve (nuevas, recuperadas, repetidas) como listas."""
    cont, activas = estado.setdefault("contador", {}), estado.setdefault("activas", {})
    nuevas, recuperadas, repetidas = [], [], []
    for k in list(cont):
        if k in evaluadas and k not in cond:
            cont.pop(k)
    for k, c in cond.items():
        tipo = c["tipo"]
        if tipo in EVENTOS:
            if not silenciar:
                nuevas.append(c)
            continue
        cont[k] = cont.get(k, 0) + 1
        if k in activas:
            if ahora - activas[k]["ultima_alerta"] >= REALERTA_S:
                activas[k]["ultima_alerta"] = ahora
                repetidas.append({**c, "desde": activas[k]["desde"]})
        elif cont[k] >= UMBRAL.get(tipo, 1) and not silenciar:
            activas[k] = {"desde": ahora, "ultima_alerta": ahora, "tipo": tipo, "sid": c.get("sid")}
            nuevas.append(c)
    for k in list(activas):
        if k in evaluadas and k not in cond:
            a = activas.pop(k)
            recuperadas.append({"clave": k, "sid": a.get("sid"), "tipo": a["tipo"],
                                "duracion_min": round((ahora - a["desde"]) / 60)})
    return nuevas, recuperadas, repetidas


NOMBRE = {"sin_grabar": "no graba", "ffmpeg_duplicado": "ffmpeg duplicado", "silencio": "MUDO",
          "carga": "carga alta", "disco": "disco", "servicio_caido": "servicio caído",
          "servicio_reinicio": "servicio reiniciado", "mediacap_inalcanzable": "mediaCAP no responde",
          "db_inalcanzable": "DB no responde"}


def linea(c: dict) -> str:
    quien = c.get("sid") or c.get("servicio") or "mediaCAP"
    return f"• {quien} — {NOMBRE.get(c['tipo'], c['tipo'])}: {c['detalle']}"


def mensaje(nuevas, recuperadas, repetidas, ahora, con_agente: bool = False) -> str:
    partes = []
    if nuevas:
        partes.append(f"🔴 mediaCAP — {len(nuevas)} alerta(s) nueva(s) · {hora_hn(ahora)} HN")
        partes += [linea(c) for c in nuevas]
        if con_agente:
            partes.append("(el agente está investigando; responde en este hilo)")
    if repetidas:
        partes.append(f"🟠 Siguen activas · {hora_hn(ahora)} HN")
        partes += [linea(c) + f" (desde {hora_hn(c['desde'])})" for c in repetidas]
    if recuperadas:
        partes.append(f"✅ Recuperado · {hora_hn(ahora)} HN")
        partes += [f"• {r['sid'] or r['clave']} — {NOMBRE.get(r['tipo'], r['tipo'])} "
                   f"({r['duracion_min']} min)" for r in recuperadas]
    return "\n".join(partes)


def agente_habilitado() -> bool:
    """La investigación con IA es opt-in: sin VIGIA_AGENTE=1 no se encola ni se llama a la API.
    Desde Telegram (/ia_off) solo se puede apagar, nunca encender por encima del entorno."""
    return (os.environ.get("VIGIA_AGENTE") == "1" and os.environ.get("VIGIA_SIMULAR") != "1"
            and not ia_detenida())


def estado_ia() -> str:
    if os.environ.get("VIGIA_AGENTE") != "1":
        return "apagado en la config del servidor"
    return "detenido desde Telegram (/ia_on)" if ia_detenida() else "activo"


def resumen_estaciones(foto: dict, db: dict | None) -> tuple:
    """(grabando, total) de las estaciones habilitadas, o ('?', '?') si no hubo foto."""
    if "error" in foto:
        return "?", "?"
    est = foto.get("estaciones", {})
    habilitadas = (db or {}).get("habilitadas") or {s for s, e in est.items() if e.get("habilitada")}
    grabando = sum(1 for s in habilitadas if (est.get(s, {}).get("edad_m3u8_s") or 1e9) <= STALE_SECS)
    return grabando, len(habilitadas)


DIAS_STATS = 7


def contar(estado: dict, ahora: float, **sumas) -> None:
    """Estadísticas por día HN (las lee /stats). Se guardan los últimos DIAS_STATS días."""
    stats = estado.setdefault("stats", {})
    dia = stats.setdefault(ahora_hn(ahora).strftime("%Y-%m-%d"), {})
    for k, v in sumas.items():
        dia[k] = dia.get(k, 0) + v
    for viejo in sorted(stats)[:-DIAS_STATS]:
        stats.pop(viejo)


def encolar(incidente: dict) -> None:
    escribir_json(COLA_DIR / f"{int(time.time() * 1000)}.json", incidente)


def lanzar_agente() -> None:
    """Corre el agente al final y lo espera: un hijo en segundo plano moriría con el servicio
    oneshot (systemd mata el cgroup). Mientras corre, el timer no dispara otro chequeo."""
    if not agente_habilitado():
        return
    try:
        subprocess.run([sys.executable, str(Path(__file__).with_name("agente.py")), "--cola"],
                       timeout=540)
    except subprocess.TimeoutExpired:
        print("agente: superó 9 min; el incidente queda en la cola para la próxima vuelta")


def latido(foto: dict, db: dict | None, estado: dict, ahora: float) -> str:
    """Resumen diario determinista: prueba de que el vigía sigue vivo (en sano no manda nada más)."""
    grabando, total = resumen_estaciones(foto, db)
    activas = estado.get("activas", {})
    lineas = [f"🟢 Vigía activo · {hora_hn(ahora)} HN",
              f"Estaciones grabando: {grabando} de {total}"]
    if "error" not in foto:
        d = foto.get("disco", {})
        lineas.append(f"Disco: {d.get('usado_pct', '?')} % · load1 {(foto.get('carga') or ['?'])[0]}")
    if activas:
        lineas.append(f"Alertas activas ({len(activas)}):")
        lineas += [f"• {a.get('sid') or k.split(':')[-1]} — {NOMBRE.get(a['tipo'], a['tipo'])} "
                   f"desde {hora_hn(a['desde'])}" for k, a in sorted(activas.items())]
    else:
        lineas.append("Sin alertas activas.")
    pausa = en_pausa(ahora)
    if pausa:
        lineas.append(f"⏸ Alertas en pausa hasta las {hora_hn(pausa)} HN (/reanudar)")
    lineas.append(f"Agente IA: {'apagado (fase 1)' if os.environ.get('VIGIA_AGENTE') != '1' else estado_ia()}")
    return "\n".join(lineas)


def vuelta(ahora: float) -> None:
    estado = leer_json(ESTADO_DIR / "estado.json", {})
    ignorar = leer_json(ESTADO_DIR / "ignorar.json", {})
    con_audio = ahora - estado.get("ultimo_audio", 0) >= INTERVALO_AUDIO_MIN * 60 - 30
    foto = ssh_mediacap("chequeo --audio" if con_audio else "chequeo", timeout=150)
    if con_audio and "error" not in foto:
        estado["ultimo_audio"] = ahora
    db = leer_db()
    cond, reinicios, evaluadas = condiciones(foto, db, ignorar, con_audio and "error" not in foto,
                                             estado.get("reinicios", {}))
    estado["reinicios"] = reinicios or estado.get("reinicios", {})
    nuevas, recuperadas, repetidas = evaluar(cond, evaluadas, estado, ahora,
                                             en_ventana_ignorada(ignorar, ahora))
    pausa = en_pausa(ahora)
    ult = estado.setdefault("agente_ultimo", {})
    para_agente = []
    if agente_habilitado() and not pausa:
        para_agente = [c for c in nuevas if ahora - ult.get(c.get("sid") or c["tipo"], 0) >= COOLDOWN_AGENTE_S]
    msg_id = None
    if nuevas or recuperadas or repetidas:
        if pausa:                                     # /pausa: se evalúa igual, pero no se avisa
            contar(estado, ahora, suprimidas=len(nuevas) + len(recuperadas) + len(repetidas))
        else:
            msg_id = telegram(mensaje(nuevas, recuperadas, repetidas, ahora, con_agente=bool(para_agente)))
            contar(estado, ahora, mensajes=1, alertas_nuevas=len(nuevas), recuperadas=len(recuperadas))
    grabando, total = resumen_estaciones(foto, db)
    estado["ultima_vuelta"] = {"ts": ahora, "grabando": grabando, "total": total, "error": foto.get("error"),
                               "disco": foto.get("disco"), "carga": (foto.get("carga") or [None])[0]}
    contar(estado, ahora, vueltas=1, errores=1 if "error" in foto else 0)

    lanzar = False
    if os.environ.get("VIGIA_SIMULAR") == "1":
        para_agente = []
    if para_agente:
        for c in para_agente:
            ult[c.get("sid") or c["tipo"]] = ahora
        encolar({"tipo": "incidente", "creado": ahora, "msg_id": msg_id, "alertas": para_agente,
                 "activas_previas": [a for k, a in estado["activas"].items()
                                     if not any(k.endswith(f":{c.get('sid')}") for c in para_agente)]})
        lanzar = True
    hoy = ahora_hn(ahora).strftime("%Y-%m-%d")
    if ahora_hn(ahora).hour == RESUMEN_HORA_HN and estado.get("resumen_fecha") != hoy:
        estado["resumen_fecha"] = hoy
        telegram(latido(foto, db, estado, ahora))     # el latido sale aun en pausa: dice que está pausado
        if agente_habilitado() and not pausa:
            encolar({"tipo": "resumen_diario", "creado": ahora, "activas": estado["activas"]})
            lanzar = True
    if os.environ.get("VIGIA_SIMULAR") == "1":       # verificación: no altera la memoria ni la cola
        return
    estado.pop("fallo_vigia", None)
    escribir_json(ESTADO_DIR / "estado.json", estado)
    if lanzar:
        lanzar_agente()


def avisar_fallo(e: Exception, ahora: float) -> None:
    """Si el propio vigía revienta, avisar (una vez por hora) en vez de quedar callado."""
    estado = leer_json(ESTADO_DIR / "estado.json", {})
    if ahora - estado.get("fallo_vigia", 0) >= 3600:
        telegram(f"⚠️ El vigía de mediaCAP falló ({type(e).__name__}) · {hora_hn(ahora)} HN. "
                 f"Las alertas no están corriendo: revisar `journalctl -u mediadev-vigia`.")
        estado["fallo_vigia"] = ahora
        escribir_json(ESTADO_DIR / "estado.json", estado)


def probar_telegram() -> int:
    """Verifica el bot (getMe) y manda un mensaje de prueba al chat configurado."""
    yo = telegram_getme()
    if not yo:
        print("Telegram: no responde o el token es inválido")
        return 1
    esperado = os.environ.get("VIGIA_BOT_USUARIO")
    print(f"bot: @{yo.get('username')} (id {yo.get('id')})")
    if esperado and yo.get("username") != esperado:
        print(f"el bot no es el esperado ({esperado})")
        return 1
    mid = telegram(f"🔧 Prueba del vigía de mediaCAP · {hora_hn()} HN. Si lees esto, las alertas llegan aquí.")
    print("mensaje de prueba enviado" if mid else "no se pudo enviar al chat configurado")
    return 0 if mid else 1


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probar-telegram", action="store_true", help="getMe + mensaje de prueba")
    ap.add_argument("--simular", action="store_true",
                    help="una vuelta completa sin mandar nada a Telegram (imprime los mensajes)")
    a = ap.parse_args()
    if a.probar_telegram:
        sys.exit(probar_telegram())
    if a.simular:
        os.environ["VIGIA_SIMULAR"] = "1"
    ahora = time.time()
    try:
        vuelta(ahora)
    except Exception as e:
        avisar_fallo(e, ahora)
        raise


if __name__ == "__main__":
    main()
