#!/opt/media-ai/monitor/venv/bin/python3
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = '5955525376:AAGJyJ85wbnq0QtGHELTiP0Un86IQaZNYAo'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name
    
    print(f'\n🎯 ============================================')
    print(f'✅ TU CHAT_ID ES: {chat_id}')
    print(f'Usuario: {user}')
    print(f'🎯 ============================================\n')
    
    logger.info(f'CHAT_ID={chat_id}')
    
    await update.message.reply_text(
        f'Tu CHAT_ID es:\n\n\n\n'
        'Cópialo y dile al asistente.'
    )

async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    print(f'\n🎯 CHAT_ID: {chat_id}\n')
    logger.info(f'Message from CHAT_ID={chat_id}: {update.message.text}')
    
    await update.message.reply_text(
        f'Tu CHAT_ID es:\n\n\n\n'
        'Cópialo y dile al asistente.'
    )

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler('start', start))
app.add_handler(MessageHandler(filters.TEXT, any_message))

logger.info('Bot temporal iniciado - esperando mensajes...')
print('\n🔄 Bot temporal escuchando...')
print('Envía cualquier mensaje a tu bot en Telegram\n')

import asyncio
asyncio.run(app.run_polling())
