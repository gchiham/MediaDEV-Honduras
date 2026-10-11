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
    cmd = ["ssh", "-i", llave, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
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


def telegram(texto: str, responder_a: int | None = None) -> int | None:
    """Manda un mensaje; devuelve message_id (para responder en el mismo hilo) o None."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(f"[telegram desactivado]\n{texto}")
        return None
    datos = {"chat_id": chat, "text": texto[:4000], "disable_web_page_preview": "true"}
    if responder_a:
        datos["reply_to_message_id"] = str(responder_a)
        datos["allow_sending_without_reply"] = "true"
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=urllib.parse.urlencode(datos).encode())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("result", {}).get("message_id")
    except Exception as e:                                   # una alerta caída no tumba el vigía
        print(f"telegram falló: {e}")
        return None


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
