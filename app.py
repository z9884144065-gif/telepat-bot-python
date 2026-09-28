import os
import re
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

notify_counters = {}

def has_letters(text):
    return bool(re.search(r'[a-zA-Zа-яА-ЯёЁ]', text))

def ensure_chat_exists(chat_id):
    """Создаёт запись в chat_rewards, если её ещё нет."""
    try:
        r = supabase.table('chat_rewards').select('chat_id').eq('chat_id', str(chat_id)).maybe_single().execute()
        if not r.data:
            supabase.table('chat_rewards').insert({'chat_id': str(chat_id)}).execute()
            print(f"CREATED new chat row: {chat_id}", flush=True)
            return True
        return False
    except Exception as e:
        print(f"ensure_chat_exists error: {e}", flush=True)
        return False

def get_chat_settings(chat_id):
    try:
        r = supabase.table('chat_rewards').select('*').eq('chat_id', str(chat_id)).maybe_single().execute()
        return r.data if r.data else {}
    except Exception as e:
        print(f"SETTINGS ERROR: {e}", flush=True)
        return {}

async def handle_message(update, context):
    try:
        msg = update.effective_message
        if not msg or not msg.text: return
        if msg.chat.type not in ('group', 'supergroup'): return
        
        user_id = msg.from_user.id
        chat_id = msg.chat.id
        text = msg.text.strip()
        
        print(f"MSG from {user_id} in {chat_id}: '{text[:30]}'", flush=True)
        
        # === СНАЧАЛА ГАРАНТИРУЕМ, ЧТО ЧАТ ЕСТЬ В БАЗЕ ===
        ensure_chat_exists(chat_id)
        
        settings = get_chat_settings(chat_id)
        
        if not settings.get('enabled', True):
            print(f"CHAT DISABLED: {chat_id}", flush=True)
            return
        
        if settings.get('exclude_commands', True) and text.startswith('/'):
            print(f"SKIP command", flush=True)
            return
        
        min_chars = settings.get('min_chars', 0) or 0
        if min_chars and len(text) < min_chars:
            print(f"SKIP: short ({len(text)}<{min_chars})", flush=True)
            return
        
        min_words = settings.get('min_words', 0) or 0
        if min_words and len(text.split()) < min_words:
            print(f"SKIP: few words", flush=True)
            return
        
        if settings.get('exclude_emoji_only', False) and not has_letters(text):
            print(f"SKIP: emoji only", flush=True)
            return
        
        result = supabase.rpc('add_chat_reward', {
            'p_telegram_id': str(user_id),
            'p_chat_id': str(chat_id)
        }).execute()
        
        if not (result.data and result.data.get('ok')):
            reason = (result.data or {}).get('error', 'unknown')
            print(f"SKIP RPC: {reason}", flush=True)
            return
        
        reward = result.data.get('reward', 0)
        token = result.data.get('token', 'TUSD')
        notify_every = result.data.get('notify_every_n', 1) or 1
        
        print(f"REWARD OK: +{reward} {token} to {user_id}", flush=True)
        
        key = f"{chat_id}:{user_id}"
        current = notify_counters.get(key, 0) + 1
        
        if current >= notify_every:
            notify_counters[key] = 0
            await msg.reply_text(f"✅ Вам начислено {reward} {token} за активность!")
            print(f"REPLY sent to {user_id}", flush=True)
        else:
            notify_counters[key] = current
    except Exception as e:
        print(f"HANDLE ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()

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
