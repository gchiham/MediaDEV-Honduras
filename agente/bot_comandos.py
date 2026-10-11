#!/usr/bin/env python3
"""bot_comandos — comandos de Telegram del vigía (corre en mediaAPP como servicio).

/estado  /stats  /pausa [horas]  /reanudar  /ia_off  /ia_on  /ayuda

Reglas:
- Solo obedece al chat TELEGRAM_CHAT_ID; a cualquier otro lo ignora sin responder.
- Solo puede APAGAR: pausar alertas (máx. 24 h) o detener la IA. /ia_on no enciende nada que el
  servidor tenga apagado (VIGIA_AGENTE=1 sigue mandando).
- Ignora comandos viejos (al arrancar y los de más de 10 min): una /pausa de ayer no se aplica hoy.
"""
import json, os, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comun
from comun import (COLA_DIR, ESTADO_DIR, ahora_hn, en_pausa, escribir_json, hora_hn, ia_detenida,
                   leer_control, leer_json, telegram, telegram_api)

OFFSET = ESTADO_DIR / "bot_offset.json"
PAUSA_MAX_H = 24
EDAD_MAX_S = 600                       # comandos más viejos que esto no se ejecutan
VUELTA_ATRASADA_S = 15 * 60            # sin vuelta del vigía en 15 min = algo anda mal

MENU = [("estado", "Alertas activas y última vuelta del vigía"),
        ("stats", "Vueltas, alertas y uso de IA de hoy"),
        ("pausa", "Pausar alertas N horas (1 por defecto, máx. 24)"),
        ("reanudar", "Quitar la pausa de alertas"),
        ("ia_off", "Detener la IA (interruptor de emergencia)"),
        ("ia_on", "Volver a permitir la IA (si el servidor la tiene habilitada)"),
        ("ayuda", "Lista de comandos")]

NOMBRE = {"sin_grabar": "no graba", "ffmpeg_duplicado": "ffmpeg duplicado", "silencio": "MUDO",
          "carga": "carga alta", "disco": "disco", "servicio_caido": "servicio caído",
          "mediacap_inalcanzable": "mediaCAP no responde", "db_inalcanzable": "DB no responde"}


# ── textos ───────────────────────────────────────────────────────────────────

def estado_ia() -> str:
    if os.environ.get("VIGIA_AGENTE") != "1":
        return "apagada en la config del servidor (fase 1)"
    return "detenida desde Telegram" if ia_detenida() else "activa"


def lineas_activas(estado: dict) -> list[str]:
    activas = estado.get("activas", {})
    if not activas:
        return ["Sin alertas activas."]
    return [f"Alertas activas ({len(activas)}):"] + [
        f"• {a.get('sid') or k.split(':')[-1]} — {NOMBRE.get(a.get('tipo'), a.get('tipo'))} "
        f"desde {hora_hn(a['desde'])}" for k, a in sorted(activas.items())]


def cmd_estado(ahora: float) -> str:
    e = leer_json(ESTADO_DIR / "estado.json", {})
    u = e.get("ultima_vuelta") or {}
    lineas = [f"📋 Estado del vigía · {hora_hn(ahora)} HN"]
    if u.get("ts"):
        hace = int((ahora - u["ts"]) / 60)
        lineas.append(f"Última vuelta: {hora_hn(u['ts'])} HN (hace {hace} min) — "
                      f"grabando {u.get('grabando')} de {u.get('total')}")
        if u.get("error"):
            lineas.append("⚠ En esa vuelta mediaCAP no respondió")
        if ahora - u["ts"] > VUELTA_ATRASADA_S:
            lineas.append(f"⚠ El vigía no corre desde hace {hace} min: revisar mediadev-vigia.timer")
        if u.get("disco"):
            lineas.append(f"Disco: {u['disco'].get('usado_pct')} % · load1 {u.get('carga')}")
    else:
        lineas.append("Todavía no hay vueltas registradas.")
    lineas += lineas_activas(e)
    pausa = en_pausa(ahora)
    lineas.append(f"⏸ Alertas en pausa hasta las {hora_hn(pausa)} HN" if pausa else "🔔 Alertas activas")
    lineas.append(f"IA: {estado_ia()}")
    return "\n".join(lineas)


