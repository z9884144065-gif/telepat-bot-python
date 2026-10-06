"""
order_publisher.py
Постит новые P2P-ордера в Telegram-канал tgbanks.
Работает в отдельном потоке, не мешает основному боту.
"""
import os
import time
import threading
import html
import requests
from datetime import datetime, timezone
from supabase import create_client

# ============================================
# НАСТРОЙКИ
# ============================================
CHANNEL_ID = -1001960985208   # канал tgbanks
POLL_INTERVAL = 10            # проверять новые ордера каждые 10 сек
ONLY_STATUS = 'open'          # постим только живые ордера
BOT_USERNAME = os.environ.get('BOT_USERNAME', 'TELEPATp2p_bot')  # username бота для deep-link
# ============================================

BOT_TOKEN = os.environ.get('BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
SUPABASE_URL = 'https://jutszxuzjzyfxarceydw.supabase.co'
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')


def esc(x):
    """Безопасно экранирует строку для HTML-разметки Telegram."""
    return html.escape(str(x)) if x is not None else '?'


def send_to_channel(order):
    """Отправляет один ордер в канал."""
    if not BOT_TOKEN:
        print("[PUBLISHER] Нет BOT_TOKEN", flush=True)
        return False
    if not CHANNEL_ID:
        print("[PUBLISHER] Не задан CHANNEL_ID", flush=True)
        return False

    try:
        from_token = esc(order.get('from_token', '?'))
        to_token = esc(order.get('to_token', '?'))
        from_amount = float(order.get('from_amount') or 0)
        to_amount = float(order.get('to_amount') or 0)
        rate = float(order.get('rate') or 0)
        creator_id = esc(order.get('creator_id', '?'))
        creator_rank = esc(order.get('creator_rank_name') or 'Житель')
        order_id = order.get('id')
    except Exception as e:
        print(f"[PUBLISHER] ❌ Ошибка разбора ордера {order.get('id')}: {e}", flush=True)
        return False

    text = (
        f"🆕 <b>НОВЫЙ P2P-ОРДЕР</b>\n\n"
        f"💰 <b>Продам:</b> {from_amount:g} {from_token}\n"
        f"💵 <b>Хочу получить:</b> {to_amount:g} {to_token}\n\n"
        f"📊 <b>Курс:</b> 1 {from_token} = {rate:g} {to_token}\n"
        f"👑 <b>Создатель:</b> {creator_rank} (ID {creator_id})\n"
    )

    # Две URL-кнопки — Telegram открывает Mini App сам, бот не участвует.
    # order_<id> → открыть карточку ордера
    # quick_<id> → открыть карточку + модалку быстрого выкупа
    reply_markup = {
        "inline_keyboard": [[
            {
                "text": "🛒 ОТКРЫТЬ",
                "url": f"https://t.me/{BOT_USERNAME}?startapp=order_{order_id}"
            },
            {
                "text": "⚡ БЫСТР.ОБМЕН",
                "url": f"https://t.me/{BOT_USERNAME}?startapp=quick_{order_id}"
            }
        ]]
    }

    # До 3 попыток: на 429 (rate limit) — ждём и повторяем
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": CHANNEL_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": reply_markup,
                },
                timeout=10,
            )

            if r.status_code == 200:
                print(f"[PUBLISHER] ✅ Ордер {order_id} отправлен", flush=True)
                return True

            if r.status_code == 429:
                try:
                    retry_after = r.json().get('parameters', {}).get('retry_after', 3)
                except Exception:
                    retry_after = 3
                print(f"[PUBLISHER] ⏳ 429 rate limit, ждём {retry_after}с", flush=True)
                time.sleep(retry_after + 1)
                continue

            print(f"[PUBLISHER] ❌ {r.status_code}: {r.text[:300]}", flush=True)
            if r.status_code == 400 and "chat not found" in r.text.lower():
                print("[PUBLISHER] Проверь, что бот добавлен в канал как админ", flush=True)
            return False

        except Exception as e:
            print(f"[PUBLISHER] ❌ Exception (попытка {attempt+1}): {e}", flush=True)
            time.sleep(2)

    return False


def poll_new_orders():
    """Бесконечный цикл: проверяет новые ордера и постит в канал."""
    if not SUPABASE_KEY:
        print("[PUBLISHER] ❌ SUPABASE_KEY не задан", flush=True)
        return

    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[PUBLISHER] ❌ supabase клиент: {e}", flush=True)
        return

    last_created = datetime.now(timezone.utc).isoformat()
    print(f"[PUBLISHER] Старт. Слежу за ордерами после {last_created}", flush=True)

    while True:
        try:
            q = (
                supabase.table('p2p_orders')
                .select('*')
                .eq('status', ONLY_STATUS)
                .gt('created_at', last_created)
                .order('created_at')
                .limit(20)
            )
            result = q.execute()

            for order in result.data:
                send_to_channel(order)
                last_created = order['created_at']
                time.sleep(1.5)

        except Exception as e:
            print(f"[PUBLISHER] Ошибка в цикле: {e}", flush=True)

        time.sleep(POLL_INTERVAL)


def start_publisher():
    """Запускает publisher в отдельном потоке."""
    t = threading.Thread(target=poll_new_orders, daemon=True)
    t.start()
    return t
