# -*- coding: utf-8 -*-
import asyncio
import html
import os
import re
import secrets
import uuid
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from yookassa import Configuration, Payment

load_dotenv()

logging.basicConfig(
    filename="bot_errors.log",
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_FILE = os.getenv("DB_FILE", "/app/data/new_bot.sqlite3")
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "3000"))
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/").strip()

BANNER_CANDIDATES = ["banner.png", "banner.jpg", "banner.jpeg", "/app/banner.png", "/app/banner.jpg", "/app/data/banner.png", "/app/data/banner.jpg"]
MSK_TZ = timezone(timedelta(hours=3))

if not BOT_TOKEN:
    raise RuntimeError("В переменных не указан BOT_TOKEN")
if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    raise RuntimeError("В переменных не указаны YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
BOT_USERNAME = ""

PLANS = {
    "1d": {"title": "1 день", "days": 1, "rubles": 99},
    "7d": {"title": "7 дней", "days": 7, "rubles": 299},
    "30d": {"title": "30 дней", "days": 30, "rubles": 799},
}

class UserStates(StatesGroup):
    waiting_question = State()
    waiting_broadcast = State()
    waiting_email = State()
    waiting_sub_target = State()
    waiting_sub_action = State()
    waiting_spy_id = State()
    waiting_nuke_id = State()

def format_msk(dt_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
    except Exception: return "активно"

def find_existing_banner() -> str | None:
    for path in BANNER_CANDIDATES:
        if os.path.isfile(path): return path
    return None

def utc_day_start():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

async def db_execute(sql, params=(), fetchone=False, fetchall=False, commit=True):
    db_dir = os.path.dirname(DB_FILE)
    if db_dir: os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        result = await cursor.fetchone() if fetchone else (await cursor.fetchall() if fetchall else cursor.rowcount)
        if commit: await db.commit()
        return result

async def init_db():
    await db_execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, paid_until TEXT, created_at TEXT NOT NULL)")
    await db_execute("CREATE TABLE IF NOT EXISTS personal_links (code TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL)")
    await db_execute("CREATE TABLE IF NOT EXISTS link_visits (id INTEGER PRIMARY KEY AUTOINCREMENT, receiver_id INTEGER NOT NULL, visitor_id INTEGER NOT NULL, created_at TEXT NOT NULL)")
    await db_execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL, text TEXT, telegram_message_id INTEGER NOT NULL, created_at TEXT NOT NULL, replied INTEGER DEFAULT 0, reported INTEGER DEFAULT 0, recipient_message_id INTEGER)")
    await db_execute("CREATE TABLE IF NOT EXISTS blocks (receiver_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, PRIMARY KEY(receiver_id, sender_id))")
    await db_execute("CREATE TABLE IF NOT EXISTS payments (telegram_charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")

async def save_user(user):
    now = datetime.now(timezone.utc).isoformat()
    await db_execute("INSERT INTO users(user_id, username, first_name, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name", (user.id, user.username or "", user.first_name or "", now))

async def get_or_create_link_code(user_id: int) -> str:
    row = await db_execute("SELECT code FROM personal_links WHERE user_id=?", (user_id,), fetchone=True, commit=False)
    if row: return row["code"]
    while True:
        code = secrets.token_urlsafe(8)
        if not await db_execute("SELECT 1 FROM personal_links WHERE code=?", (code,), fetchone=True, commit=False): break
    await db_execute("INSERT INTO personal_links(code, user_id, created_at) VALUES (?, ?, ?)", (code, user_id, datetime.now(timezone.utc).isoformat()))
    return code

async def user_id_by_code(code: str):
    row = await db_execute("SELECT user_id FROM personal_links WHERE code=?", (code,), fetchone=True, commit=False)
    return row["user_id"] if row else None

async def get_message(message_id):
    return await db_execute("SELECT * FROM messages WHERE id=?", (message_id,), fetchone=True, commit=False)

async def is_blocked(receiver_id, sender_id):
    return (await db_execute("SELECT 1 FROM blocks WHERE receiver_id=? AND sender_id=?", (receiver_id, sender_id), fetchone=True, commit=False)) is not None

async def user_is_paid(user_id: int) -> bool:
    row = await db_execute("SELECT paid_until FROM users WHERE user_id=?", (user_id,), fetchone=True, commit=False)
    if not row or not row["paid_until"]: return False
    try:
        dt = datetime.fromisoformat(row["paid_until"])
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception: return False

async def get_my_blocked_users(user_id):
    return await db_execute("SELECT u.user_id, u.username, u.first_name FROM blocks b LEFT JOIN users u ON u.user_id=b.sender_id WHERE b.receiver_id=? ORDER BY b.sender_id DESC", (user_id,), fetchall=True, commit=False)

async def safe_edit_text(call: CallbackQuery, text: str, reply_markup=None):
    try: await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): await call.message.answer(text, reply_markup=reply_markup)
    except Exception: await call.message.answer(text, reply_markup=reply_markup)

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link"), InlineKeyboardButton(text="📊 Статистика", callback_data="profile_stats")],
        [InlineKeyboardButton(text="💎 Премиум", callback_data="prices"), InlineKeyboardButton(text="🚫 Блок-лист", callback_data="my_blocked")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])

