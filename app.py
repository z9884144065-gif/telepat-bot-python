import os
import re
import asyncio
import threading
import requests
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from supabase import create_client

app = Flask(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
SUPABASE_URL = 'https://jutszxuzjzyfxarceydw.supabase.co'
SUPABASE_KEY = 'sb_publishable_geRczpRc3faUHRGto2ue7A_eFFiJDwO'
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Webhook URL — если домен Render изменится, поменяй здесь
WEBHOOK_URL = 'https://telepat-bot.onrender.com/webhook'

# Глобальные ссылки для webhook-режима
telegram_app = None
telegram_loop = None

notify_counters = {}
REPLY_TTL_SECONDS = 10

TOKEN_COLUMN = {
    'TG':   'tg_balance',
    'AI':   'ai_balance',
    'TUSD': 'tusd_balance',
}


def has_letters(text):
    return bool(re.search(r'[a-zA-Zа-яА-ЯёЁ]', text))


async def delete_later(bot, chat_id: int, message_id: int, delay: int = REPLY_TTL_SECONDS):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"delete_later error: {e}", flush=True)


# ==================== /start — ПРИВЯЗКА РЕФЕРАЛА ====================
async def start_command(update, context):
    """Обработчик /start <referrer_id> — привязка реферала навсегда."""
    try:
        msg = update.effective_message
        user = msg.from_user
        if not user:
            return

        user_id = user.id

        # 1) создаём пользователя если его нет
        try:
            supabase.rpc('ensure_user_exists', {'p_telegram_id': user_id}).execute()
        except Exception as e:
            print(f"[start] ensure_user_exists error: {e}", flush=True)

        # 1.1) активируем "отложенного" реферала, если админ заранее сохранил @username
        try:
            username = user.username
            if username:
                r = supabase.rpc('activate_pending_referral', {
                    'p_user_id': user_id,
                    'p_username': username,
                }).execute()
                print(f"[start] activate_pending_referral: {r.data}", flush=True)
        except Exception as e:
            print(f"[start] activate_pending_referral error: {e}", flush=True)

        # 2) если пришёл параметр — это реферальный ID
        if context.args and len(context.args) > 0:
            raw = (context.args[0] or '').strip()
            try:
                referrer_id = int(raw)
            except (ValueError, TypeError):
                referrer_id = None

            if referrer_id and referrer_id != user_id:
                try:
                    r = supabase.rpc('set_referrer', {
                        'p_user_id': user_id,
                        'p_referrer_id': referrer_id,
                    }).execute()
                    print(f"[start] set_referrer {user_id} -> {referrer_id}: {r.data}", flush=True)
                except Exception as e:
                    print(f"[start] set_referrer error: {e}", flush=True)

        # 3) приветствие
        name = user.first_name or 'друг'
        await msg.reply_text(
            f"👑 Привет, {name}!\n\n"
            f"Добро пожаловать в TELEPAT.\n"
            f"Открой приложение кнопкой ниже 👇"
        )
    except Exception as e:
        print(f"[start] fatal: {e}", flush=True)


# ==================== ОБРАБОТКА СООБЩЕНИЙ В ГРУППАХ ====================
async def handle_message(update, context):
    """
    Все проверки (min_words, min_chars, cooldown, лимиты, бюджет, дубликаты)
    выполняются внутри RPC process_chat_message_v2 на стороне Supabase.
    """
    try:
        msg = update.effective_message
        if not msg or not msg.text:
            return
        if msg.chat.type not in ('group', 'supergroup'):
            return

        user_id = msg.from_user.id
        chat_id = msg.chat.id
        chat_title = msg.chat.title
        text = msg.text.strip()
        username = msg.from_user.username

        result = supabase.rpc('process_chat_message_v2', {
            'p_chat_telegram_id': chat_id,
            'p_telegram_id': user_id,
            'p_message_id': msg.message_id,
            'p_text': text,
            'p_username': username,
            'p_chat_title': chat_title,
        }).execute()

        data = result.data or {}

        if not data.get('eligible'):
            return

        reward = data.get('reward', 0)
        token = data.get('token_symbol', 'TUSD')

        notify_every = 1

        key = f"{chat_id}:{user_id}"
        current = notify_counters.get(key, 0) + 1

        if current >= notify_every:
            notify_counters[key] = 0
            sent = await msg.reply_text(f"✅ Вам начислено {reward} {token} за активность!")
            asyncio.create_task(delete_later(context.bot, chat_id, sent.message_id))
        else:
            notify_counters[key] = current

    except Exception as e:
        print(f"HANDLE ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def extract_chat_username(link):
    if not link:
        return None
    m = re.search(r't\.me/([a-zA-Z0-9_]+)', link)
    if not m:
        return None
    name = m.group(1)
    if name in ('+', 'joinchat', 'c'):
        return None
    return '@' + name


def tg_get_chat_member_status(chat_username, user_id):
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{BOT_TOKEN}/getChatMember',
            params={'chat_id': chat_username, 'user_id': int(user_id)},
            timeout=10,
        )
        data = r.json()
        if not data.get('ok'):
            print(f"tg_get_chat_member error: {data}", flush=True)
            return 'error'
        return data['result']['status']
    except Exception as e:
        print(f"tg_get_chat_member exception: {e}", flush=True)
        return 'error'


