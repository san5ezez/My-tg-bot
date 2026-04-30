import asyncio
import socket
import struct
import time
import json
import re
import base64
import io
import datetime
import os
import pickle

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8466551129:AAFZ_k0eOzRjRhdPgeiN5j0PT9Z25zAtQME"          # токен от @BotFather
MC_HOST    = "185.9.145.215"
MC_PORT    = 25573
PROXY      = None                        # например "socks5://65.109.218.115:1080"
DATA_FILE  = "bot_data.pkl"             # файл сохранения
# ──────────────────────────────────────────────────────────────────────────────

# ─── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────────
server_history    = []    # последние 60 проверок
alert_chats       = set()
last_server_state = None
chat_log          = []    # [{time, user, user_id, text}]

# Счётчики игроков: {period: {name: count}}
# period = "day" | "week" | "month" | "all"
player_counter = {"day": {}, "week": {}, "month": {}, "all": {}}

# Дата последнего сброса
last_reset = {"day": None, "week": None, "month": None}
# ──────────────────────────────────────────────────────────────────────────────


# ─── СОХРАНЕНИЕ / ЗАГРУЗКА ───────────────────────────────────────────────────

def save_data():
    data = {
        "player_counter": player_counter,
        "alert_chats":    list(alert_chats),
        "chat_log":       chat_log[-500:],   # максимум 500 сообщений
        "last_reset":     last_reset,
        "server_history": server_history,
    }
    with open(DATA_FILE, "wb") as f:
        pickle.dump(data, f)


def load_data():
    global player_counter, alert_chats, chat_log, last_reset, server_history
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "rb") as f:
            data = pickle.load(f)
        player_counter = data.get("player_counter", {"day": {}, "week": {}, "month": {}, "all": {}})
        alert_chats    = set(data.get("alert_chats", []))
        chat_log       = data.get("chat_log", [])
        last_reset     = data.get("last_reset", {"day": None, "week": None, "month": None})
        server_history = data.get("server_history", [])
        print(f"✅ Данные загружены: {len(chat_log)} сообщений, {len(player_counter['all'])} игроков")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить данные: {e}")


def reset_periods_if_needed():
    now  = datetime.datetime.now()
    today = now.date()

    # День
    if last_reset["day"] != today:
        player_counter["day"] = {}
        last_reset["day"] = today

    # Неделя (сброс в понедельник)
    week_start = today - datetime.timedelta(days=today.weekday())
    if last_reset["week"] != week_start:
        player_counter["week"] = {}
        last_reset["week"] = week_start

    # Месяц
    month_start = today.replace(day=1)
    if last_reset["month"] != month_start:
        player_counter["month"] = {}
        last_reset["month"] = month_start


# ─── MINECRAFT PING ───────────────────────────────────────────────────────────

def ping_minecraft(host: str, port: int, timeout: int = 5) -> dict:
    try:
        sock = socket.create_connection((host, port), timeout=timeout)

        def pack_varint(val):
            data = b""
            while True:
                b = val & 0x7F
                val >>= 7
                if val:
                    b |= 0x80
                data += bytes([b])
                if not val:
                    break
            return data

        def read_varint(s):
            num, shift = 0, 0
            while True:
                b = s.recv(1)
                if not b:
                    raise EOFError("Connection closed")
                b = b[0]
                num |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            return num

        host_encoded = host.encode("utf-8")
        handshake = (
            pack_varint(0x00)
            + pack_varint(47)
            + pack_varint(len(host_encoded))
            + host_encoded
            + struct.pack(">H", port)
            + pack_varint(1)
        )
        handshake = pack_varint(len(handshake)) + handshake
        sock.sendall(handshake)
        sock.sendall(b"\x01\x00")

        _length    = read_varint(sock)
        _packet_id = read_varint(sock)
        str_len    = read_varint(sock)

        raw = b""
        while len(raw) < str_len:
            chunk = sock.recv(str_len - len(raw))
            if not chunk:
                break
            raw += chunk

        ping_payload = struct.pack(">q", int(time.time() * 1000))
        ping_packet  = pack_varint(len(ping_payload) + 1) + b"\x01" + ping_payload
        t1 = time.time()
        sock.sendall(ping_packet)
        try:
            sock.recv(10)
        except Exception:
            pass
        ping_ms = round((time.time() - t1) * 1000)
        sock.close()

        data           = json.loads(raw.decode("utf-8"))
        players_online = data.get("players", {}).get("online", 0)
        players_max    = data.get("players", {}).get("max", 0)
        player_list    = [p.get("name", "?") for p in data.get("players", {}).get("sample", [])]
        description    = data.get("description", {})
        motd = description.get("text", "") if isinstance(description, dict) else str(description)
        motd = re.sub(r"§.", "", motd).strip()
        version = data.get("version", {}).get("name", "?")
        favicon = data.get("favicon", "")

        return {
            "online": True,
            "players": players_online,
            "max_players": players_max,
            "player_list": player_list,
            "motd": motd,
            "version": version,
            "ping_ms": ping_ms,
            "favicon": favicon,
        }

    except Exception as e:
        return {"online": False, "error": str(e)}


# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────────

def build_status_text(s: dict) -> str:
    if not s["online"]:
        return f"🔴 *Сервер недоступен*\n```\n{s.get('error', 'Нет ответа')}\n```"
    ping_emoji   = "🟢" if s["ping_ms"] < 80 else "🟡" if s["ping_ms"] < 200 else "🔴"
    players_line = f"{s['players']}/{s['max_players']}"
    if s["player_list"]:
        names         = "\n".join(f"  • {n}" for n in s["player_list"])
        players_block = f"\n\n👥 *Онлайн сейчас:*\n{names}"
    else:
        players_block = "\n\n_Список игроков скрыт сервером_"
    return (
        f"🟢 *Сервер онлайн*\n\n"
        f"📋 *MOTD:* {s['motd'] or '—'}\n"
        f"🎮 *Версия:* `{s['version']}`\n"
        f"👤 *Игроков:* `{players_line}`\n"
        f"{ping_emoji} *Пинг:* `{s['ping_ms']} мс`"
        f"{players_block}"
    )


def refresh_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh")
    ]])


def top_period_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("За день",    callback_data="top_day"),
        InlineKeyboardButton("За неделю",  callback_data="top_week"),
    ], [
        InlineKeyboardButton("За месяц",   callback_data="top_month"),
        InlineKeyboardButton("За всё время", callback_data="top_all"),
    ]])


def build_top_text(period: str) -> str:
    labels = {"day": "день", "week": "неделю", "month": "месяц", "all": "всё время"}
    counter = player_counter.get(period, {})
    if not counter:
        return f"📊 *Топ за {labels[period]}*\n\n_Пока нет данных_"
    top   = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = "\n".join(f"  {i+1}. `{name}` — {cnt} раз" for i, (name, cnt) in enumerate(top))
    return f"📊 *Топ игроков за {labels[period]}:*\n\n{lines}"


CHAT_PAGE_SIZE = 10

def build_chat_log_text(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total    = len(chat_log)
    if total == 0:
        return "💬 *Лог чата пуст*", InlineKeyboardMarkup([])

    total_pages = max(1, (total + CHAT_PAGE_SIZE - 1) // CHAT_PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))

    # Показываем от новых к старым
    reversed_log = list(reversed(chat_log))
    start = page * CHAT_PAGE_SIZE
    chunk = reversed_log[start:start + CHAT_PAGE_SIZE]

    lines = []
    for entry in chunk:
        lines.append(f"🕐 `{entry['time']}` *{entry['user']}*: {entry['text']}")

    text = f"💬 *Лог чата* (стр. {page+1}/{total_pages}, всего {total}):\n\n" + "\n".join(lines)

    nav = []
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("⬅️ Старее", callback_data=f"chatlog_{page+1}"))
    if page > 0:
        nav.append(InlineKeyboardButton("Новее ➡️", callback_data=f"chatlog_{page-1}"))
    keyboard = InlineKeyboardMarkup([nav]) if nav else InlineKeyboardMarkup([])

    return text, keyboard


# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⛏ *Minecraft Server Monitor*\n\n"
        f"Слежу за сервером `{MC_HOST}:{MC_PORT}`\n\n"
        "Команды:\n"
        "/status — статус сервера\n"
        "/players — кто онлайн\n"
        "/ping — пинг до сервера\n"
        "/icon — иконка сервера\n"
        "/top — топ игроков (день/неделя/месяц/всё время)\n"
        "/history — последние проверки\n"
        "/alert — вкл/выкл уведомления\n"
        "/chat\\_log — лог чата бота",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Проверяю сервер...")
    s   = ping_minecraft(MC_HOST, MC_PORT)
    await msg.edit_text(build_status_text(s), parse_mode="Markdown", reply_markup=refresh_keyboard())


async def cmd_players(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Получаю список игроков...")
    s   = ping_minecraft(MC_HOST, MC_PORT)
    if not s["online"]:
        await msg.edit_text("🔴 Сервер недоступен", reply_markup=refresh_keyboard())
        return
    if s["player_list"]:
        names = "\n".join(f"  • {n}" for n in s["player_list"])
        text  = f"👥 *Игроков онлайн: {s['players']}/{s['max_players']}*\n\n{names}"
    else:
        text = f"👤 *Онлайн: {s['players']}/{s['max_players']}*\n_Список скрыт сервером_"
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=refresh_keyboard())


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📡 Пингую...")
    s   = ping_minecraft(MC_HOST, MC_PORT)
    if not s["online"]:
        await msg.edit_text("🔴 Сервер не отвечает")
        return
    p     = s["ping_ms"]
    emoji = "🟢" if p < 80 else "🟡" if p < 200 else "🔴"
    await msg.edit_text(
        f"{emoji} *Пинг:* `{p} мс`\n`{MC_HOST}:{MC_PORT}`",
        parse_mode="Markdown", reply_markup=refresh_keyboard(),
    )


async def cmd_icon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🖼 Получаю иконку...")
    s   = ping_minecraft(MC_HOST, MC_PORT)
    if not s["online"]:
        await msg.delete()
        await update.message.reply_text("🔴 Сервер недоступен")
        return
    favicon = s.get("favicon", "")
    if not favicon or "," not in favicon:
        await msg.delete()
        await update.message.reply_text("❌ Сервер не вернул иконку")
        return
    img_data = base64.b64decode(favicon.split(",")[1])
    await msg.delete()
    await update.message.reply_photo(
        photo=io.BytesIO(img_data),
        caption=f"🖼 Иконка `{MC_HOST}:{MC_PORT}`",
        parse_mode="Markdown",
    )


async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in alert_chats:
        alert_chats.discard(chat_id)
        await update.message.reply_text("🔕 Алерты выключены")
    else:
        alert_chats.add(chat_id)
        await update.message.reply_text(
            "🔔 *Алерты включены!*\nБуду уведомлять когда сервер падает или поднимается.",
            parse_mode="Markdown",
        )
    save_data()


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_top_text("all"),
        parse_mode="Markdown",
        reply_markup=top_period_keyboard(),
    )


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not server_history:
        await update.message.reply_text("📋 Пока нет данных — первая проверка через минуту.")
        return
    lines = "\n".join(
        f"  {h['time']} — {'🟢' if h['online'] else '🔴'} {h['players']} игр."
        for h in server_history[-10:]
    )
    await update.message.reply_text(f"📋 *Последние проверки:*\n\n{lines}", parse_mode="Markdown")


