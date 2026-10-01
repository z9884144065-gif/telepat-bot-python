import os
import re
import asyncio
import threading
from flask import Flask, request, jsonify
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram import Bot
from supabase import create_client

app = Flask(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
SUPABASE_URL = 'https://jutszxuzjzyfxarceydw.supabase.co'
SUPABASE_KEY = 'sb_publishable_geRczpRc3faUHRGto2ue7A_eFFiJDwO'
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

notify_counters = {}

# Сколько секунд держать ответ бота перед удалением
REPLY_TTL_SECONDS = 10

# Колонки в users для базовых токенов.
# Для VKCOIN и других кастомных — работаем через telepat_token_balances.
TOKEN_COLUMN = {
    'TG':   'tg_balance',
    'AI':   'ai_balance',
    'TUSD': 'tusd_balance',
}


def has_letters(text):
    return bool(re.search(r'[a-zA-Zа-яА-ЯёЁ]', text))


def get_chat_settings(chat_id):
    try:
        r = (
            supabase.table('chat_rewards')
            .select('*')
            .eq('chat_id', str(chat_id))
            .maybe_single()
            .execute()
        )
        return r.data if r.data else {}
    except Exception as e:
        print(f"get_chat_settings error: {e}", flush=True)
        return {}


def ensure_chat_exists(chat_id, chat_title):
    try:
        r = (
            supabase.table('chat_rewards')
            .select('chat_id')
            .eq('chat_id', str(chat_id))
            .maybe_single()
            .execute()
        )
        if not r.data:
            # chat_rewards.reward_token_id — NOT NULL, поэтому
            # при создании нового чата ставим дефолтный TUSD.
            tusd = (
                supabase.table('telepat_tokens')
                .select('id')
                .eq('symbol', 'TUSD')
                .maybe_single()
                .execute()
            )
            tusd_id = tusd.data.get('id') if tusd.data else None
            supabase.table('chat_rewards').insert({
                'chat_id': str(chat_id),
                'chat_title': chat_title or str(chat_id),
                'reward_token': 'TUSD',
                'reward_token_id': tusd_id,
                'reward': 0.0002,
                'enabled': True,
            }).execute()
            print(f"[ensure_chat_exists] created chat_rewards for {chat_id} (TUSD)", flush=True)
        else:
            supabase.table('chat_rewards').update(
                {'chat_title': chat_title}
            ).eq('chat_id', str(chat_id)).execute()
    except Exception as e:
        print(f"ensure_chat_exists error: {e}", flush=True)


async def delete_later(bot, chat_id: int, message_id: int, delay: int = REPLY_TTL_SECONDS):
    """Удаляет сообщение бота через delay секунд."""
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        # Если бот не админ или сообщение уже удалено — просто логируем.
        print(f"delete_later error: {e}", flush=True)


async def handle_message(update, context):
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

        ensure_chat_exists(chat_id, chat_title)
        settings = get_chat_settings(chat_id)

        if not settings.get('enabled', True):
            return
        if settings.get('exclude_commands', True) and text.startswith('/'):
            return

        min_chars = settings.get('min_chars', 0) or 0
        if min_chars and len(text) < min_chars:
            return

        min_words = settings.get('min_words', 0) or 0
        if min_words and len(text.split()) < min_words:
            return

        if settings.get('exclude_emoji_only', False) and not has_letters(text):
            return

        result = supabase.rpc('add_chat_reward', {
            'p_telegram_id': str(user_id),
            'p_chat_id': str(chat_id)
        }).execute()

        if not (result.data and result.data.get('ok')):
            return

        reward = result.data.get('reward', 0)
        token = result.data.get('token', 'TUSD')
        notify_every = result.data.get('notify_every_n', 1) or 1

        key = f"{chat_id}:{user_id}"
        current = notify_counters.get(key, 0) + 1

        if current >= notify_every:
            notify_counters[key] = 0
            sent = await msg.reply_text(f"✅ Вам начислено {reward} {token} за активность!")
            # авто-удаление ответа через REPLY_TTL_SECONDS секунд
            asyncio.create_task(delete_later(context.bot, chat_id, sent.message_id))
        else:
            notify_counters[key] = current
    except Exception as e:
        print(f"HANDLE ERROR: {e}", flush=True)


def extract_chat_username(link):
    """Извлекает @username из ссылки вида https://t.me/xxx"""
    if not link:
        return None
    m = re.search(r't\.me/([a-zA-Z0-9_]+)', link)
    if not m:
        return None
    name = m.group(1)
    if name in ('+', 'joinchat', 'c'):
        return None
    return '@' + name


def get_token_balance(telegram_id: int, token_symbol: str):
    """Возвращает текущий баланс пользователя по токену."""
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
    # кастомный токен → telepat_token_balances
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
    """Устанавливает баланс пользователя по токену."""
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


@app.route('/check_task', methods=['POST'])
def check_task():
    """Проверяет подписку через Telegram API и начисляет награду."""
    try:
        data = request.get_json()
        task_id = data.get('task_id')
        user_id = data.get('user_id')

        if not task_id or not user_id:
            return jsonify({'ok': False, 'error': 'Missing params'}), 400

        task_r = (
            supabase.table('tasks')
            .select('*')
            .eq('id', task_id)
            .maybe_single()
            .execute()
        )
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

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def check():
            async with Bot(token=BOT_TOKEN) as b:
                try:
                    member = await b.get_chat_member(chat_id=username, user_id=int(user_id))
                    return member.status
                except Exception as e:
                    print(f"CHECK ERROR: {e}", flush=True)
                    return 'error'

        status = loop.run_until_complete(check())
        loop.close()

        if status == 'error':
            return jsonify({'ok': False, 'error': 'Бот не админ канала — проверка невозможна'})
        if status in ('left', 'kicked'):
            return jsonify({'ok': False, 'error': 'Вы не подписаны на канал/чат'})

        reward = float(task.get('reward', 0))
        token = (task.get('reward_token') or 'TUSD').strip().upper()
        creator_id = int(task.get('creator_id'))
        user_id_int = int(user_id)

        # --- Списываем у рекламодателя ---
        creator_balance = get_token_balance(creator_id, token)
        if creator_balance < reward:
            return jsonify({'ok': False, 'error': 'У рекламодателя недостаточно средств'})
        set_token_balance(creator_id, token, creator_balance - reward)

        # --- Начисляем получателю ---
        user_balance = get_token_balance(user_id_int, token)
        set_token_balance(user_id_int, token, user_balance + reward)

        # --- Отметки выполнения ---
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