def investigaciones_del_dia(dia: str) -> dict:
    """Incidentes que procesó el agente ese día (archivos de la cola, nombrados por epoch en ms)."""
    res = {}
    for destino in ("hechos", "omitidos", "fallidos", "rechazados"):
        d = COLA_DIR / destino
        n = 0
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    n += ahora_hn(int(f.stem) / 1000).strftime("%Y-%m-%d") == dia
                except ValueError:
                    pass
        res[destino] = n
    return res


def gasto(dia: str) -> tuple[dict, float]:
    g = ESTADO_DIR / "gasto"
    hoy = leer_json(g / f"{dia}.json", {})
    mes = sum(float(leer_json(f, {}).get("usd", 0)) for f in g.glob(f"{dia[:7]}-*.json")) if g.is_dir() else 0.0
    return hoy, mes


def cmd_stats(ahora: float) -> str:
    dia = ahora_hn(ahora).strftime("%Y-%m-%d")
    e = leer_json(ESTADO_DIR / "estado.json", {})
    s = e.get("stats", {}).get(dia, {})
    inv = investigaciones_del_dia(dia)
    g, mes = gasto(dia)
    total_ia = inv["hechos"] + inv["omitidos"] + inv["fallidos"]
    lineas = [f"📊 Hoy {dia} (hasta las {hora_hn(ahora)} HN)",
              f"Vigía: {s.get('vueltas', 0)} vueltas · {s.get('errores', 0)} sin respuesta de mediaCAP",
              f"Alertas: {s.get('mensajes', 0)} mensajes ({s.get('alertas_nuevas', 0)} nuevas, "
              f"{s.get('recuperadas', 0)} recuperadas) · {s.get('suprimidas', 0)} suprimidas por pausa",
              f"IA: {total_ia} investigaciones ({inv['hechos']} completas, {inv['omitidos']} omitidas por tope, "
              f"{inv['fallidos']} fallidas) · {g.get('llamadas', 0)} llamadas a la API",
              f"Gasto IA: US${g.get('usd', 0):.2f} hoy (tope {os.environ.get('AGENTE_TOPE_USD_DIA', '3')}) · "
              f"US${mes:.2f} este mes (tope {os.environ.get('AGENTE_TOPE_USD_MES', '40')})",
              f"IA: {estado_ia()}"]
    if g.get("sin_medir"):
        lineas.append(f"⚠ {len(g['sin_medir'])} llamada(s) sin métricas de uso (gasto estimado)")
    previos = [d for d in sorted(e.get("stats", {})) if d < dia][-3:]
    if previos:
        lineas.append("Días anteriores (vueltas / mensajes / IA):")
        for d in previos:
            x, i = e["stats"][d], investigaciones_del_dia(d)
            lineas.append(f"• {d}: {x.get('vueltas', 0)} / {x.get('mensajes', 0)} / "
                          f"{i['hechos'] + i['omitidos'] + i['fallidos']}")
    return "\n".join(lineas)


def guardar_control(cambios: dict, ahora: float, accion: str) -> None:
    c = leer_control()
    c.update(cambios)
    c["ultimo_cambio"] = {"accion": accion, "ts": ahora, "por": "telegram"}
    escribir_json(comun.CONTROL, c)
    print(f"control: {accion}")


def cmd_pausa(args: list[str], ahora: float) -> str:
    texto = (args[0] if args else "1").lower().rstrip("h")
    if not re.fullmatch(r"\d{1,2}", texto) or not 1 <= int(texto) <= PAUSA_MAX_H:
        return f"Uso: /pausa [horas], entre 1 y {PAUSA_MAX_H}. Ej.: /pausa 2"
    hasta = ahora + int(texto) * 3600
    guardar_control({"pausa_hasta": hasta}, ahora, f"pausa {texto} h")
    return (f"⏸ Alertas en pausa hasta las {hora_hn(hasta)} HN ({texto} h).\n"
            f"El vigía sigue chequeando; el resumen diario sale igual. /reanudar para quitarla.")