def get_token_balance(telegram_id: int, token_symbol: str):
    col = TOKEN_COLUMN.get(token_symbol)
    if col:
        r = (
            supabase.table('users')
            .select(col)
            .eq('telegram_id', telegram_id)
            .maybe_single()
            .execute()
        )
        return float((r.data or {}).get(col) or 0)
    tok = (
        supabase.table('telepat_tokens')
        .select('id')
        .eq('symbol', token_symbol)
        .maybe_single()
        .execute()
    )
    if not tok.data:
        return 0.0
    token_id = tok.data['id']
    r = (
        supabase.table('telepat_token_balances')
        .select('balance')
        .eq('telegram_id', telegram_id)
        .eq('token_id', token_id)
        .maybe_single()
        .execute()
    )
    return float((r.data or {}).get('balance') or 0)


def set_token_balance(telegram_id: int, token_symbol: str, value: float):
    col = TOKEN_COLUMN.get(token_symbol)
    if col:
        supabase.table('users').update({col: value}).eq('telegram_id', telegram_id).execute()
        return
    tok = (
        supabase.table('telepat_tokens')
        .select('id')
        .eq('symbol', token_symbol)
        .maybe_single()
        .execute()
    )
    if not tok.data:
        return
    token_id = tok.data['id']
    supabase.table('telepat_token_balances').upsert({
        'telegram_id': telegram_id,
        'token_id': token_id,
        'balance': value,
    }, on_conflict='telegram_id,token_id').execute()


# ==================== FLASK: WEBHOOK И ЗАДАНИЯ ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram присылает сюда updates."""
    global telegram_app, telegram_loop
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update),
            telegram_loop
        )
        return 'ok', 200
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return 'error', 500


@app.route('/check_task', methods=['POST'])
def check_task():
    try:
        data = request.get_json()
        task_id = data.get('task_id')
        user_id = data.get('user_id')

        if not task_id or not user_id:
            return jsonify({'ok': False, 'error': 'Missing params'}), 400

        task_r = supabase.table('tasks').select('*').eq('id', task_id).maybe_single().execute()
        if not task_r.data:
            return jsonify({'ok': False, 'error': 'Задание не найдено'})

        task = task_r.data
        if task.get('status') != 'approved':
            return jsonify({'ok': False, 'error': 'Задание не активно'})

        if task.get('limit_count', 0) > 0 and task.get('completed_count', 0) >= task['limit_count']:
            return jsonify({'ok': False, 'error': 'Лимит выполнений исчерпан'})

        dup_r = (
            supabase.table('task_completions')
            .select('id')
            .eq('task_id', task_id)
            .eq('user_id', str(user_id))
            .execute()
        )
        if dup_r.data:
            return jsonify({'ok': False, 'error': 'Вы уже выполняли это задание'})

        username = extract_chat_username(task.get('link'))
        if not username:
            return jsonify({'ok': False, 'error': 'Некорректная ссылка задания'})

        status = tg_get_chat_member_status(username, user_id)

        if status == 'error':
            return jsonify({'ok': False, 'error': 'Бот не админ канала — проверка невозможна'})
        if status in ('left', 'kicked'):
            return jsonify({'ok': False, 'error': 'Вы не подписаны на канал/чат'})

        reward = float(task.get('reward', 0))
        token = (task.get('reward_token') or 'TUSD').strip().upper()
        creator_id = int(task.get('creator_id'))
        user_id_int = int(user_id)

        creator_balance = get_token_balance(creator_id, token)
        if creator_balance < reward:
            return jsonify({'ok': False, 'error': 'У рекламодателя недостаточно средств'})
        set_token_balance(creator_id, token, creator_balance - reward)

        user_balance = get_token_balance(user_id_int, token)
        set_token_balance(user_id_int, token, user_balance + reward)

        supabase.table('task_completions').insert({
            'task_id': task_id,
            'user_id': str(user_id)
        }).execute()

        supabase.table('tasks').update({
            'completed_count': task.get('completed_count', 0) + 1
        }).eq('id', task_id).execute()

        print(f"TASK OK: {user_id} +{reward} {token}", flush=True)
        return jsonify({'ok': True, 'reward': reward, 'token': token})

    except Exception as e:
        print(f"CHECK_TASK ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/')
def home():
    return "Bot is running"


@app.route('/health')
def health():
    return "OK"


def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)


async def bot_main():
    global telegram_app, telegram_loop
    print("BOT INIT START...", flush=True)
    telegram_loop = asyncio.get_running_loop()

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler('start', start_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()

    # Устанавливаем webhook
    await telegram_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
    )
    print(f"Webhook установлен: {WEBHOOK_URL}", flush=True)
    print("Bot started (webhook mode)...", flush=True)

    # Держим loop живым, пока работает Flask
    await asyncio.Event().wait()


if __name__ == '__main__':
    print("STARTING FLASK THREAD...", flush=True)
    threading.Thread(target=run_flask, daemon=True).start()

    print("STARTING BOT WEBHOOK...", flush=True)
    try:
        asyncio.run(bot_main())
    except Exception as e:
        print(f"BOT FATAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
