#!/opt/media-ai/monitor/venv/bin/python3
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('logs/bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

TOKEN = '5955525376:AAE63GJ5CwbJ6DWTTZnGch8VAVlhsM2zulY'

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info('Comando /start')
    await update.message.reply_text('✅ Bot Online!\n/status /wg /alerts /streams')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info('Comando /status')
    import psutil, time
    try:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        uptime = int(time.time() - psutil.boot_time())
        h = uptime // 3600
        text = f'🖥 Status\n\nCPU: {cpu:.1f}%\nRAM: {mem.used//(1024**2)}/{mem.total//(1024**2)} MB\nDisk: {disk.used//(1024**3)}/{disk.total//(1024**3)} GB\nUptime: {h}h'
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

async def wg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info('Comando /wg')
    import subprocess
    try:
        r = subprocess.run(['wg', 'show', 'wg0'], capture_output=True, text=True, timeout=5)
        text = '🔐 WireGuard\n\n'
        text += f'Raspberry Pi: {🟢 if Xt4N in r.stdout else 🔴}\nPC Sedesol: {🟢 if WeJu in r.stdout else 🔴}\nPC-LCE: {🟢 if ESXXCvl in r.stdout else 🔴}'
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info('Comando /alerts')
    import sqlite3
    try:
        con = sqlite3.connect('events.db')
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute('SELECT * FROM events ORDER BY ts DESC LIMIT 10')
        rows = cur.fetchall()
        con.close()
        if not rows:
            await update.message.reply_text('No events')
            return
        text = '🚨 Events\n\n'
        for r in rows:
            text += f'{r[ts]}: {r[type]} - {r[peer]}\n'
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

async def streams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info('Comando /streams')
    import subprocess
    try:
        r = subprocess.run(['supervisorctl', 'status'], capture_output=True, text=True, timeout=5)
        running = len([l for l in r.stdout.split('\n') if 'RUNNING' in l])
        total = len([l for l in r.stdout.split('\n') if 'stream' in l])
        await update.message.reply_text(f'🎵 Streams\n\nRunning: {running}/{total}')
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

if __name__ == '__main__':
    logger.info('Bot iniciando...')
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('wg', wg))
    app.add_handler(CommandHandler('alerts', alerts))
    app.add_handler(CommandHandler('streams', streams))
    
    logger.info('Iniciando polling...')
    app.run_polling(allowed_updates=['message'])
