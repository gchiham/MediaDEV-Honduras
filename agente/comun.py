"""Piezas compartidas por vigia.py y agente.py (corren en mediaAPP).

Toda la configuración sale del entorno (systemd: EnvironmentFile=/etc/mediadev-agent.env).
"""
import json, os, subprocess, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ESTADO_DIR = Path(os.environ.get("VIGIA_ESTADO_DIR", "/var/lib/mediadev-vigia"))
COLA_DIR = ESTADO_DIR / "cola"
AUDITORIA_DIR = Path(os.environ.get("AGENTE_AUDITORIA_DIR", "/var/log/mediadev-agent"))
HN = timezone(timedelta(hours=-6))          # Honduras sin DST: solo para mostrar


def ahora_hn(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), HN)


def hora_hn(ts: float | None = None) -> str:
    return ahora_hn(ts).strftime("%H:%M")


def ssh_mediacap(comando: str, timeout: int = 60) -> dict:
    """Ejecuta un subcomando de mediacap-ro. La llave solo puede correr ese script (command=)."""
    destino = os.environ.get("MEDIACAP_SSH")                 # vigia@<IP privada de mediaCAP>
    if not destino:
        return {"error": "MEDIACAP_SSH no configurado"}
    llave = os.environ.get("MEDIACAP_SSH_KEY", "/etc/mediadev-vigia/id_ed25519")
    hosts = os.environ.get("MEDIACAP_KNOWN_HOSTS", "/etc/mediadev-vigia/known_hosts")
    ssh_bin = os.environ.get("MEDIACAP_SSH_BIN", "ssh")          # solo las pruebas lo cambian
    cmd = [ssh_bin, "-i", llave, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts}", destino, comando]
    try:
        # entorno mínimo: ssh no hereda la API key, el token de Telegram ni la URL de la DB
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": f"ssh: timeout de {timeout}s"}
    if p.returncode != 0:
        return {"error": f"ssh rc={p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}"}
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"error": "respuesta no JSON", "inicio": p.stdout[:500]}


def _api_telegram() -> str | None:
    """Base de la API: https://api.telegram.org, salvo un servidor local en las pruebas."""
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    p = urllib.parse.urlsplit(base)
    if p.scheme == "https" or (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost")):
        return base
    return None


def _token_permitido(token: str) -> bool:
    """El vigía usa un bot PROPIO: si el token es de un bot ya usado por otro servicio (ids en
    VIGIA_BOT_PROHIBIDOS, p. ej. el del monitor viejo), no se usa."""
    prohibidos = {x.strip() for x in os.environ.get("VIGIA_BOT_PROHIBIDOS", "").split(",") if x.strip()}
    return token.split(":", 1)[0] not in prohibidos


def _llamar_telegram(metodo: str, datos: dict | None = None, timeout: int = 15) -> dict | list | None:
    token, base = os.environ.get("TELEGRAM_BOT_TOKEN"), _api_telegram()
    if not token or not base:
        return None
    if not _token_permitido(token):
        print("telegram: el token pertenece a un bot prohibido (reutilizado); no se usa")
        return None
    req = urllib.request.Request(f"{base}/bot{token}/{metodo}",
                                 data=urllib.parse.urlencode(datos).encode() if datos else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
        return res.get("result") if res.get("ok") else None
    except Exception as e:                                   # una alerta caída no tumba el vigía
        print(f"telegram falló: {type(e).__name__} {getattr(e, 'code', '')}".strip())
        return None


def telegram(texto: str, responder_a: int | None = None, teclado: dict | None = None) -> int | None:
    """Manda un mensaje; devuelve message_id (para responder en el mismo hilo) o None.
    teclado: botones inline (p. ej. 👍/👎 de un diagnóstico)."""
    if os.environ.get("VIGIA_SIMULAR") == "1":
        print(f"[SIMULADO, no se envió]\n{texto}")
        return None
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not chat:
        print(f"[telegram desactivado]\n{texto}")
        return None
    datos = {"chat_id": chat, "text": texto[:4000], "disable_web_page_preview": "true"}
    if responder_a:
        datos["reply_to_message_id"] = str(responder_a)
        datos["allow_sending_without_reply"] = "true"
    if teclado:
        datos["reply_markup"] = json.dumps(teclado)
    res = _llamar_telegram("sendMessage", datos)
    return res.get("message_id") if res else None


def telegram_getme() -> dict | None:
    return _llamar_telegram("getMe")


def telegram_api(metodo: str, datos: dict | None = None, timeout: int = 15):
    """Llamada genérica (getUpdates, setMyCommands) con las mismas reglas: https, bot no prohibido."""
    return _llamar_telegram(metodo, datos, timeout)


# Control desde Telegram (lo escribe bot_comandos.py, lo leen vigia.py y agente.py).
# Solo puede APAGAR cosas: pausar alertas o detener la IA. Encender la IA sigue dependiendo de
# VIGIA_AGENTE=1 en el entorno del servidor.
CONTROL = ESTADO_DIR / "control.json"


def leer_control() -> dict:
    return leer_json(CONTROL, {})


def en_pausa(ahora: float | None = None) -> float | None:
    """Hasta cuándo están pausadas las alertas (epoch), o None si no hay pausa vigente."""
    hasta = leer_control().get("pausa_hasta")
    return hasta if isinstance(hasta, (int, float)) and (ahora or time.time()) < hasta else None


def ia_detenida() -> bool:
    return leer_control().get("ia_detenida") is True


def leer_json(path: Path, defecto):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return defecto


def escribir_json(path: Path, datos) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1, default=str))
    tmp.replace(path)
