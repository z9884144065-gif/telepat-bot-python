import os
from flask import Flask
from telegram.ext import ApplicationBuilder
import asyncio

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK"

async def run_bot():
    application = ApplicationBuilder().token(os.environ['BOT_TOKEN']).build()
    print("Bot is running...")
    await application.run_polling()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
