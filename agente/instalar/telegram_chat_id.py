#!/usr/bin/python3 -I
"""Muestra los chats que le escribieron al bot del vigía (para TELEGRAM_CHAT_ID).

Uso en mediaAPP, como root, después de mandarle /start al bot desde el chat de alertas:
    python3 -I telegram_chat_id.py
Lee el token de /etc/mediadev-agent.env y NO lo imprime. Solo consulta getUpdates (lectura).
"""
import json, sys, urllib.request

ENV = "/etc/mediadev-agent.env"


def main() -> int:
    try:
        lineas = open(ENV).read().splitlines()
    except OSError as e:
        print(f"no se pudo leer {ENV}: {e.strerror}")
        return 1
    token = next((l.split("=", 1)[1].strip().strip("\"'") for l in lineas
                  if l.startswith("TELEGRAM_BOT_TOKEN=")), "")
    if not token:
        print("falta TELEGRAM_BOT_TOKEN en el archivo")
        return 1
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as r:
            datos = json.load(r)
    except Exception as e:
        print(f"Telegram no respondió: {type(e).__name__} {getattr(e, 'code', '')}")
        return 1
    vistos = {}
    for u in datos.get("result", []):
        c = (u.get("message") or u.get("my_chat_member") or u.get("channel_post") or {}).get("chat", {})
        if c.get("id") is not None:
            vistos[c["id"]] = (c.get("type"), c.get("title") or c.get("username") or c.get("first_name"))
    if not vistos:
        print("sin mensajes: mándale /start al bot desde el chat de alertas y repite")
        return 1
    for cid, (tipo, nombre) in vistos.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({tipo}: {nombre})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
