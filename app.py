import os
import asyncio
import threading
from flask import Flask
from telegram.ext import ApplicationBuilder

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK"

# Запуск Telegram-бота в отдельном потоке
def run_bot():
    async def bot_main():
        application = ApplicationBuilder().token(os.environ['BOT_TOKEN']).build()
        print("Bot started...")
        await application.run_polling()
    
    asyncio.run(bot_main())

# Запускаем бота в фоне при старте Flask
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