async def cmd_chat_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, keyboard = build_chat_log_text(0)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ─── СОХРАНЕНИЕ СООБЩЕНИЙ ─────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    user = msg.from_user
    name = user.username and f"@{user.username}" or user.first_name or "Аноним"
    entry = {
        "time":    datetime.datetime.now().strftime("%d.%m %H:%M"),
        "user":    name,
        "user_id": user.id,
        "text":    msg.text[:200],
    }
    chat_log.append(entry)
    if len(chat_log) > 500:
        chat_log.pop(0)
    # Сохраняем каждые 10 сообщений
    if len(chat_log) % 10 == 0:
        save_data()


# ─── CALLBACKS ────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "refresh":
        s = ping_minecraft(MC_HOST, MC_PORT)
        try:
            await query.edit_message_text(
                build_status_text(s),
                parse_mode="Markdown",
                reply_markup=refresh_keyboard(),
            )
        except Exception:
            pass

    elif data.startswith("top_"):
        period = data[4:]  # day / week / month / all
        try:
            await query.edit_message_text(
                build_top_text(period),
                parse_mode="Markdown",
                reply_markup=top_period_keyboard(),
            )
        except Exception:
            pass

    elif data.startswith("chatlog_"):
        page = int(data[8:])
        text, keyboard = build_chat_log_text(page)
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass


# ─── ФОНОВАЯ ПРОВЕРКА ────────────────────────────────────────────────────────

async def background_check(ctx: ContextTypes.DEFAULT_TYPE):
    global last_server_state
    reset_periods_if_needed()

    s   = ping_minecraft(MC_HOST, MC_PORT)
    now = datetime.datetime.now().strftime("%H:%M:%S")

    server_history.append({
        "time":    now,
        "online":  s["online"],
        "players": s.get("players", 0),
    })
    if len(server_history) > 60:
        server_history.pop(0)

    for name in s.get("player_list", []):
        for period in ("day", "week", "month", "all"):
            player_counter[period][name] = player_counter[period].get(name, 0) + 1

    if last_server_state is not None and last_server_state != s["online"]:
        if s["online"]:
            text = (
                f"🟢 *Сервер снова онлайн!*\n"
                f"👤 Игроков: {s['players']}/{s['max_players']}\n"
                f"🏓 Пинг: {s['ping_ms']} мс"
            )
        else:
            text = "🔴 *Сервер упал!*"
        for chat_id in list(alert_chats):
            try:
                await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception:
                pass

    last_server_state = s["online"]
    save_data()
    print(f"[{now}] {'🟢 онлайн' if s['online'] else '🔴 офлайн'} | игроков: {s.get('players', 0)}")


# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

async def main():
    load_data()

    builder = ApplicationBuilder().token(BOT_TOKEN)
    if PROXY:
        builder = builder.proxy(PROXY).get_updates_proxy(PROXY)
    app = builder.build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("players",  cmd_players))
    app.add_handler(CommandHandler("ping",     cmd_ping))
    app.add_handler(CommandHandler("info",     cmd_status))
    app.add_handler(CommandHandler("icon",     cmd_icon))
    app.add_handler(CommandHandler("alert",    cmd_alert))
    app.add_handler(CommandHandler("top",      cmd_top))
    app.add_handler(CommandHandler("history",  cmd_history))
    app.add_handler(CommandHandler("chat_log", cmd_chat_log))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(background_check, interval=60, first=10)

    print(f"🚀 Бот запущен | Сервер: {MC_HOST}:{MC_PORT}")
    async with app:
        await app.start()
        await app.updater.start_polling()
        print("Нажми Ctrl+C для остановки")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

