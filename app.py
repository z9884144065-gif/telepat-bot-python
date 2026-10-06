import os
import re
import asyncio
import threading
import requests
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters
)
from supabase import create_client

# ==================== КОНФИГ ====================
app = Flask(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
SUPABASE_URL = 'https://jutszxuzjzyfxarceydw.supabase.co'
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

if BOT_TOKEN:
    os.environ.setdefault('BOT_TOKEN', BOT_TOKEN)
if SUPABASE_KEY:
    os.environ.setdefault('SUPABASE_KEY', SUPABASE_KEY)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

from order_publisher import start_publisher

WEBHOOK_URL = 'https://telepat-bot.onrender.com/webhook'

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


# ==================== /start ====================
async def start_command(update, context):
    try:
        msg = update.effective_message
        user = msg.from_user
        if not user:
            return
        user_id = user.id

        try:
            supabase.rpc('ensure_user_exists', {'p_telegram_id': user_id}).execute()
        except Exception as e:
            print(f"[start] ensure_user_exists error: {e}", flush=True)

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

        name = user.first_name or 'друг'
        await msg.reply_text(
            f"👑 Привет, {name}!\n\n"
            f"Добро пожаловать в TELEPAT.\n"
            f"Открой приложение кнопкой ниже 👇"
        )
    except Exception as e:
        print(f"[start] fatal: {e}", flush=True)


# ==================== /obmen ====================
async def obmen_command(update, context):
    """
    /obmen 100 TG на TUSD — создаёт ордер в p2p_orders.
    Publisher подхватит его и запостит в канал с двумя кнопками.
    """
    try:
        msg = update.effective_message
        if not msg or not msg.text:
            return
        user = msg.from_user

        parts = msg.text.strip().split()
        if len(parts) != 5 or parts[3].lower() not in ('на', 'to'):
            await msg.reply_text(
                "❌ Формат: <code>/obmen 100 TG на TUSD</code>",
                parse_mode='HTML'
            )
            return

        try:
            amount = float(parts[1].replace(',', '.'))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await msg.reply_text(
                "❌ Не понял сумму. Пример: <code>/obmen 100 TG на TUSD</code>",
                parse_mode='HTML'
            )
            return

        from_token = parts[2].upper()
        to_token = parts[4].upper()

        if from_token == to_token:
            await msg.reply_text("❌ Токены должны быть разными")
            return

        try:
            supabase.rpc('ensure_user_exists', {'p_telegram_id': user.id}).execute()
        except Exception as e:
            print(f"[obmen] ensure_user_exists: {e}", flush=True)

        try:
            bal = get_token_balance(user.id, from_token)
            if bal < amount:
                await msg.reply_text(
                    f"❌ У тебя только <b>{bal:g} {from_token}</b>, а нужно <b>{amount:g}</b>",
                    parse_mode='HTML'
                )
                return
        except Exception as e:
            print(f"[obmen] balance check: {e}", flush=True)

        try:
            row_from = supabase.table('telepat_tokens').select('price_tusd').eq('symbol', from_token).maybe_single().execute()
            row_to = supabase.table('telepat_tokens').select('price_tusd').eq('symbol', to_token).maybe_single().execute()
            if not (row_from.data and row_to.data):
                await msg.reply_text(f"❌ Токен {from_token} или {to_token} не найден")
                return
            rate = float(row_from.data['price_tusd']) / float(row_to.data['price_tusd'])
        except Exception as e:
            print(f"[obmen] rate error: {e}", flush=True)
            await msg.reply_text("❌ Ошибка курса, попробуй позже")
            return

        to_amount = amount * rate

        try:
            result = supabase.table('p2p_orders').insert({
                'creator_id': user.id,
                'from_token': from_token,
                'to_token': to_token,
                'from_amount': amount,
                'to_amount': to_amount,
                'rate': rate,
                'status': 'open',
            }).execute()
            order_id = result.data[0]['id']
            print(f"[obmen] created order {order_id} by {user.id}", flush=True)
        except Exception as e:
            print(f"[obmen] insert error: {e}", flush=True)
            await msg.reply_text(f"❌ Не удалось создать ордер: {e}")
            return

        try:
            await msg.delete()
        except Exception:
            pass

        sent = await context.bot.send_message(
            chat_id=msg.chat.id,
            text=f"✅ Ордер создан. Скоро появится в канале с кнопкой обмена."
        )
        asyncio.create_task(delete_later(context.bot, msg.chat.id, sent.message_id, 5))

    except Exception as e:
        print(f"[obmen] FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== /pay ====================
async def pay_command(update, context):
    """Перевод в ответ на сообщение или по @username.
       /pay 100 AI  (в ответ на сообщение)
       /pay @user 100 AI
    """
    try:
        msg = update.effective_message
        if not msg or not msg.text:
            return
        sender = msg.from_user
        parts = msg.text.strip().split()
        args = parts[1:]

        to_id = None
        if msg.reply_to_message and msg.reply_to_message.from_user:
            to_id = msg.reply_to_message.from_user.id
            to_name = msg.reply_to_message.from_user.first_name or 'получатель'
        elif args and args[0].startswith('@'):
            username = args[0][1:]
            r = supabase.table('users').select('telegram_id, first_name').ilike('username', username).limit(1).execute()
            if not r.data:
                await msg.reply_text(f"❌ @{username} не найден в базе")
                return
            to_id = int(r.data[0]['telegram_id'])
            to_name = r.data[0].get('first_name') or f'@{username}'
            args = args[1:]
        else:
            await msg.reply_text(
                "❌ Формат:\n"
                "• Ответь на сообщение и напиши <code>/pay 100 AI</code>\n"
                "• Или <code>/pay @username 100 AI</code>",
                parse_mode='HTML'
            )
            return

        if not args or len(args) < 2:
            await msg.reply_text("❌ Укажи сумму и токен: <code>/pay 100 AI</code>", parse_mode='HTML')
            return

        try:
            amount = float(args[0].replace(',', '.'))
        except (ValueError, TypeError):
            await msg.reply_text("❌ Не понял сумму")
            return
        if amount <= 0:
            await msg.reply_text("❌ Сумма должна быть больше 0")
            return

        token = args[1].upper()

        if to_id == sender.id:
            await msg.reply_text("❌ Нельзя перевести самому себе")
            return

        r = supabase.rpc('user_transfer', {
            'p_from_id': sender.id,
            'p_to_id': to_id,
            'p_token': token,
            'p_amount': amount,
        }).execute()

        data = r.data or {}
        if not data.get('ok'):
            err = data.get('error', 'unknown')
            msgs = {
                'insufficient':     f"❌ Недостаточно средств. У тебя {data.get('have',0):g} {token}",
                'self_transfer':    "❌ Нельзя себе",
                'invalid_amount':   "❌ Некорректная сумма",
                'from_banned':      "❌ Ты заблокирован",
                'to_banned':        "❌ Получатель заблокирован",
                'from_not_found':   "❌ Твой аккаунт не найден, открой /start",
                'to_not_found':     "❌ Получатель не найден в базе",
                'token_not_found':  f"❌ Токен {token} не найден",
            }
            await msg.reply_text(msgs.get(err, f"❌ Ошибка: {err}"))
            return

        await msg.reply_text(
            f"✅ Перевод выполнен\n\n"
            f"👤 Кому: <b>{to_name}</b>\n"
            f"💵 Сумма: <b>{amount:g} {token}</b>\n"
            f"💰 Твой остаток: <b>{data.get('from_new_balance',0):g} {token}</b>",
            parse_mode='HTML'
        )

        try:
            await context.bot.send_message(
                chat_id=to_id,
                text=(
                    f"💸 <b>Вам перевод!</b>\n\n"
                    f"👤 От: {sender.first_name or 'пользователь'}\n"
                    f"💵 Сумма: <b>{amount:g} {token}</b>"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[pay] notify error: {e}", flush=True)

    except Exception as e:
        print(f"[pay] FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== /rain (/flashpay, /flash) ====================
async def rain_command(update, context):
    """Раздаёт токены всем активным в чате за последние 30 минут.

    Использование:
      /rain 100 AI
      /flashpay 1 TG
      /flash 0.5 TUSD
    """
    try:
        msg = update.effective_message
        if not msg or not msg.text:
            return

        if msg.chat.type not in ('group', 'supergroup'):
            await msg.reply_text("❌ Только в чате")
            return

        parts = msg.text.strip().split()
        if len(parts) != 3:
            await msg.reply_text(
                "❌ Формат: <code>/rain 100 AI</code>\n"
                "Раздаёт всем, кто писал в чате за последние 30 минут.",
                parse_mode='HTML'
            )
            return

        try:
            total = float(parts[1].replace(',', '.'))
        except (ValueError, TypeError):
            await msg.reply_text("❌ Не понял сумму")
            return
        if total <= 0:
            await msg.reply_text("❌ Сумма должна быть больше 0")
            return

        token = parts[2].upper()
        chat_id = msg.chat.id
        sender = msg.from_user

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        except Exception:
            pass

        r = supabase.rpc('chat_rain', {
            'p_chat_telegram_id': chat_id,
            'p_from_id': sender.id,
            'p_token': token,
            'p_total_amount': total,
            'p_window_minutes': 30,
            'p_min_per_user': 0.0001,
        }).execute()

        data = r.data or {}
        if not data.get('ok'):
            err = data.get('error', 'unknown')
            if err == 'no_recipients':
                await msg.reply_text("🤷 В чате никого активного за последние 30 минут")
            elif err == 'insufficient':
                await msg.reply_text(
                    f"❌ Недостаточно {token}.\n"
                    f"У тебя: <b>{data.get('have',0):g}</b>\n"
                    f"Нужно: <b>{data.get('need',0):g}</b>",
                    parse_mode='HTML'
                )
            elif err == 'too_small':
                per = data.get('per_user', 0)
                mn = data.get('min_per_user', 0)
                cnt = data.get('recipients', 0)
                await msg.reply_text(
                    f"❌ Слишком мало на человека.\n"
                    f"👥 Активных: <b>{cnt}</b>\n"
                    f"💰 По <b>{per:g}</b> {token} — меньше минимума {mn:g}.\n"
                    f"💡 Увеличь сумму или подожди.",
                    parse_mode='HTML'
                )
            elif err == 'chat_not_active':
                await msg.reply_text("❌ Чат не зарегистрирован в боте")
            else:
                await msg.reply_text(f"❌ Ошибка: {err}")
            return

        await msg.reply_text(
            f"🌧 <b>ДОЖДЬ РАЗДАЧИ!</b>\n\n"
            f"💵 Роздано: <b>{data.get('total_sent', 0):g} {token}</b>\n"
            f"👥 Получателей: <b>{data.get('sent', 0)}</b>\n"
            f"💰 Каждому: <b>~{data.get('per_user', 0):g} {token}</b>\n"
            f"👑 От: {sender.first_name or 'щедрый друг'}",
            parse_mode='HTML'
        )

    except Exception as e:
        print(f"[rain] FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await update.effective_message.reply_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


# ==================== /help ====================
async def help_command(update, context):
    text = (
        "🤖 <b>КОМАНДЫ БОТА</b>\n\n"
        "🚀 /start — регистрация\n"
        "🔄 /obmen 100 TG на TUSD — создать P2P-ордер\n"
        "💸 /pay 100 AI — перевести (ответом на сообщение)\n"
        "💸 /pay @user 100 AI — перевести по @username\n"
        "🌧 /rain 100 AI — дождь (раздать всем активным)\n"
        "🏆 /top — топ-10 чата за сегодня\n"
        "💰 /balance — мой баланс\n"
        "❓ /help — эта справка\n\n"
        "📱 Полный функционал — в приложении"
    )
    await update.effective_message.reply_text(text, parse_mode='HTML')


# ==================== /top ====================
async def top_command(update, context):
    try:
        msg = update.effective_message
        chat_id = msg.chat.id
        r = supabase.rpc('get_chat_leaderboard_v2', {
            'p_chat_telegram_id': chat_id,
            'p_limit': 10,
        }).execute()
        rows = r.data or []
        if not rows:
            await msg.reply_text("Пока никто не писал сегодня 🤷")
            return

        lines = ["🏆 <b>ТОП-10 ЧАТА ЗА СЕГОДНЯ</b>\n"]
        medals = ['🥇', '🥈', '🥉']
        for row in rows:
            place_num = int(row.get('rank_position') or 0)
            place = medals[place_num - 1] if 1 <= place_num <= 3 else f"{place_num}."
            name = row.get('username') or '—'
            cnt = row.get('message_count') or 0
            lines.append(f"{place} <b>{name}</b> — {cnt} сообщ.")
        await msg.reply_text("\n".join(lines), parse_mode='HTML')
    except Exception as e:
        print(f"[top] error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        await msg.reply_text(f"❌ Ошибка топа: {e}")


# ==================== /balance ====================
async def balance_command(update, context):
    try:
        msg = update.effective_message
        user_id = msg.from_user.id
        r = supabase.table('users').select('tg_balance,ai_balance,tusd_balance,rank_name,rank_level').eq('telegram_id', user_id).maybe_single().execute()
        u = r.data or {}
        text = (
            f"💰 <b>ТВОЙ БАЛАНС</b>\n\n"
            f"🥇 TG: <b>{float(u.get('tg_balance') or 0):g}</b>\n"
            f"🤖 AI: <b>{float(u.get('ai_balance') or 0):g}</b>\n"
            f"💵 TUSD: <b>{float(u.get('tusd_balance') or 0):g}</b>\n\n"
            f"👑 Титул: <b>{u.get('rank_name') or '—'}</b> (ур. {u.get('rank_level') or 1})"
        )
        await msg.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"[balance] error: {e}", flush=True)
        await msg.reply_text("❌ Ошибка, попробуй позже")


# ==================== КНОПКА «⚡ ОБМЕНЯТЬ СРАЗУ» ====================
async def take_order_callback(update, context):
    """
    Нажатие кнопки 'ОБМЕНЯТЬ СРАЗУ' под постом в канале.
    Делает обмен напрямую, без открытия Mini App.
    """
    try:
        query = update.callback_query
        if not query or not query.data:
            return

        print(f"[take] CALLBACK RECEIVED: {query.data} from user {query.from_user.id}", flush=True)

        try:
            await query.answer()
        except Exception as e:
            print(f"[take] answer error (не критично): {e}", flush=True)

        order_id = query.data.replace('take_', '')
        taker = query.from_user
        taker_id = taker.id
        taker_name = taker.first_name or taker.username or 'Участник'

        print(f"[take] calling RPC accept_p2p_order_chat(order={order_id}, taker={taker_id})", flush=True)

        r = None
        try:
            r = supabase.rpc('accept_p2p_order_chat', {
                'p_order_id': str(order_id),
                'p_taker_id': str(taker_id),
            }).execute()
            print(f"[take] RPC RESPONSE: {r.data}", flush=True)
        except Exception as e:
            print(f"[take] RPC RAISED: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            try:
                await query.answer("❌ Ошибка соединения, попробуй позже", show_alert=True)
            except Exception:
                pass
            return

        data = (r.data if r else None) or {}
        if not data.get('ok'):
            err = data.get('error', 'unknown')
            print(f"[take] RPC returned not ok: {err}", flush=True)
            msgs = {
                'not_found':            '❌ Ордер не найден',
                'already_taken':        '❌ Ордер уже принят кем-то',
                'own_order':            '❌ Нельзя принять свой ордер',
                'creator_insufficient': '❌ У продавца недостаточно токенов',
                'taker_insufficient':   f"❌ У вас недостаточно {data.get('need_token','')}. Нужно: {data.get('need',0)}",
                'banned':               '❌ Один из пользователей заблокирован',
            }
            try:
                await query.answer(msgs.get(err, f"❌ Ошибка: {err}"), show_alert=True)
            except Exception:
                pass
            return

        from_amt = float(data['from_amount'])
        to_amt   = float(data['to_amount'])
        from_tok = data['from_token']
        to_tok   = data['to_token']
        creator_name = data.get('creator_name') or 'Продавец'

        print(f"[take] SUCCESS: {from_amt} {from_tok} <-> {to_amt} {to_tok}", flush=True)

        new_text = (
            f"✅ <b>СДЕЛКА СОВЕРШЕНА</b>\n\n"
            f"👤 <b>{creator_name}</b> → <b>{taker_name}</b>\n"
            f"💱 {from_amt:g} {from_tok} ⇄ {to_amt:g} {to_tok}\n"
            f"⏱ {data.get('now_time','')}"
        )

        try:
            await query.edit_message_text(text=new_text, parse_mode='HTML')
            print(f"[take] message edited successfully", flush=True)
        except Exception as e:
            print(f"[take] edit_message error: {type(e).__name__}: {e}", flush=True)

        creator_id = data.get('creator_id')
        for uid, txt in [
            (creator_id, f"✅ Твой ордер принят!\n+{to_amt:g} {to_tok} зачислено на кошелёк"),
            (taker_id,  f"✅ Обмен выполнен!\n+{from_amt:g} {from_tok} зачислено на кошелёк"),
        ]:
            if uid:
                try:
                    await context.bot.send_message(chat_id=int(uid), text=txt)
                    print(f"[take] notified user {uid}", flush=True)
                except Exception as e:
                    print(f"[take] notify {uid} error: {type(e).__name__}: {e}", flush=True)

    except Exception as e:
        print(f"[take] FATAL: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== СООБЩЕНИЯ В ГРУППАХ ====================
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
        username = msg.from_user.username
        first_name = msg.from_user.first_name or ''
        last_name  = msg.from_user.last_name or ''
        full_name  = (first_name + ' ' + last_name).strip() or None

        # Синхронизируем имя в users, чтобы в топе не было «anon»
        try:
            supabase.rpc('ensure_user_exists', {
                'p_telegram_id': user_id,
                'p_first_name': full_name,
                'p_username': username,
            }).execute()
        except Exception as e:
            print(f"[handle_message] ensure_user_exists error: {e}", flush=True)

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


# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================
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


# ==================== FLASK ====================
@app.route('/webhook', methods=['POST'])
def webhook():
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
    telegram_app.add_handler(CommandHandler('start',    start_command))
    telegram_app.add_handler(CommandHandler('obmen',    obmen_command))
    telegram_app.add_handler(CommandHandler('pay',      pay_command))
    telegram_app.add_handler(CommandHandler('help',     help_command))
    telegram_app.add_handler(CommandHandler('top',      top_command))
    telegram_app.add_handler(CommandHandler('balance',  balance_command))
    telegram_app.add_handler(CommandHandler('rain',     rain_command))
    telegram_app.add_handler(CommandHandler('flashpay', rain_command))
    telegram_app.add_handler(CommandHandler('flash',    rain_command))
    telegram_app.add_handler(CallbackQueryHandler(take_order_callback, pattern=r'^take_'))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()

    await telegram_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
    )
    print(f"Webhook установлен: {WEBHOOK_URL}", flush=True)
    print("Bot started (webhook mode)...", flush=True)

    await asyncio.Event().wait()


if __name__ == '__main__':
    print("STARTING PUBLISHER...", flush=True)
    start_publisher()

    print("STARTING FLASK THREAD...", flush=True)
    threading.Thread(target=run_flask, daemon=True).start()

    print("STARTING BOT WEBHOOK...", flush=True)
    try:
        asyncio.run(bot_main())
    except Exception as e:
        print(f"BOT FATAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
