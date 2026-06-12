#!/opt/media-ai/monitor/venv/bin/python3
import subprocess, time, logging, sqlite3, re, requests
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('logs/monitor.log'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

TOKEN   = '5955525376:AAE63GJ5CwbJ6DWTTZnGch8VAVlhsM2zulY'
CHAT_ID = 1687412948
BASE_URL = 'https://api.telegram.org/bot' + TOKEN

# =============================================================================
# PEERS monitoreados por WireGuard.
# Para agregar un nuevo peer:
#   1. Obtener su public key: wg show wg0 peers
#   2. Agregar aqui con nombre, IP y emoji
#   3. Reiniciar el monitor: systemctl restart mediadev-monitor
# =============================================================================
PEERS = {
    'Xt4N/0OY6UpqfZYY+cz2OVcV3wQRKJuf30DAbHDm5ng=': {'name': 'RPi-HN01',    'ip': '10.101.0.2', 'icon': '🍓'},
    'WeJuHENAEsRFwjT2phg1Vn5K3i6oTZiDFs6Wh35Kf3Q=': {'name': 'PC-Sedesol',  'ip': '10.101.0.3', 'icon': '💻'},
    'ESXXCvlAC0a+RQhbBdAcIftxc2E+2E0IU9VRmIiDOVs=': {'name': 'PC-LCE',      'ip': '10.101.0.5', 'icon': '🖥'},
    'iLWsb6GyE2bys9XOkiBUPXlx1mtxhz18TXjDmkFFjWk=': {'name': 'RPi-Levi',    'ip': '10.101.0.6', 'icon': '🍓'},
}

def send_alert(text):
    try:
        requests.post(BASE_URL + '/sendMessage',
                     json={'chat_id': CHAT_ID, 'text': text},
                     timeout=10)
        logger.info('Alert sent to Telegram')
    except Exception as e:
        logger.error('Failed to send alert: %s', e)

def handshake_to_seconds(s):
    if s == 'Never': return 99999
    total = 0
    for word, mult in [('days?', 86400), ('hours?', 3600), ('minutes?', 60), ('seconds?', 1)]:
        m = re.search(r'(\d+)\s+' + word, s)
        if m: total += int(m.group(1)) * mult
    return total

def check_online(peer_key):
    try:
        r = subprocess.run(['wg', 'show', 'wg0'], capture_output=True, text=True, timeout=10)
        if peer_key not in r.stdout: return False
        lines = r.stdout.split('\n')
        for i, line in enumerate(lines):
            if peer_key in line:
                for j in range(i, min(i+15, len(lines))):
                    if 'latest handshake' in lines[j]:
                        hs = lines[j].split(':', 1)[1].strip()
                        return handshake_to_seconds(hs) < 180
        return False
    except: return False

def init_db():
    Path('events.db').parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect('events.db')
    cur = con.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, type TEXT, peer TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP)')
    cur.execute('CREATE TABLE IF NOT EXISTS pstatus (peer_key TEXT PRIMARY KEY, name TEXT, online BOOLEAN, ts DATETIME DEFAULT CURRENT_TIMESTAMP)')
    con.commit()
    con.close()

def add_event(etype, pname):
    con = sqlite3.connect('events.db')
    con.execute('INSERT INTO events (type, peer) VALUES (?, ?)', (etype, pname))
    con.commit()
    con.close()

def update_status(pkey, pname, online):
    con = sqlite3.connect('events.db')
    con.execute('INSERT OR REPLACE INTO pstatus (peer_key, name, online, ts) VALUES (?, ?, ?, CURRENT_TIMESTAMP)', (pkey, pname, online))
    con.commit()
    con.close()

init_db()
logger.info('Monitor started')
states = {pk: check_online(pk) for pk in PEERS}

# Log estado inicial
for pk, pi in PEERS.items():
    status = 'ONLINE' if states[pk] else 'OFFLINE'
    logger.info('Initial: %s -> %s', pi['name'], status)

while True:
    for pk, pi in PEERS.items():
        online = check_online(pk)

        if states[pk] != online:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if online:
                msg = (
                    '✅ Peer conectado\n'
                    '━━━━━━━━━━━━\n\n'
                    '%s %s\n'
                    '\U0001f4cd IP VPN:  %s\n'
                    '\U0001f55b Hora:    %s (HN)'
                ) % (pi['icon'], pi['name'], pi['ip'], now)
                logger.warning('%s -> ONLINE', pi['name'])
            else:
                msg = (
                    '\U0001f534 Peer desconectado\n'
                    '━━━━━━━━━━━━\n\n'
                    '%s %s\n'
                    '\U0001f4cd IP VPN:  %s\n'
                    '\U0001f55b Hora:    %s (HN)'
                ) % (pi['icon'], pi['name'], pi['ip'], now)
                logger.warning('%s -> OFFLINE', pi['name'])

            send_alert(msg)
            add_event('peer_ONLINE' if online else 'peer_OFFLINE', pi['name'])
            states[pk] = online

        update_status(pk, pi['name'], online)

    time.sleep(60)
