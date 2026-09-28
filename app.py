import os
import asyncio
import threading
from flask import Flask
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from supabase import create_client

app = Flask(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
SUPABASE_URL = 'https://jutszxuzjzyfxarceydw.supabase.co'
SUPABASE_KEY = 'sb_publishable_geRczpRc3faUHRGto2ue7A_eFFiJDwO'
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

message_counters = {}

# ⭐ ЗА СКОЛЬКО СООБЩЕНИЙ НАЧИСЛЯТЬ (1 = за каждое)
MESSAGES_PER_REWARD = 1

async def handle_message(update, context):
    try:
        msg = update.effective_message
        if not msg or not msg.text: return
        if msg.text.startswith('/'): return
        if msg.chat.type not in ('group', 'supergroup'): return
        
        user_id = msg.from_user.id
        chat_id = msg.chat.id
        key = f"{chat_id}:{user_id}"
        
        current = message_counters.get(key, 0) + 1
        print(f"MSG from {user_id} in {chat_id}: count={current}", flush=True)
        
        if current >= MESSAGES_PER_REWARD:
            message_counters[key] = 0
            try:
                result = supabase.rpc('add_chat_reward', {
                    'p_telegram_id': str(user_id),
                    'p_chat_id': str(chat_id)
                }).execute()
                
                if result.data and result.data.get('ok'):
                    reward = result.data.get('reward', 0)
                    token = result.data.get('token', 'TUSD')
                    await msg.reply_text(f"✅ Вам начислено {reward} {token} за активность!")
                    print(f"REWARD to {user_id}: {reward} {token}", flush=True)
            except Exception as e:
                print(f"REWARD ERROR: {e}", flush=True)
        else:
            message_counters[key] = current
    except Exception as e:
        print(f"HANDLE ERROR: {e}", flush=True)

def run_bot():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def bot_main():
            print("BOT INIT START...", flush=True)
            application = ApplicationBuilder().token(BOT_TOKEN).build()
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            await application.initialize()
            await application.start()
            await application.updater.start_polling()
            print("Bot started...", flush=True)
            while True:
                await asyncio.sleep(3600)
        
        loop.run_until_complete(bot_main())
    except Exception as e:
        print(f"BOT FATAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()

print("STARTING BOT THREAD...", flush=True)
threading.Thread(target=run_bot, daemon=True).start()

@app.route('/')
def home():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