def link_keyboard(link: str):
    share_url = f"https://t.me/share/url?url={link}&text=%D0%9D%D0%B0%D0%BF%D0%B8%D1%88%D0%B8%20%D0%BC%D0%BD%D0%B5%20%D1%87%D1%82%D0%BE-%D0%BD%D0%B8%D0%B1%D1%83%D0%B4%D1%8C%20%D0%B0%D0%BD%D0%BE%D0%BD%D0%B8%D0%BC%D0%BD%D0%BE%20%F0%9F%A4%AB"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться в Telegram", url=share_url)],
        [InlineKeyboardButton(text="📱 Открыть меню", callback_data="open_menu")]
    ])

def message_keyboard(message_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Узнать автора", callback_data=f"who:{message_id}")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block:{message_id}")],
    ])

def blocked_user_keyboard(sender_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"unblock:{sender_id}")]])

def prices_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 1 ДЕНЬ — 99 ₽", callback_data="buy:1d")],
        [InlineKeyboardButton(text="🗓 7 ДНЕЙ — 299 ₽", callback_data="buy:7d")],
        [InlineKeyboardButton(text="👑 30 ДНЕЙ — 799 ₽", callback_data="buy:30d")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back")]])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"), InlineKeyboardButton(text="📈 Аналитика", callback_data="admin_chart")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"), InlineKeyboardButton(text="🎯 Снайперы", callback_data="admin_snipers")],
        [InlineKeyboardButton(text="💰 Топ донатеров", callback_data="admin_top_donors"), InlineKeyboardButton(text="💎 Подписки", callback_data="admin_subs")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"), InlineKeyboardButton(text="📥 Скачать БД", callback_data="admin_backup")],
    ])

def admin_subs_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="admin_give")],
        [InlineKeyboardButton(text="🚫 Отозвать подписку", callback_data="admin_revoke")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")],
    ])

def admin_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]])

def build_link_text(username: str, code: str) -> str:
    link = f"https://t.me/{username}?start={code}"
    return f"Начни получать анонимные вопросы прямо сейчас 👀\n\n🔗 <b>Ссылка для получения анонимных вопросов:</b>\n<code>{link}</code>\n\nРазмести эту ссылку 👇 в описании профиля 📱 / 🎵 / 📸, чтобы тебе могли написать."

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if message.chat.type != "private": return
    await save_user(message.from_user)
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) == 2 else ""

    if payload:
        receiver_id = await user_id_by_code(payload)
        if receiver_id is None: return await message.answer("❌ Некорректная или устаревшая ссылка.")
        if receiver_id == message.from_user.id: return await message.answer("🙂 Это твоя собственная ссылка!")
        if await is_blocked(receiver_id, message.from_user.id): return await message.answer("🚫 Этот пользователь ограничил получение сообщений от тебя.")
        await db_execute("INSERT INTO link_visits(receiver_id, visitor_id, created_at) VALUES (?, ?, ?)", (receiver_id, message.from_user.id, datetime.now(timezone.utc).isoformat()))
        await state.set_state(UserStates.waiting_question)
        await state.update_data(receiver_id=receiver_id)
        return await message.answer("✍️ <b>Напиши анонимное сообщение:</b>\n\nМожно отправить текст, фото, видео или голосовое.\n🔒 <i>Получатель не узнает твой профиль.</i>")

    code = await get_or_create_link_code(message.from_user.id)
    text = build_link_text(BOT_USERNAME, code)
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await message.answer(text, reply_markup=link_keyboard(link))
    await message.answer("📱 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыбери, что тебя интересует:", reply_markup=main_keyboard())

@dp.callback_query(F.data == "open_menu")
async def open_menu(call: CallbackQuery):
    try: await call.message.delete()
    except Exception: pass
    await call.message.answer("📱 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыбери, что тебя интересует:", reply_markup=main_keyboard())
    await call.answer()

@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery):
    text = "📱 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыбери, что тебя интересует:"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=main_keyboard())
    else: await safe_edit_text(call, text, reply_markup=main_keyboard())
    await call.answer()

@dp.callback_query(F.data == "my_link")
async def my_link(call: CallbackQuery):
    code = await get_or_create_link_code(call.from_user.id)
    text = build_link_text(BOT_USERNAME, code)
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=link_keyboard(link))
    else: await safe_edit_text(call, text, reply_markup=link_keyboard(link))
    await call.answer()