def cmd_reanudar(ahora: float) -> str:
    if not en_pausa(ahora):
        return "No había pausa: las alertas ya estaban activas."
    guardar_control({"pausa_hasta": None}, ahora, "reanudar")
    e = leer_json(ESTADO_DIR / "estado.json", {})
    return "\n".join(["🔔 Alertas reanudadas."] + lineas_activas(e))


def cmd_ia(encender: bool, ahora: float) -> str:
    if not encender:
        guardar_control({"ia_detenida": True}, ahora, "ia_off")
        return ("🛑 IA detenida. No se encolan ni se corren investigaciones; las alertas simples siguen.\n"
                "/ia_on para volver a permitirla.")
    if os.environ.get("VIGIA_AGENTE") != "1":
        return ("La IA está apagada en la configuración del servidor (fase 1). Desde Telegram solo se "
                "puede apagar, no encender: hay que habilitarla en el servidor.")
    guardar_control({"ia_detenida": False}, ahora, "ia_on")
    return "✅ IA permitida otra vez (con los mismos topes de gasto)."


def ayuda() -> str:
    return "Comandos del vigía de mediaCAP:\n" + "\n".join(f"/{c} — {d}" for c, d in MENU)


def responder(texto: str, ahora: float) -> str | None:
    """Respuesta a un comando, o None si el mensaje no es un comando."""
    partes = texto.strip().split()
    if not partes or not partes[0].startswith("/"):
        return None
    cmd = partes[0][1:].split("@", 1)[0].lower()
    args = partes[1:]
    if cmd == "estado":
        return cmd_estado(ahora)
    if cmd == "stats":
        return cmd_stats(ahora)
    if cmd in ("pausa", "stop"):
        return cmd_pausa(args, ahora)
    if cmd in ("reanudar", "seguir"):
        return cmd_reanudar(ahora)
    if cmd == "ia_off":
        return cmd_ia(False, ahora)
    if cmd == "ia_on":
        return cmd_ia(True, ahora)
    if cmd in ("ayuda", "start", "help"):
        return ayuda()
    return f"No conozco /{cmd[:30]}.\n\n" + ayuda()


# ── bucle ────────────────────────────────────────────────────────────────────

def autorizado(msg: dict) -> bool:
    chat = str((msg.get("chat") or {}).get("id"))
    return bool(os.environ.get("TELEGRAM_CHAT_ID")) and chat == os.environ["TELEGRAM_CHAT_ID"].strip()


def procesar(updates: list, ahora: float) -> int | None:
    """Atiende un lote de updates; devuelve el próximo offset (o None si no hubo)."""
    siguiente = None
    for u in updates:
        siguiente = max(siguiente or 0, u.get("update_id", 0) + 1)
        msg = u.get("message") or {}
        texto = msg.get("text")
        if not texto:
            continue
        if not autorizado(msg):
            print(f"ignorado: mensaje de un chat no autorizado ({(msg.get('chat') or {}).get('type')})")
            continue
        if ahora - msg.get("date", 0) > EDAD_MAX_S:
            print("ignorado: comando viejo")
            continue
        r = responder(texto, ahora)
        if r:
            telegram(r, responder_a=msg.get("message_id"))
    return siguiente


def descartar_pendientes() -> int | None:
    """Al arrancar sin offset guardado: saltar lo acumulado (no ejecutar comandos de otro momento)."""
    ultimos = telegram_api("getUpdates", {"offset": "-1", "timeout": "0"}) or []
    return ultimos[-1]["update_id"] + 1 if ultimos else None


def bucle() -> None:
    telegram_api("setMyCommands", {"commands": json.dumps(
        [{"command": c, "description": d} for c, d in MENU])})
    offset = leer_json(OFFSET, {}).get("offset") or descartar_pendientes()
    print("bot de comandos del vigía escuchando")
    while True:
        datos = {"timeout": "50", "allowed_updates": '["message"]'}
        if offset:
            datos["offset"] = str(offset)
        updates = telegram_api("getUpdates", datos, timeout=60)
        if updates is None:                       # red o Telegram caído: esperar y reintentar
            time.sleep(10)
            continue
        nuevo = procesar(updates, time.time())
        if nuevo:
            offset = nuevo
            escribir_json(OFFSET, {"offset": offset})


if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_CHAT_ID") or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        sys.exit("faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    bucle()
