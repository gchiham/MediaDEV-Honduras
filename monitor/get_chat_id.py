#!/opt/media-ai/monitor/venv/bin/python3
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TOKEN = '5955525376:AAGJyJ85wbnq0QtGHELTiP0Un86IQaZNYAo'
logging.basicConfig(level=logging.INFO)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'\n============================================')
    print(f'✅ CHAT_ID = {update.effective_chat.id}')
    print(f'User: {update.effective_user.first_name}')
    print(f'Message: {update.message.text}')
    print(f'============================================\n')
    await update.message.reply_text(f'Tu CHAT_ID es: {update.effective_chat.id}')

app = Application.builder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

print('\n🔄 Esperando mensajes en Telegram...')
print('Envía cualquier mensaje a tu bot para obtener tu CHAT_ID\n')

import asyncio
asyncio.run(app.run_polling())