@dp.message(UserStates.waiting_question)
async def receive_question(message: Message, state: FSMContext):
    data = await state.get_data()
    receiver_id = data.get("receiver_id")
    sender_id = message.from_user.id
    if not receiver_id:
        await state.clear()
        return await message.answer("⚠️ Ссылка устарела. Открой её заново.")
    if await is_blocked(receiver_id, sender_id):
        await state.clear()
        return await message.answer("🚫 Этот пользователь заблокировал приём сообщений от тебя.")
    count = await db_execute("SELECT COUNT(*) AS count FROM messages WHERE sender_id=? AND receiver_id=? AND created_at>?", (sender_id, receiver_id, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()), fetchone=True, commit=False)
    if count and count["count"] >= RATE_LIMIT:
        await state.clear()
        return await message.answer("⚠️ Превышен лимит сообщений в час.")
    text = message.text or message.caption or ""
    now = datetime.now(timezone.utc).isoformat()
    row = await db_execute("INSERT INTO messages (sender_id, receiver_id, text, telegram_message_id, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id", (sender_id, receiver_id, text, message.message_id, now), fetchone=True)
    message_id = row["id"]
    await state.clear()
    await message.answer("✅ <b>Сообщение успешно отправлено анонимно!</b>")
    try:
        header = "💌 <b>У тебя новое анонимное сообщение!</b>\n\n"
        footer = "\n\n↩️ <i>Сделай свайп (ответ) на это сообщение, чтобы ответить анонимно.</i>"
        if message.content_type == "text":
            body = f"<blockquote>{html.escape(message.text or '')}</blockquote>"
            sent = await bot.send_message(receiver_id, header + body + footer, reply_markup=message_keyboard(message_id))
        else:
            caption_text = message.caption or ""
            body = f"<blockquote>{html.escape(caption_text)}</blockquote>" if caption_text else ""
            sent = await bot.copy_message(chat_id=receiver_id, from_chat_id=message.chat.id, message_id=message.message_id, caption=header + body + footer, parse_mode=ParseMode.HTML, reply_markup=message_keyboard(message_id))
        await db_execute("UPDATE messages SET recipient_message_id=? WHERE id=?", (sent.message_id, message_id))
    except Exception:
        logging.error("Ошибка доставки", exc_info=True)
        await message.answer("⚠️ Не удалось доставить сообщение.")

@dp.message(F.reply_to_message)
async def swipe_reply(message: Message):
    if message.chat.type != "private": return
    row = await db_execute("SELECT * FROM messages WHERE receiver_id=? AND recipient_message_id=?", (message.from_user.id, message.reply_to_message.message_id), fetchone=True, commit=False)
    if not row: return
    try:
        await bot.send_message(row["sender_id"], "💬 <b>Тебе ответили на твой анонимный вопрос:</b>")
        await bot.copy_message(row["sender_id"], message.chat.id, message.message_id)
        await db_execute("UPDATE messages SET replied=1 WHERE id=?", (row["id"],))
        await message.answer("✅ Твой ответ отправлен автору!")
    except Exception: await message.answer("⚠️ Не удалось отправить ответ автору.")

async def build_profile_stats(user_id: int) -> str:
    day_start = utc_day_start()
    tm = await db_execute("SELECT COUNT(*) AS n FROM messages WHERE receiver_id=? AND created_at>=?", (user_id, day_start), fetchone=True, commit=False)
    am = await db_execute("SELECT COUNT(*) AS n FROM messages WHERE receiver_id=?", (user_id,), fetchone=True, commit=False)
    tv = await db_execute("SELECT COUNT(*) AS n FROM link_visits WHERE receiver_id=? AND created_at>=?", (user_id, day_start), fetchone=True, commit=False)
    av = await db_execute("SELECT COUNT(*) AS n FROM link_visits WHERE receiver_id=?", (user_id,), fetchone=True, commit=False)
    tc = tm["n"] if tm else 0
    ac = am["n"] if am else 0
    code = await get_or_create_link_code(user_id)
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    user = await db_execute("SELECT paid_until FROM users WHERE user_id=?", (user_id,), fetchone=True, commit=False)
    sub = "❌ Не активна"
    if user and user["paid_until"]:
        try:
            dt = datetime.fromisoformat(user["paid_until"])
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc): sub = f"✅ до {format_msk(user['paid_until'])}"
        except Exception: pass
    t_visits = tv["n"] if tv else 0
    a_visits = av["n"] if av else 0
    return f"📊 <b>СТАТИСТИКА ПРОФИЛЯ</b>\n\n📅 <b>За сегодня:</b>\n▫️ Сообщений: <b>{tc}</b>\n▫️ Переходов: <b>{t_visits}</b>\n▫️ Популярность: <b>{min(10, tc // 5)}/10</b>\n\n🌐 <b>За всё время:</b>\n▫️ Сообщений: <b>{ac}</b>\n▫️ Переходов: <b>{a_visits}</b>\n▫️ Уровень: <b>{min(10, ac // 5)}/10</b>\n\n💎 <b>Премиум:</b> {sub}\n\n🔗 <b>Ссылка:</b>\n<code>{link}</code>\n<i>(нажми, чтобы скопировать)</i>"

@dp.callback_query(F.data == "profile_stats")
async def profile_stats_cb(call: CallbackQuery):
    text = await build_profile_stats(call.from_user.id)
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=back_keyboard())
    else: await safe_edit_text(call, text, reply_markup=back_keyboard())
    await call.answer()

@dp.message(Command("stats"))
async def stats_command(message: Message):
    if message.chat.type == "private": await message.answer(await build_profile_stats(message.from_user.id), reply_markup=back_keyboard())

@dp.message(Command("help"))
async def help_command(message: Message):
    if message.chat.type == "private":
        await message.answer("❓ <b>КАК ЭТО РАБОТАЕТ</b>\n\n1️⃣ Нажми <b>«🔗 Моя ссылка»</b> и скопируй её.\n2️⃣ Размести ссылку в профиле.\n3️⃣ Получай анонимные сообщения.\n4️⃣ Чтобы ответить — сделай <b>свайп (Reply)</b>.\n5️⃣ Чтобы раскрыть автора — нажми <b>«✨ Узнать автора»</b>.\n\n💎 <b>Премиум</b> открывает авторов без ограничений.\n\n🔒 <i>Анонимность гарантирована.</i>", reply_markup=back_keyboard())

