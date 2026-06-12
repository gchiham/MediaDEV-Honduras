#!/opt/media-ai/monitor/venv/bin/python3
import requests
import time
import logging
import subprocess
import psutil
import sqlite3

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/opt/media-ai/monitor/logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TOKEN = '5955525376:AAE63GJ5CwbJ6DWTTZnGch8VAVlhsM2zulY'
BASE_URL = 'https://api.telegram.org/bot' + TOKEN

def send(chat_id, text):
    try:
        r = requests.post(BASE_URL + '/sendMessage',
                         json={'chat_id': chat_id, 'text': text},
                         timeout=10)
        logger.info('Sent to %s: %s', chat_id, r.status_code)
    except Exception as e:
        logger.error('Send error: %s', e)

def handle(msg):
    chat_id = msg['chat']['id']
    text = msg.get('text', '')
    user = msg.get('from', {}).get('username', '?')
    logger.info('Msg from %s (%s): %s', user, chat_id, text)

    if text.startswith('/start') or text.startswith('/help'):
        send(chat_id, (
            '\U0001f3b5 MediaDEV Bot\n\n'
            '\U0001f4ca /status  — Estado del servidor\n'
            '\U0001f510 /wg      — WireGuard peers\n'
            '\U0001f6a8 /alerts  — Últimos eventos\n'
            '\U0001f4fb /streams — Streams activos'
        ))

    elif text.startswith('/status'):
        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            uptime = int(time.time() - psutil.boot_time())
            hours = uptime // 3600
            mins = (uptime % 3600) // 60
            ram_used = mem.used / (1024**3)
            ram_total = mem.total / (1024**3)
            disk_used = disk.used / (1024**3)
            disk_total = disk.total / (1024**3)
            # CPU emoji based on usage
            cpu_icon = '\U0001f7e2' if cpu < 50 else '\U0001f7e1' if cpu < 80 else '\U0001f534'
            ram_icon = '\U0001f7e2' if mem.percent < 60 else '\U0001f7e1' if mem.percent < 85 else '\U0001f534'
            disk_icon = '\U0001f7e2' if disk.percent < 70 else '\U0001f7e1' if disk.percent < 90 else '\U0001f534'
            reply = (
                '\U0001f5a5 Server Status\n'
                '━━━━━━━━━━\n\n'
                '%s CPU:    %.1f%%\n'
                '%s RAM:    %.1f/%.1f GB (%.1f%%)\n'
                '%s Disco:  %.1f/%.1f GB (%.1f%%)\n\n'
                '⏱ Uptime: %dh %dm'
            ) % (
                cpu_icon, cpu,
                ram_icon, ram_used, ram_total, mem.percent,
                disk_icon, disk_used, disk_total, disk.percent,
                hours, mins
            )
            send(chat_id, reply)
        except Exception as e:
            send(chat_id, 'Error: ' + str(e))

    elif text.startswith('/wg'):
        try:
            import re
            r = subprocess.run(['wg', 'show', 'wg0'],
                               capture_output=True, text=True, timeout=5)
            out = r.stdout

            def get_peer_info(key):
                lines = out.split('\n')
                in_peer = False
                handshake_str = None
                for line in lines:
                    if 'peer:' in line:
                        in_peer = key in line
                    if in_peer and 'latest handshake' in line:
                        handshake_str = line.split(':', 1)[1].strip()
                if not handshake_str:
                    return False, 'Sin conexion'
                total = 0
                for p, m in [(r'(\d+)\s+days?', 86400), (r'(\d+)\s+hours?', 3600),
                             (r'(\d+)\s+minutes?', 60), (r'(\d+)\s+seconds?', 1)]:
                    match = re.search(p, handshake_str)
                    if match:
                        total += int(match.group(1)) * m
                online = total < 180
                # Format handshake nicely
                if total < 60:
                    hs_fmt = '%ds ago' % total
                elif total < 3600:
                    hs_fmt = '%dm %ds ago' % (total // 60, total % 60)
                elif total < 86400:
                    hs_fmt = '%dh %dm ago' % (total // 3600, (total % 3600) // 60)
                else:
                    hs_fmt = '%dd ago' % (total // 86400)
                return online, hs_fmt

            PEERS = [
                ('Xt4N', '\U0001f353', 'Raspberry Pi ', '10.101.0.2'),
                ('WeJu', '\U0001f4bb', 'PC Sedesol   ', '10.101.0.3'),
                ('DPNe', '\U0001f4bb', 'PC Developer ', '10.101.0.4'),
                ('ESXX', '\U0001f4bb', 'PC-LCE       ', '10.101.0.5'),
            ]

            gateway_ip = None
            try:
                with open('/etc/privoxy/config', 'r') as f:
                    for line in f:
                        m = re.search(r'forward-socks5\s+/\s+([\d.]+):', line)
                        if m:
                            gateway_ip = m.group(1)
                            break
            except Exception:
                pass

            reply  = '\U0001f510 WireGuard Peers\n'
            reply += '━━━━━━━━━━━━━━━━\n\n'
            for key, icon, name, ip in PEERS:
                online, hs = get_peer_info(key)
                status = '\U0001f7e2 ONLINE' if online else '\U0001f534 OFFLINE'
                gw_tag = '  \U0001f4e1 STREAMS' if ip == gateway_ip else ''
                reply += '%s %s%s\n' % (icon, name, gw_tag)
                reply += '   %s\n' % status
                reply += '   \U0001f552 %s\n\n' % hs
            send(chat_id, reply)
        except Exception as e:
            send(chat_id, 'Error: ' + str(e))

    elif text.startswith('/streams'):
        try:
            r = subprocess.run(['supervisorctl', 'status'],
                               capture_output=True, text=True, timeout=5)
            lines = r.stdout.strip().split('\n')
            running = [l for l in lines if 'RUNNING' in l]
            backoff = [l for l in lines if 'BACKOFF' in l]
            stopped = [l for l in lines if 'STOPPED' in l]
            total = len([l for l in lines if 'stream' in l])
            status_icon = '\U0001f7e2' if len(running) == total else '\U0001f7e1' if len(running) > 0 else '\U0001f534'
            reply  = '\U0001f4fb Streams\n'
            reply += '━━━━━━━━━━\n\n'
            reply += '%s Running: %d/%d\n' % (status_icon, len(running), total)
            if backoff:
                reply += '\U0001f7e1 Backoff:  %d\n' % len(backoff)
            if stopped:
                reply += '\U0001f534 Stopped:  %d\n' % len(stopped)
            send(chat_id, reply)
        except Exception as e:
            send(chat_id, 'Error: ' + str(e))

    elif text.startswith('/alerts'):
        try:
            con = sqlite3.connect('/opt/media-ai/monitor/events.db')
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute('SELECT * FROM events ORDER BY ts DESC LIMIT 10')
            rows = cur.fetchall()
            con.close()
            if not rows:
                send(chat_id, '\U0001f4ed Sin eventos recientes')
                return
            reply = '\U0001f6a8 Eventos Recientes\n'
            reply += '━━━━━━━━━━\n\n'
            for row in rows:
                icon = '✅' if 'online' in row['type'].lower() else '\U0001f6a8'
                reply += '%s %s\n   %s\n\n' % (icon, row['peer'], row['ts'])
            send(chat_id, reply[:4000])
        except Exception as e:
            send(chat_id, 'Error: ' + str(e))

logger.info('Bot starting (simple requests mode)...')
requests.post(BASE_URL + '/deleteWebhook', json={'drop_pending_updates': True})
logger.info('Webhook cleared, polling...')

offset = 0
while True:
    try:
        resp = requests.get(
            BASE_URL + '/getUpdates',
            params={'offset': offset, 'timeout': 30, 'allowed_updates': ['message']},
            timeout=35
        )
        data = resp.json()
        if not data.get('ok'):
            logger.error('API Error: %s', data)
            time.sleep(5)
            continue
        for update in data.get('result', []):
            offset = update['update_id'] + 1
            if 'message' in update:
                handle(update['message'])
    except requests.exceptions.Timeout:
        continue
    except Exception as e:
        logger.error('Error: %s', e)
        time.sleep(5)