@dp.callback_query(F.data == "help")
async def help_cb(call: CallbackQuery):
    text = "❓ <b>КАК ЭТО РАБОТАЕТ</b>\n\n1️⃣ Нажми <b>«🔗 Моя ссылка»</b> и скопируй её.\n2️⃣ Размести ссылку в профиле.\n3️⃣ Получай анонимные сообщения.\n4️⃣ Чтобы ответить — сделай <b>свайп (Reply)</b>.\n5️⃣ Чтобы раскрыть автора — нажми <b>«✨ Узнать автора»</b>.\n\n💎 <b>Премиум</b> открывает авторов без ограничений.\n\n🔒 <i>Анонимность гарантирована.</i>"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=back_keyboard())
    else: await safe_edit_text(call, text, reply_markup=back_keyboard())
    await call.answer()

@dp.callback_query(F.data == "my_blocked")
async def my_blocked(call: CallbackQuery):
    blocked = await get_my_blocked_users(call.from_user.id)
    if not blocked:
        msg_text = "🚫 <b>БЛОК-ЛИСТ</b>\n\n✅ Список пуст."
        if call.message.photo:
            try: await call.message.delete()
            except Exception: pass
            await call.message.answer(msg_text, reply_markup=back_keyboard())
        else: await safe_edit_text(call, msg_text, reply_markup=back_keyboard())
        return await call.answer()
    await call.message.answer(f"🚫 <b>БЛОК-ЛИСТ</b>\n\nЗаблокировано: <b>{len(blocked)}</b>")
    for user in blocked:
        name = html.escape(user["first_name"] or "Без имени")
        username = f"@{html.escape(user['username'])}" if user["username"] else "без username"
        await call.message.answer(f"👤 <b>{name}</b> ({username})", reply_markup=blocked_user_keyboard(user["user_id"]))
    await call.answer()

@dp.callback_query(F.data.startswith("unblock:"))
async def unblock_from_list(call: CallbackQuery):
    try: sender_id = int(call.data.split(":", 1)[1])
    except ValueError: return await call.answer("Ошибка.", show_alert=True)
    if not await is_blocked(call.from_user.id, sender_id): return await call.answer("Уже разблокирован.", show_alert=True)
    await db_execute("DELETE FROM blocks WHERE receiver_id=? AND sender_id=?", (call.from_user.id, sender_id))
    await safe_edit_text(call, f"✅ Пользователь <code>{sender_id}</code> разблокирован.", reply_markup=back_keyboard())
    await call.answer("Разблокирован!")

@dp.callback_query(F.data.startswith("block:"))
async def block_author(call: CallbackQuery):
    try: message_id = int(call.data.split(":", 1)[1])
    except ValueError: return await call.answer("Ошибка.", show_alert=True)
    row = await get_message(message_id)
    if not row or row["receiver_id"] != call.from_user.id: return await call.answer("Сообщение не найдено.", show_alert=True)
    await db_execute("INSERT OR IGNORE INTO blocks(receiver_id, sender_id) VALUES (?, ?)", (row["receiver_id"], row["sender_id"]))
    await call.answer("🚫 Автор заблокирован!", show_alert=True)

async def send_tariffs_view(message: Message):
    caption = "👑 <b>ВЫБЕРИ СВОЙ ТАРИФ:</b>"
    banner_file = find_existing_banner()
    if banner_file:
        try:
            await message.answer_photo(photo=FSInputFile(banner_file), caption=caption, reply_markup=prices_keyboard())
            return
        except Exception: pass
    await message.answer(caption, reply_markup=prices_keyboard())

@dp.callback_query(F.data == "prices")
async def prices(call: CallbackQuery):
    try: await call.message.delete()
    except Exception: pass
    await send_tariffs_view(call.message)
    await call.answer()

@dp.callback_query(F.data.startswith("who:"))
async def who_author(call: CallbackQuery):
    try: message_id = int(call.data.split(":", 1)[1])
    except ValueError: return await call.answer("Ошибка.", show_alert=True)
    row = await get_message(message_id)
    if not row or row["receiver_id"] != call.from_user.id: return await call.answer("Сообщение не найдено.", show_alert=True)
    if not await user_is_paid(call.from_user.id):
        await send_tariffs_view(call.message)
        return await call.answer()
    sender_id = row["sender_id"]
    user = await db_execute("SELECT * FROM users WHERE user_id=?", (sender_id,), fetchone=True, commit=False)
    first_name = html.escape(user["first_name"]) if user and user["first_name"] else "Не указано"
    username = f"@{html.escape(user['username'])}" if user and user["username"] else "<i>(скрыт)</i>"
    tg_link = f"<a href='tg://user?id={sender_id}'>Написать в ЛС</a>"
    await bot.send_message(call.from_user.id, f"🔓 <b>АВТОР РАСКРЫТ</b>\n\n👤 <b>Имя:</b> {first_name}\n🔗 <b>Username:</b> {username}\n🆔 <b>ID:</b> <code>{sender_id}</code>\n💬 <b>Контакт:</b> {tg_link}", disable_web_page_preview=True)
    await call.answer("Личность раскрыта!")

@dp.callback_query(F.data.startswith("buy:"))
async def buy_start(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    plan = PLANS.get(key)
    if not plan: return await call.answer("Тариф не найден.", show_alert=True)
    await state.set_state(UserStates.waiting_email)
    await state.update_data(chosen_plan=key)
    await call.message.answer(f"💎 <b>ОФОРМЛЕНИЕ ПОДПИСКИ</b>\n\n📦 Тариф: <b>{plan['title']}</b>\n💰 Стоимость: <b>{plan['rubles']} ₽</b>\n\n📧 <b>Напиши свой Email</b>:\nНа него ЮKassa отправит чек.")
    await call.answer()

@dp.message(UserStates.waiting_email)
async def buy_receive_email(message: Message, state: FSMContext):
    email = (message.text or "").strip()
    if len(email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+", email):
        return await message.answer("❌ Неверный формат почты! Напиши корректный email, например: <code>name@mail.ru</code>")
    data = await state.get_data()
    key = data.get("chosen_plan")
    plan = PLANS.get(key)
    if not plan:
        await state.clear()
        return await message.answer("Ошибка тарифа.", reply_markup=main_keyboard())
    amount_value = f"{plan['rubles']:.2f}"
    description = f"Премиум-подписка: {plan['title']}"
    receipt = {"customer": {"email": email}, "tax_system_code": 2, "items": [{"description": description, "quantity": "1.00", "amount": {"value": amount_value, "currency": "RUB"}, "vat_code": 1, "payment_mode": "full_payment", "payment_subject": "service"}]}
    try:
        payment = await asyncio.to_thread(Payment.create, {"amount": {"value": amount_value, "currency": "RUB"}, "capture": True, "confirmation": {"type": "redirect", "return_url": RETURN_URL}, "description": description, "receipt": receipt, "metadata": {"user_id": str(message.from_user.id), "plan": key}}, str(uuid.uuid4()))
        await db_execute("INSERT OR REPLACE INTO payments (telegram_charge_id, user_id, plan, created_at, status) VALUES (?, ?, ?, ?, 'pending')", (payment.id, message.from_user.id, key, datetime.now(timezone.utc).isoformat()))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить {plan['rubles']} ₽", url=payment.confirmation.confirmation_url)], [InlineKeyboardButton(text="🔄 Я оплатил (проверить)", callback_data=f"check_pay:{payment.id}")]])
        await message.answer(f"🧾 <b>СЧЁТ СОЗДАН</b>\n\n📦 Тариф: <b>{plan['title']}</b>\n💰 Сумма: <b>{plan['rubles']} ₽</b>\n📧 Чек на: <code>{html.escape(email)}</code>\n\nПосле оплаты нажми <b>«🔄 Я оплатил»</b>.", reply_markup=keyboard)
        await state.clear()
    except Exception:
        logging.error("ОШИБКА ЮKASSA", exc_info=True)
        await message.answer("❌ Не удалось создать счёт. Попробуй позже.", reply_markup=main_keyboard())
        await state.clear()

async def activate_payment(payment_id: str):
    row = await db_execute("SELECT * FROM payments WHERE telegram_charge_id=?", (payment_id,), fetchone=True, commit=False)
    if not row or row["status"] == "succeeded": return False
    plan = PLANS.get(row["plan"])
    if not plan: return False
    user = await db_execute("SELECT paid_until FROM users WHERE user_id=?", (row["user_id"],), fetchone=True, commit=False)
    now = datetime.now(timezone.utc)
    base = now
    if user and user["paid_until"]:
        try:
            old_until = datetime.fromisoformat(user["paid_until"])
            if old_until.tzinfo is None: old_until = old_until.replace(tzinfo=timezone.utc)
            if old_until > now: base = old_until
        except Exception: pass
    until_iso = (base + timedelta(days=plan["days"])).isoformat()
    await db_execute("UPDATE users SET paid_until=? WHERE user_id=?", (until_iso, row["user_id"]))
    await db_execute("UPDATE payments SET status='succeeded' WHERE telegram_charge_id=?", (payment_id,))
    try: await bot.send_message(row["user_id"], f"🎉 <b>ОПЛАТА ПОЛУЧЕНА</b>\n\n💎 Премиум активирован до:\n<b>{format_msk(until_iso)}</b>\n\nТеперь нажимай <b>«✨ Узнать автора»</b>!", reply_markup=main_keyboard())
    except Exception: pass
    return True

@dp.callback_query(F.data.startswith("check_pay:"))
async def manual_check_payment(call: CallbackQuery):
    payment_id = call.data.split(":", 1)[1]
    await call.answer("Проверяем статус...")
    try:
        payment = await asyncio.to_thread(Payment.find_one, payment_id)
        if payment.status == "succeeded":
            await activate_payment(payment_id)
            await safe_edit_text(call, "🎉 <b>ОПЛАТА ПОДТВЕРЖДЕНА</b>\n\nПремиум-подписка активирована.", reply_markup=main_keyboard())
        elif payment.status == "pending": await call.answer("Платёж ещё обрабатывается. Подожди 10-15 секунд.", show_alert=True)
        elif payment.status == "canceled": await call.answer("Платёж был отменён.", show_alert=True)
        else: await call.answer(f"Статус: {payment.status}", show_alert=True)
    except Exception: await call.answer("Ошибка связи с ЮKassa.", show_alert=True)

async def yookassa_webhook(request: web.Request):
    try:
        data = await request.json()
        if data.get("event") != "payment.succeeded": return web.json_response({"ok": True})
        payment_id = data.get("object", {}).get("id")
        if payment_id:
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
            if payment.status == "succeeded": await activate_payment(payment_id)
        return web.json_response({"ok": True})
    except Exception:
        logging.error("Webhook Error", exc_info=True)
        return web.json_response({"ok": False}, status=500)

@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    users = await db_execute("SELECT COUNT(*) AS n FROM users", fetchone=True, commit=False)
    messages = await db_execute("SELECT COUNT(*) AS n FROM messages", fetchone=True, commit=False)
    await message.answer(f"🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\n👥 Пользователей: <b>{users['n']}</b>\n💬 Анонимок: <b>{messages['n']}</b>\n\nВыбери раздел:", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.clear()
    users = await db_execute("SELECT COUNT(*) AS n FROM users", fetchone=True, commit=False)
    messages = await db_execute("SELECT COUNT(*) AS n FROM messages", fetchone=True, commit=False)
    text = f"🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\n👥 Пользователей: <b>{users['n']}</b>\n💬 Анонимок: <b>{messages['n']}</b>\n\nВыбери раздел:"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_keyboard())
    else: await safe_edit_text(call, text, reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    today_start = utc_day_start()
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    tu = await db_execute("SELECT COUNT(*) AS n FROM users", fetchone=True, commit=False)
    pu = await db_execute("SELECT COUNT(*) AS n FROM users WHERE paid_until IS NOT NULL AND paid_until > ?", (datetime.now(timezone.utc).isoformat(),), fetchone=True, commit=False)
    tm = await db_execute("SELECT COUNT(*) AS n FROM messages", fetchone=True, commit=False)
    tdm = await db_execute("SELECT COUNT(*) AS n FROM messages WHERE created_at >= ?", (today_start,), fetchone=True, commit=False)
    wkm = await db_execute("SELECT COUNT(*) AS n FROM messages WHERE created_at >= ?", (week_start,), fetchone=True, commit=False)
    tv = await db_execute("SELECT COUNT(*) AS n FROM link_visits", fetchone=True, commit=False)
    sc = await db_execute("SELECT COUNT(*) AS n FROM payments WHERE status='succeeded'", fetchone=True, commit=False)
    revenue = 0
    rows = await db_execute("SELECT plan FROM payments WHERE status='succeeded'", fetchall=True, commit=False)
    for r in rows:
        p = PLANS.get(r["plan"])
        if p: revenue += p["rubles"]
    text = f"📊 <b>СТАТИСТИКА БОТА</b>\n\n👥 <b>Пользователи:</b> {tu['n']}\n💎 <b>С подпиской:</b> {pu['n']}\n\n💬 <b>Анонимок всего:</b> {tm['n']}\n📅 <b>За сегодня:</b> {tdm['n']}\n📆 <b>За 7 дней:</b> {wkm['n']}\n\n🔗 <b>Переходов:</b> {tv['n']}\n\n✅ <b>Оплат:</b> {sc['n']}\n💰 <b>Выручка:</b> {revenue} ₽"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_keyboard())
    else: await safe_edit_text(call, text, reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_chart")
async def admin_chart(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    text = "📈 <b>АКТИВНОСТЬ ЗА 7 ДНЕЙ</b>\n\n<i>(Количество новых анонимок)</i>\n\n"
    for i in range(6, -1, -1):
        ds = (datetime.now(timezone.utc) - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        de = ds + timedelta(days=1)
        c = await db_execute("SELECT COUNT(*) as n FROM messages WHERE created_at >= ? AND created_at < ?", (ds.isoformat(), de.isoformat()), fetchone=True, commit=False)
        n = c["n"] if c else 0
        bar = "█" * min(n, 15) + ("…" if n > 15 else "")
        text += f"<b>{ds.astimezone(MSK_TZ).strftime('%d.%m')}:</b> {bar} <i>({n})</i>\n"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_keyboard())
    else: await safe_edit_text(call, text, reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    rows = await db_execute("SELECT user_id, username, first_name, paid_until, created_at FROM users ORDER BY created_at DESC LIMIT 15", fetchall=True, commit=False)
    if not rows: text = "👥 <b>Пользователи</b>\n\nПока никого нет."
    else:
        lines = ["👥 <b>Последние 15 пользователей</b>\n"]
        for r in rows:
            name = html.escape(r["first_name"] or "—")
            uname = f"@{html.escape(r['username'])}" if r["username"] else "—"
            status = "💎" if r["paid_until"] and r["paid_until"] > datetime.now(timezone.utc).isoformat() else "▫️"
            lines.append(f"{status} <b>{name}</b> ({uname})\n   <code>{r['user_id']}</code>")
        text = "\n".join(lines)
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_keyboard())
    else: await safe_edit_text(call, text, reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_snipers")
async def admin_snipers(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    rows = await db_execute("SELECT lv.visitor_id, u.first_name, u.username, COUNT(*) as visits FROM link_visits lv LEFT JOIN users u ON u.user_id = lv.visitor_id WHERE lv.visitor_id NOT IN (SELECT sender_id FROM messages) GROUP BY lv.visitor_id ORDER BY visits DESC LIMIT 15", fetchall=True, commit=False)
    if not rows: text = "🎯 <b>СНАЙПЕРЫ</b>\n\n✅ Все, кто переходил по ссылкам, уже написали сообщения."
    else:
        text = "🎯 <b>СНАЙПЕРЫ</b> (перешли, но не написали)\n\n"
        for r in rows:
            name = html.escape(r["first_name"] or "—")
            uname = f"@{html.escape(r['username'])}" if r["username"] else "—"
            text += f"👤 {name} ({uname}) — <b>{r['visits']}</b> визитов\n   <a href='tg://user?id={r['visitor_id']}'>Написать в ЛС</a>\n\n"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_keyboard(), disable_web_page_preview=True)
    else: await safe_edit_text(call, text, reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_top_donors")
async def admin_top_donors(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    rows = await db_execute("SELECT p.user_id, u.first_name, u.username, COUNT(*) as cnt, SUM(CASE p.plan WHEN '1d' THEN 99 WHEN '7d' THEN 299 WHEN '30d' THEN 799 ELSE 0 END) as total FROM payments p LEFT JOIN users u ON u.user_id = p.user_id WHERE p.status = 'succeeded' GROUP BY p.user_id ORDER BY total DESC LIMIT 10", fetchall=True, commit=False)
    if not rows: text = "💰 <b>ТОП ДОНАТЕРОВ</b>\n\nПока нет успешных платежей."
    else:
        text = "💰 <b>ТОП-10 ДОНАТЕРОВ</b>\n\n"
        for i, r in enumerate(rows, 1):
            name = html.escape(r["first_name"] or "—")
            uname = f"@{html.escape(r['username'])}" if r["username"] else "—"
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            text += f"{medals} <b>{name}</b> ({uname})\n   💵 Потрачено: <b>{r['total']} ₽</b> | Покупок: {r['cnt']}\n\n"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_keyboard())
    else: await safe_edit_text(call, text, reply_markup=admin_keyboard())
    await call.answer()

@dp.message(Command("spy"))
async def admin_spy_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(UserStates.waiting_spy_id)
    await message.answer("🕵️ <b>СЛЕЖКА</b>\n\nОтправь <b>ID пользователя</b>:", reply_markup=admin_cancel_keyboard())

@dp.message(UserStates.waiting_spy_id)
async def process_spy_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return await state.clear()
    try: target_id = int((message.text or "").strip())
    except ValueError: return await message.answer("❌ ID должен быть числом.", reply_markup=admin_cancel_keyboard())
    await state.clear()
    rows = await db_execute("SELECT * FROM messages WHERE sender_id=? OR receiver_id=? ORDER BY id DESC LIMIT 20", (target_id, target_id), fetchall=True, commit=False)
    if not rows: return await message.answer(f"У пользователя <code>{target_id}</code> нет сообщений.", reply_markup=admin_keyboard())
    text = f"🕵️ <b>ПЕРЕПИСКА ЮЗЕРА</b> <code>{target_id}</code>\n\n"
    for r in rows:
        arrow = "📤" if r["sender_id"] == target_id else "📥"
        who = r["receiver_id"] if r["sender_id"] == target_id else r["sender_id"]
        body = (r["text"] or "[медиа]")[:100]
        text += f"{arrow} <code>{who}</code>: {html.escape(body)}\n"
    await message.answer(text, reply_markup=admin_keyboard())

@dp.message(Command("nuke"))
async def admin_nuke_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(UserStates.waiting_nuke_id)
    await message.answer("💣 <b>КРАСНАЯ КНОПКА</b>\n\nОтправь <b>ID пользователя</b> для удаления:", reply_markup=admin_cancel_keyboard())

@dp.message(UserStates.waiting_nuke_id)
async def process_nuke_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return await state.clear()
    try: target_id = int((message.text or "").strip())
    except ValueError: return await message.answer("❌ ID должен быть числом.", reply_markup=admin_cancel_keyboard())
    await state.clear()
    for sql in ["DELETE FROM messages WHERE sender_id=? OR receiver_id=?", "DELETE FROM personal_links WHERE user_id=?", "DELETE FROM link_visits WHERE receiver_id=? OR visitor_id=?", "DELETE FROM blocks WHERE receiver_id=? OR sender_id=?", "DELETE FROM payments WHERE user_id=?", "DELETE FROM users WHERE user_id=?"]:
        await db_execute(sql, (target_id, target_id))
    await message.answer(f"💣 Пользователь <code>{target_id}</code> полностью удалён.", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_subs")
async def admin_subs(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    text = "💎 <b>УПРАВЛЕНИЕ ПОДПИСКАМИ</b>\n\n🎁 <b>Выдать</b> — продлить доступ.\n🚫 <b>Отозвать</b> — снять доступ.\n\nВыбери действие:"
    if call.message.photo:
        try: await call.message.delete()
        except Exception: pass
        await call.message.answer(text, reply_markup=admin_subs_keyboard())
    else: await safe_edit_text(call, text, reply_markup=admin_subs_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_give")
async def admin_give_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.set_state(UserStates.waiting_sub_target)
    await state.update_data(sub_action="give")
    await call.message.answer("🎁 <b>Выдача подписки</b>\n\nОтправь <b>ID пользователя</b>:", reply_markup=admin_cancel_keyboard())
    await call.answer()

@dp.callback_query(F.data == "admin_revoke")
async def admin_revoke_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.set_state(UserStates.waiting_sub_target)
    await state.update_data(sub_action="revoke")
    await call.message.answer("🚫 <b>Отзыв подписки</b>\n\nОтправь <b>ID пользователя</b>:", reply_markup=admin_cancel_keyboard())
    await call.answer()

@dp.message(UserStates.waiting_sub_target)
async def admin_sub_target(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return await state.clear()
    try: target_id = int((message.text or "").strip())
    except ValueError: return await message.answer("❌ ID должен быть числом.", reply_markup=admin_cancel_keyboard())
    action = (await state.get_data()).get("sub_action")
    user = await db_execute("SELECT paid_until FROM users WHERE user_id=?", (target_id,), fetchone=True, commit=False)
    if not user:
        await state.clear()
        return await message.answer(f"❌ Пользователь <code>{target_id}</code> не найден.", reply_markup=admin_keyboard())
    if action == "revoke":
        if not user["paid_until"]:
            await state.clear()
            return await message.answer(f"ℹ️ У <code>{target_id}</code> подписка и так не активна.", reply_markup=admin_keyboard())
        await db_execute("UPDATE users SET paid_until=NULL WHERE user_id=?", (target_id,))
        await state.clear()
        await message.answer(f"🚫 Подписка отозвана.\nID: <code>{target_id}</code>", reply_markup=admin_keyboard())
        try: await bot.send_message(target_id, "⚠️ <b>Ваш премиум-доступ был отозван администратором.</b>", reply_markup=main_keyboard())
        except Exception: pass
        return
    await state.update_data(sub_target=target_id)
    await state.set_state(UserStates.waiting_sub_action)
    await message.answer(f"✅ Найден: <code>{target_id}</code>\n\nСколько <b>дней</b> выдать?", reply_markup=admin_cancel_keyboard())

@dp.message(UserStates.waiting_sub_action)
async def admin_sub_days(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return await state.clear()
    try:
        days = int((message.text or "").strip())
        if days <= 0: raise ValueError
    except ValueError: return await message.answer("❌ Дни должны быть > 0.", reply_markup=admin_cancel_keyboard())
    target_id = (await state.get_data()).get("sub_target")
    if not target_id:
        await state.clear()
        return await message.answer("⚠️ Ошибка. Начни заново.", reply_markup=admin_keyboard())
    user = await db_execute("SELECT paid_until FROM users WHERE user_id=?", (target_id,), fetchone=True, commit=False)
    now = datetime.now(timezone.utc)
    base = now
    if user and user["paid_until"]:
        try:
            old_until = datetime.fromisoformat(user["paid_until"])
            if old_until.tzinfo is None: old_until = old_until.replace(tzinfo=timezone.utc)
            if old_until > now: base = old_until
        except Exception: pass
    until_iso = (base + timedelta(days=days)).isoformat()
    await db_execute("UPDATE users SET paid_until=? WHERE user_id=?", (until_iso, target_id))
    await state.clear()
    await message.answer(f"🎁 Подписка выдана.\nID: <code>{target_id}</code>\n📅 До: <b>{format_msk(until_iso)}</b>", reply_markup=admin_keyboard())
    try: await bot.send_message(target_id, f"🎁 <b>Вам активирован премиум-доступ!</b>\n\nСрок: до <b>{format_msk(until_iso)}</b>.", reply_markup=main_keyboard())
    except Exception: pass

@dp.callback_query(F.data == "admin_backup")
async def admin_backup_cb(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    if not os.path.exists(DB_FILE): return await call.answer("Файл базы не найден!", show_alert=True)
    await call.answer("Отправляю...")
    await bot.send_document(call.from_user.id, FSInputFile(DB_FILE, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.sqlite3"), caption="📦 Свежая копия БД")

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.set_state(UserStates.waiting_broadcast)
    await call.message.answer("📢 <b>Рассылка</b>\n\nОтправь сообщение или /cancel для отмены.", reply_markup=admin_cancel_keyboard())
    await call.answer()

@dp.message(UserStates.waiting_broadcast, Command("cancel"))
async def cancel_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=main_keyboard())

@dp.message(UserStates.waiting_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return await state.clear()
    await state.clear()
    rows = await db_execute("SELECT user_id FROM users", fetchall=True, commit=False)
    sent = failed = 0
    status_msg = await message.answer("⏳ Рассылка выполняется…")
    for row in rows:
        try:
            await bot.copy_message(row["user_id"], message.chat.id, message.message_id)
            sent += 1
        except Exception: failed += 1
        await asyncio.sleep(0.04)
    await status_msg.edit_text(f"📢 <b>Рассылка завершена</b>\n\n✅ Доставлено: <b>{sent}</b>\n❌ Не доставлено: <b>{failed}</b>")

async def main():
    global BOT_USERNAME
    await init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню / Ссылка"),
        BotCommand(command="stats", description="Статистика профиля"),
        BotCommand(command="help", description="Инструкция"),
    ])
    app = web.Application()
    app.router.add_post("/yookassa/webhook", yookassa_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    print(f"🚀 Бот @{BOT_USERNAME} успешно запущен!")
    print(f"🌐 Webhook слушает порт {WEBHOOK_PORT}")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
