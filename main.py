# -*- coding: utf-8 -*-
import asyncio
import html
import os
import re
import secrets
import uuid
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_FILE = os.getenv("DB_FILE", "/app/data/new_bot.sqlite3")
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "3000"))
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/").strip()

# Поддерживаем любые форматы баннера (png, jpg, jpeg) в корне или папке data
BANNER_CANDIDATES = [
    "banner.png",
    "banner.jpg",
    "banner.jpeg",
    "/app/banner.png",
    "/app/banner.jpg",
    "/app/data/banner.png",
    "/app/data/banner.jpg",
]

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


def format_msk(dt_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_msk = dt.astimezone(MSK_TZ)
        return dt_msk.strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return "активно"


def find_existing_banner() -> str | None:
    for path in BANNER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


class UserStates(StatesGroup):
    waiting_question = State()
    waiting_broadcast = State()
    waiting_email = State()


PLANS = {
    "1d": {"title": "1 день", "days": 1, "rubles": 99},
    "7d": {"title": "7 дней", "days": 7, "rubles": 299},
    "30d": {"title": "30 дней", "days": 30, "rubles": 799},
}


async def db_execute(sql, params=(), fetchone=False, fetchall=False, commit=True):
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        result = None
        if fetchone:
            result = await cursor.fetchone()
        elif fetchall:
            result = await cursor.fetchall()
        else:
            result = cursor.rowcount
        if commit:
            await db.commit()
        return result


async def init_db():
    await db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            paid_until TEXT,
            created_at TEXT NOT NULL
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS personal_links (
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS link_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receiver_id INTEGER NOT NULL,
            visitor_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            text TEXT,
            telegram_message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            replied INTEGER DEFAULT 0,
            reported INTEGER DEFAULT 0,
            recipient_message_id INTEGER
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS blocks (
            receiver_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            PRIMARY KEY(receiver_id, sender_id)
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS payments (
            telegram_charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)

    columns = await db_execute("PRAGMA table_info(messages)", fetchall=True, commit=False)
    names = {row["name"] for row in columns}
    if "recipient_message_id" not in names:
        await db_execute("ALTER TABLE messages ADD COLUMN recipient_message_id INTEGER")


async def save_user(user):
    now = datetime.now(timezone.utc).isoformat()
    await db_execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user.id, user.username or "", user.first_name or "", now))


async def get_or_create_link_code(user_id: int) -> str:
    row = await db_execute(
        "SELECT code FROM personal_links WHERE user_id=?",
        (user_id,), fetchone=True, commit=False,
    )
    if row:
        return row["code"]

    while True:
        code = secrets.token_urlsafe(8)
        exists = await db_execute(
            "SELECT 1 FROM personal_links WHERE code=?",
            (code,), fetchone=True, commit=False,
        )
        if not exists:
            break

    await db_execute(
        "INSERT INTO personal_links(code, user_id, created_at) VALUES (?, ?, ?)",
        (code, user_id, datetime.now(timezone.utc).isoformat()),
    )
    return code


async def user_id_by_code(code: str):
    row = await db_execute(
        "SELECT user_id FROM personal_links WHERE code=?",
        (code,), fetchone=True, commit=False,
    )
    return row["user_id"] if row else None


async def get_message(message_id):
    return await db_execute(
        "SELECT * FROM messages WHERE id=?",
        (message_id,), fetchone=True, commit=False,
    )


async def is_blocked(receiver_id, sender_id):
    row = await db_execute(
        "SELECT 1 FROM blocks WHERE receiver_id=? AND sender_id=?",
        (receiver_id, sender_id), fetchone=True, commit=False,
    )
    return row is not None


async def user_is_paid(user_id: int) -> bool:
    row = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (user_id,), fetchone=True, commit=False,
    )
    if not row or not row["paid_until"]:
        return False
    try:
        dt = datetime.fromisoformat(row["paid_until"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception:
        return False


async def get_my_blocked_users(user_id):
    return await db_execute("""
        SELECT u.user_id, u.username, u.first_name
        FROM blocks b LEFT JOIN users u ON u.user_id=b.sender_id
        WHERE b.receiver_id=? ORDER BY b.sender_id DESC
    """, (user_id,), fetchall=True, commit=False)


async def unblock_user(receiver_id, sender_id):
    await db_execute(
        "DELETE FROM blocks WHERE receiver_id=? AND sender_id=?",
        (receiver_id, sender_id),
    )


# ================= КЛАВИАТУРЫ =================

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
        [InlineKeyboardButton(text="📊 Статистика профиля", callback_data="profile_stats")],
        [InlineKeyboardButton(text="💎 Премиум-подписка", callback_data="prices")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="my_blocked")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def link_keyboard(link: str):
    share_url = (
        f"https://t.me/share/url?url={link}&"
        "text=%D0%9D%D0%B0%D0%BF%D0%B8%D1%88%D0%B8%20%D0%BC%D0%BD%D0%B5%20%D1%87%D1%82%D0%BE-%D0%BD%D0%B8%D0%B1%D1%83%D0%B4%D1%8C%20%D0%B0%D0%BD%D0%BE%D0%BD%D0%B8%D0%BC%D0%BD%D0%BE%20%F0%9F%A4%AB"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться в Telegram", url=share_url)],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back")],
    ])


def message_keyboard(message_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Узнать автора", callback_data=f"who:{message_id}")],
        [InlineKeyboardButton(text="🚫 Заблокировать автора", callback_data=f"block:{message_id}")],
    ])


def blocked_user_keyboard(sender_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"unblock:{sender_id}")]
    ])


def prices_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 1 ДЕНЬ — 99 ₽", callback_data="buy:1d")],
        [InlineKeyboardButton(text="🗓 7 ДНЕЙ — 299 ₽", callback_data="buy:7d")],
        [InlineKeyboardButton(text="👑 30 ДНЕЙ — 799 ₽", callback_data="buy:30d")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back")]
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💎 Выдать подписку", callback_data="admin_sub_info")],
        [InlineKeyboardButton(text="📥 Скачать базу данных", callback_data="admin_backup")],
    ])


async def safe_edit_text(call: CallbackQuery, text: str, reply_markup=None):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        else:
            await call.message.answer(text, reply_markup=reply_markup)
    except Exception:
        await call.message.answer(text, reply_markup=reply_markup)


def build_link_text(username: str, code: str) -> str:
    link = f"https://t.me/{username}?start={code}"
    return (
        "🔗 <b>Твоя персональная ссылка:</b>\n\n"
        f"<code>{link}</code>\n\n"
        "👆 <i>Нажми на ссылку выше, чтобы моментально скопировать её!</i>\n\n"
        "Размести её в описании своего профиля Telegram, канала, VK или био Instagram."
    )


# ================= ХЕНДЛЕРЫ =================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return

    await save_user(message.from_user)
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) == 2 else ""

    if payload:
        receiver_id = await user_id_by_code(payload)
        if receiver_id is None:
            await message.answer("❌ Некорректная или устаревшая ссылка.")
            return
        if receiver_id == message.from_user.id:
            await message.answer("🙂 Это твоя собственная ссылка! Отправь её друзьям.")
            return
        if await is_blocked(receiver_id, message.from_user.id):
            await message.answer("🚫 Этот пользователь ограничил получение сообщений от тебя.")
            return

        await db_execute(
            "INSERT INTO link_visits(receiver_id, visitor_id, created_at) VALUES (?, ?, ?)",
            (receiver_id, message.from_user.id, datetime.now(timezone.utc).isoformat()),
        )
        await state.set_state(UserStates.waiting_question)
        await state.update_data(receiver_id=receiver_id)
        await message.answer(
            "✍️ <b>Напиши анонимное сообщение:</b>\n\n"
            "Можно отправить текст, фото, видео или голосовое.\n"
            "🔒 <i>Получатель не узнает твой профиль.</i>"
        )
        return

    code = await get_or_create_link_code(message.from_user.id)
    text = (
        "👀 <b>Начни получать анонимные вопросы прямо сейчас!</b>\n\n"
        + build_link_text(BOT_USERNAME, code)
    )
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await message.answer(text, reply_markup=link_keyboard(link))
    await message.answer("📱 <b>Управление ботом:</b>", reply_markup=main_keyboard())


def utc_day_start():
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


async def build_profile_stats(user_id: int) -> str:
    day_start = utc_day_start()
    today_messages = await db_execute(
        "SELECT COUNT(*) AS n FROM messages WHERE receiver_id=? AND created_at>=?",
        (user_id, day_start), fetchone=True, commit=False,
    )
    all_messages = await db_execute(
        "SELECT COUNT(*) AS n FROM messages WHERE receiver_id=?",
        (user_id,), fetchone=True, commit=False,
    )
    today_visits = await db_execute(
        "SELECT COUNT(*) AS n FROM link_visits WHERE receiver_id=? AND created_at>=?",
        (user_id, day_start), fetchone=True, commit=False,
    )
    all_visits = await db_execute(
        "SELECT COUNT(*) AS n FROM link_visits WHERE receiver_id=?",
        (user_id,), fetchone=True, commit=False,
    )

    today_count = today_messages["n"] if today_messages else 0
    total_count = all_messages["n"] if all_messages else 0
    today_level = min(10, today_count // 5)
    total_level = min(10, total_count // 5)

    code = await get_or_create_link_code(user_id)
    link = f"https://t.me/{BOT_USERNAME}?start={code}"

    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (user_id,), fetchone=True, commit=False,
    )

    subscription_str = "❌ Не активна"
    if user and user["paid_until"]:
        try:
            dt = datetime.fromisoformat(user["paid_until"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                subscription_str = f"✅ Активна до {format_msk(user['paid_until'])}"
        except Exception:
            pass

    t_visits = today_visits["n"] if today_visits else 0
    a_visits = all_visits["n"] if all_visits else 0

    return (
        "📊 <b>Статистика твоего профиля:</b>\n\n"
        "📅 <b>За сегодня:</b>\n"
        f"▫️ Сообщений: <b>{today_count}</b>\n"
        f"▫️ Переходов по ссылке: <b>{t_visits}</b>\n"
        f"▫️ Популярность: <b>{today_level}/10</b>\n\n"
        "🌐 <b>За всё время:</b>\n"
        f"▫️ Всего сообщений: <b>{total_count}</b>\n"
        f"▫️ Всего переходов: <b>{a_visits}</b>\n"
        f"▫️ Общий уровень: <b>{total_level}/10</b>\n\n"
        f"💎 <b>Премиум-подписка:</b> {subscription_str}\n\n"
        "🔗 <b>Твоя ссылка:</b>\n"
        f"<code>{link}</code>\n"
        "<i>(нажми, чтобы скопировать)</i>"
    )


@dp.callback_query(F.data == "profile_stats")
async def profile_stats_cb(call: CallbackQuery):
    text = await build_profile_stats(call.from_user.id)
    if call.message.photo:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=back_keyboard())
    else:
        await safe_edit_text(call, text, reply_markup=back_keyboard())
    await call.answer()


@dp.message(Command("stats"))
async def stats_command(message: Message):
    if message.chat.type == "private":
        text = await build_profile_stats(message.from_user.id)
        await message.answer(text, reply_markup=back_keyboard())


@dp.message(Command("help"))
async def help_command(message: Message):
    if message.chat.type == "private":
        await message.answer(
            "❓ <b>Как пользоваться ботом:</b>\n\n"
            "1. Нажми <b>«🔗 Моя ссылка»</b> и скопируй её.\n"
            "2. Опубликуй ссылку в своём Telegram, истории или соцсетях.\n"
            "3. Получай анонимные признания и вопросы.\n"
            "4. Чтобы ответить на вопрос — просто сделай <b>«Свайп» (Reply)</b> на сообщение.\n"
            "5. Чтобы раскрыть личность отправителя — нажми <b>«✨ Узнать автора»</b> под вопросом.\n\n"
            "👑 <b>Премиум-подписка</b> позволяет раскрывать авторов без ограничений весь срок действия!",
            reply_markup=back_keyboard(),
        )


@dp.callback_query(F.data == "help")
async def help_cb(call: CallbackQuery):
    text = (
        "❓ <b>Как пользоваться ботом:</b>\n\n"
        "1. Нажми <b>«🔗 Моя ссылка»</b> и скопируй её.\n"
        "2. Опубликуй ссылку в своём Telegram, истории или соцсетях.\n"
        "3. Получай анонимные признания и вопросы.\n"
        "4. Чтобы ответить на вопрос — просто сделай <b>«Свайп» (Reply)</b> на сообщение.\n"
        "5. Чтобы раскрыть личность отправителя — нажми <b>«✨ Узнать автора»</b> под вопросом.\n\n"
        "👑 <b>Премиум-подписка</b> позволяет раскрывать авторов без ограничений весь срок действия!"
    )
    if call.message.photo:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=back_keyboard())
    else:
        await safe_edit_text(call, text, reply_markup=back_keyboard())
    await call.answer()


@dp.message(UserStates.waiting_question)
async def receive_question(message: Message, state: FSMContext):
    data = await state.get_data()
    receiver_id = data.get("receiver_id")
    sender_id = message.from_user.id

    if not receiver_id:
        await state.clear()
        await message.answer("⚠️ Ссылка устарела. Открой её заново.")
        return
    if await is_blocked(receiver_id, sender_id):
        await state.clear()
        await message.answer("🚫 Этот пользователь заблокировал приём сообщений от тебя.")
        return

    count = await db_execute(
        "SELECT COUNT(*) AS count FROM messages WHERE sender_id=? AND receiver_id=? AND created_at>?",
        (sender_id, receiver_id,
         (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),
        fetchone=True, commit=False,
    )
    if count and count["count"] >= RATE_LIMIT:
        await state.clear()
        await message.answer("⚠️ Превышен лимит сообщений в час. Попробуй позже.")
        return

    text = message.text or message.caption or ""
    now = datetime.now(timezone.utc).isoformat()
    row = await db_execute(
        """INSERT INTO messages
        (sender_id, receiver_id, text, telegram_message_id, created_at)
        VALUES (?, ?, ?, ?, ?) RETURNING id""",
        (sender_id, receiver_id, text, message.message_id, now), fetchone=True,
    )
    message_id = row["id"]
    await state.clear()
    await message.answer("✅ <b>Сообщение успешно отправлено анонимно!</b>")

    try:
        header = "💌 <b>У тебя новое анонимное сообщение!</b>\n\n"
        footer = "\n\n↩️ <i>Сделай свайп (ответ) на это сообщение, чтобы ответить анонимно.</i>"

        if message.content_type == "text":
            body = f"<blockquote>{html.escape(message.text or '')}</blockquote>"
            sent = await bot.send_message(
                receiver_id, header + body + footer,
                reply_markup=message_keyboard(message_id),
            )
        else:
            caption_text = message.caption or ""
            body = (
                f"<blockquote>{html.escape(caption_text)}</blockquote>"
                if caption_text else ""
            )
            sent = await bot.copy_message(
                chat_id=receiver_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=header + body + footer,
                parse_mode=ParseMode.HTML,
                reply_markup=message_keyboard(message_id),
            )

        await db_execute(
            "UPDATE messages SET recipient_message_id=? WHERE id=?",
            (sent.message_id, message_id),
        )
    except Exception as error:
        print("Ошибка доставки сообщения:", error)
        await message.answer("⚠️ Не удалось доставить сообщение.")


@dp.message(F.reply_to_message)
async def swipe_reply(message: Message):
    if message.chat.type != "private":
        return
    row = await db_execute(
        "SELECT * FROM messages WHERE receiver_id=? AND recipient_message_id=?",
        (message.from_user.id, message.reply_to_message.message_id),
        fetchone=True, commit=False,
    )
    if not row:
        return
    try:
        await bot.send_message(row["sender_id"], "💬 <b>Тебе ответили на твой анонимный вопрос:</b>")
        await bot.copy_message(row["sender_id"], message.chat.id, message.message_id)
        await db_execute("UPDATE messages SET replied=1 WHERE id=?", (row["id"],))
        await message.answer("✅ Твой ответ отправлен автору!")
    except Exception:
        await message.answer("⚠️ Не удалось отправить ответ автору.")


@dp.callback_query(F.data == "my_link")
async def my_link(call: CallbackQuery):
    code = await get_or_create_link_code(call.from_user.id)
    text = build_link_text(BOT_USERNAME, code)
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    if call.message.photo:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=link_keyboard(link))
    else:
        await safe_edit_text(call, text, reply_markup=link_keyboard(link))
    await call.answer()


@dp.callback_query(F.data == "my_blocked")
async def my_blocked(call: CallbackQuery):
    blocked = await get_my_blocked_users(call.from_user.id)
    if not blocked:
        msg_text = "✅ <b>Список пуст:</b> у тебя нет заблокированных пользователей."
        if call.message.photo:
            try:
                await call.message.delete()
            except Exception:
                pass
            await call.message.answer(msg_text, reply_markup=back_keyboard())
        else:
            await safe_edit_text(call, msg_text, reply_markup=back_keyboard())
        await call.answer()
        return

    await call.message.answer(
        f"🚫 <b>Заблокировано пользователей: {len(blocked)}</b>\n"
        "Нажми кнопку ниже, чтобы разблокировать:"
    )
    for user in blocked:
        name = html.escape(user["first_name"] or "Без имени")
        username = (
            f"@{html.escape(user['username'])}"
            if user["username"] else "без username"
        )
        await call.message.answer(
            f"👤 <b>{name}</b> ({username})",
            reply_markup=blocked_user_keyboard(user["user_id"]),
        )
    await call.answer()


@dp.callback_query(F.data.startswith("unblock:"))
async def unblock_from_list(call: CallbackQuery):
    try:
        sender_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Ошибка пользователя.", show_alert=True)
        return
    if not await is_blocked(call.from_user.id, sender_id):
        await call.answer("Пользователь уже разблокирован.", show_alert=True)
        return
    await unblock_user(call.from_user.id, sender_id)
    await safe_edit_text(call, f"✅ Пользователь с ID <code>{sender_id}</code> разблокирован.", reply_markup=back_keyboard())
    await call.answer("Разблокирован!")


# ================= ЛАКОНИЧНЫЙ ВЫВОД ТАРИФОВ (ТЕКСТ УБРАН, ОСТАЛАСЬ КАРТИНКА И КНОПКИ) =================

async def send_tariffs_view(message: Message):
    caption = "👑 <b>ВЫБЕРИ СВОЙ ТАРИФ:</b>"

    banner_file = find_existing_banner()
    if banner_file:
        try:
            await message.answer_photo(
                photo=FSInputFile(banner_file),
                caption=caption,
                reply_markup=prices_keyboard()
            )
            return
        except Exception as e:
            print(f"Ошибка отправки файла баннера {banner_file}:", e)

    # Если файла с картинкой нет
    await message.answer(caption, reply_markup=prices_keyboard())


@dp.callback_query(F.data == "prices")
async def prices(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass

    await send_tariffs_view(call.message)
    await call.answer()


@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery):
    if call.message.photo:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer("📱 <b>Главное меню:</b>", reply_markup=main_keyboard())
    else:
        await safe_edit_text(call, "📱 <b>Главное меню:</b>", reply_markup=main_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("block:"))
async def block_author(call: CallbackQuery):
    try:
        message_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный запрос.", show_alert=True)
        return
    row = await get_message(message_id)
    if not row or row["receiver_id"] != call.from_user.id:
        await call.answer("Сообщение не найдено.", show_alert=True)
        return
    await db_execute(
        "INSERT OR IGNORE INTO blocks(receiver_id, sender_id) VALUES (?, ?)",
        (row["receiver_id"], row["sender_id"]),
    )
    await call.answer("🚫 Автор заблокирован и больше не сможет писать тебе!", show_alert=True)


async def send_author_card(target_user_id: int, message_row: aiosqlite.Row):
    sender_id = message_row["sender_id"]
    user = await db_execute(
        "SELECT * FROM users WHERE user_id=?",
        (sender_id,), fetchone=True, commit=False,
    )

    first_name = html.escape(user["first_name"]) if user and user["first_name"] else "Не указано"
    username = f"@{html.escape(user['username'])}" if user and user["username"] else "<i>(скрыт или отсутствует)</i>"
    tg_link = f"<a href='tg://user?id={sender_id}'>Написать в ЛС</a>"

    await bot.send_message(
        target_user_id,
        "🔓 <b>Автор сообщения раскрыт!</b>\n\n"
        f"👤 <b>Имя:</b> {first_name}\n"
        f"🔗 <b>Username:</b> {username}\n"
        f"🆔 <b>Telegram ID:</b> <code>{sender_id}</code>\n"
        f"💬 <b>Прямой контакт:</b> {tg_link}",
        disable_web_page_preview=True
    )


@dp.callback_query(F.data.startswith("who:"))
async def who_author(call: CallbackQuery):
    try:
        message_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный запрос.", show_alert=True)
        return

    row = await get_message(message_id)
    if not row or row["receiver_id"] != call.from_user.id:
        await call.answer("Сообщение не найдено.", show_alert=True)
        return

    if not await user_is_paid(call.from_user.id):
        await send_tariffs_view(call.message)
        await call.answer()
        return

    await send_author_card(call.from_user.id, row)
    await call.answer("Личность раскрыта!")


# ================= ОФОРМЛЕНИЕ ПЛАТЕЖА С ЧЕКОМ ПО 54-ФЗ =================

@dp.callback_query(F.data.startswith("buy:"))
async def buy_start(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    plan = PLANS.get(key)
    if not plan:
        await call.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(UserStates.waiting_email)
    await state.update_data(chosen_plan=key)

    await call.message.answer(
        f"💎 <b>Оформление подписки: {plan['title']} ({plan['rubles']} ₽)</b>\n\n"
        "📧 <b>Напиши свой Email</b>:\n"
        "На него ЮKassa отправит чек об оплате по 54-ФЗ."
    )
    await call.answer()


@dp.message(UserStates.waiting_email)
async def buy_receive_email(message: Message, state: FSMContext):
    email = (message.text or "").strip()

    email_pattern = (
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    )
    if len(email) > 254 or not re.fullmatch(email_pattern, email):
        await message.answer(
            "❌ Неверный формат почты! Напиши корректный email, например: <code>name@mail.ru</code>"
        )
        return

    data = await state.get_data()
    key = data.get("chosen_plan")
    plan = PLANS.get(key)
    if not plan:
        await state.clear()
        await message.answer("Ошибка тарифа. Открой меню заново.", reply_markup=main_keyboard())
        return

    amount_value = f"{plan['rubles']:.2f}"
    description = f"Премиум-подписка: {plan['title']}"

    receipt = {
        "customer": {"email": email},
        "tax_system_code": 2,  # УСН Доходы
        "items": [
            {
                "description": description,
                "quantity": "1.00",
                "amount": {
                    "value": amount_value,
                    "currency": "RUB",
                },
                "vat_code": 1,  # Без НДС
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }
        ],
    }

    try:
        payment = await asyncio.to_thread(
            Payment.create,
            {
                "amount": {
                    "value": amount_value,
                    "currency": "RUB",
                },
                "capture": True,
                "confirmation": {
                    "type": "redirect",
                    "return_url": RETURN_URL,
                },
                "description": description,
                "receipt": receipt,
                "metadata": {
                    "user_id": str(message.from_user.id),
                    "plan": key,
                },
            },
            str(uuid.uuid4()),
        )

        await db_execute(
            """
            INSERT OR REPLACE INTO payments
            (telegram_charge_id, user_id, plan, created_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (
                payment.id,
                message.from_user.id,
                key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"💳 Оплатить {plan['rubles']} ₽",
                        url=payment.confirmation.confirmation_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Я оплатил (проверить статус)",
                        callback_data=f"check_pay:{payment.id}",
                    )
                ]
            ]
        )

        await message.answer(
            f"🧾 <b>Счёт на оплату сформирован!</b>\n\n"
            f"Тариф: <b>{plan['title']}</b>\n"
            f"Сумма: <b>{plan['rubles']} ₽</b>\n"
            f"Чек будет выслан на: <code>{html.escape(email)}</code>\n\n"
            "После оплаты в ЮKassa нажми кнопку <b>«🔄 Я оплатил»</b> ниже.",
            reply_markup=keyboard,
        )
        await state.clear()

    except Exception as error:
        print(f"ОШИБКА ЮKASSA: {type(error).__name__}: {error!r}")
        await message.answer(
            "❌ Не удалось создать счёт на оплату. Попробуй позже или напиши поддержке.",
            reply_markup=main_keyboard()
        )
        await state.clear()


async def activate_payment(payment_id: str):
    row = await db_execute(
        "SELECT * FROM payments WHERE telegram_charge_id=?",
        (payment_id,), fetchone=True, commit=False,
    )
    if not row:
        print(f"⚠️ Платеж {payment_id} не найден в БД!")
        return False

    if row["status"] == "succeeded":
        return True

    plan = PLANS.get(row["plan"])
    if not plan:
        print(f"⚠️ Неизвестный тариф {row['plan']} у платежа {payment_id}")
        return False

    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (row["user_id"],), fetchone=True, commit=False,
    )
    now = datetime.now(timezone.utc)
    base = now

    if user and user["paid_until"]:
        try:
            old_until = datetime.fromisoformat(user["paid_until"])
            if old_until.tzinfo is None:
                old_until = old_until.replace(tzinfo=timezone.utc)
            if old_until > now:
                base = old_until
        except Exception:
            pass

    until = base + timedelta(days=plan["days"])
    until_iso = until.isoformat()

    await db_execute(
        "UPDATE users SET paid_until=? WHERE user_id=?",
        (until_iso, row["user_id"]),
    )
    await db_execute(
        "UPDATE payments SET status='succeeded' WHERE telegram_charge_id=?",
        (payment_id,),
    )
    print(f"✅ УСПЕШНО АКТИВИРОВАН платеж {payment_id} для user_id={row['user_id']} до {until_iso}")

    try:
        await bot.send_message(
            row["user_id"],
            "🎉 <b>Оплата успешно получена!</b>\n\n"
            f"Премиум-подписка активирована до:\n"
            f"<b>{format_msk(until_iso)}</b>\n\n"
            "Теперь нажимай <b>«✨ Узнать автора»</b> под любыми вопросами!",
            reply_markup=main_keyboard(),
        )
    except Exception as error:
        print("Не удалось отправить уведомление об оплате:", error)

    return True


@dp.callback_query(F.data.startswith("check_pay:"))
async def manual_check_payment(call: CallbackQuery):
    payment_id = call.data.split(":", 1)[1]
    await call.answer("Проверяем статус в банке...", show_alert=False)

    try:
        payment = await asyncio.to_thread(Payment.find_one, payment_id)
        print(f"🔍 Ручная проверка платежа {payment_id}: статус {payment.status}")

        if payment.status == "succeeded":
            activated = await activate_payment(payment_id)
            if activated:
                await safe_edit_text(
                    call,
                    "🎉 <b>Оплата подтверждена!</b>\n\nПремиум-подписка успешно активирована.",
                    reply_markup=main_keyboard(),
                )
            else:
                await call.message.answer("✅ Подписка уже активна в твоем профиле!", reply_markup=main_keyboard())
        elif payment.status == "pending":
            await call.answer("Платёж ещё обрабатывается банком. Подожди 10-15 секунд.", show_alert=True)
        elif payment.status == "canceled":
            await call.answer("Платёж был отменён или истёк срок.", show_alert=True)
        else:
            await call.answer(f"Статус платежа: {payment.status}", show_alert=True)
    except Exception as e:
        print(f"Ошибка проверки платежа {payment_id}: {e}")
        await call.answer("Ошибка при связи с ЮKassa. Попробуй ещё раз через пару секунд.", show_alert=True)


async def yookassa_webhook(request: web.Request):
    try:
        data = await request.json()
        event = data.get("event")
        payment_object = data.get("object", {})
        payment_id = payment_object.get("id")

        print(f"📩 ВЕБХУК: event={event}, id={payment_id}")

        if event != "payment.succeeded" or not payment_id:
            return web.json_response({"ok": True})

        payment = await asyncio.to_thread(Payment.find_one, payment_id)
        if payment.status != "succeeded":
            return web.json_response({"ok": True})

        await activate_payment(payment_id)
        return web.json_response({"ok": True})
    except Exception as error:
        print("Ошибка webhook ЮKassa:", error)
        return web.json_response({"ok": False}, status=500)


# ================= АДМИН-ПАНЕЛЬ =================

@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users = await db_execute("SELECT COUNT(*) AS n FROM users", fetchone=True, commit=False)
    messages = await db_execute("SELECT COUNT(*) AS n FROM messages", fetchone=True, commit=False)
    u_n = users['n'] if users else 0
    m_n = messages['n'] if messages else 0
    await message.answer(
        f"🛠 <b>Админ-панель бота</b>\n\n"
        f"👥 Всего пользователей: <b>{u_n}</b>\n"
        f"💬 Отправлено анонимок: <b>{m_n}</b>",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_backup")
async def admin_backup_cb(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    if not os.path.exists(DB_FILE):
        await call.answer("Файл базы не найден!", show_alert=True)
        return
    await call.answer("Отправляю резервную копию...", show_alert=False)
    await bot.send_document(
        chat_id=call.from_user.id,
        document=FSInputFile(DB_FILE, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.sqlite3"),
        caption="📦 <b>Свежая резервная копия базы данных</b>"
    )


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(UserStates.waiting_broadcast)
    await call.message.answer("📢 <b>Отправь сообщение для рассылки всем пользователям:</b>\n\nДля отмены напиши /cancel.")
    await call.answer()


@dp.message(UserStates.waiting_broadcast, Command("cancel"))
async def cancel_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=main_keyboard())


@dp.message(UserStates.waiting_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    await state.clear()
    rows = await db_execute("SELECT user_id FROM users", fetchall=True, commit=False)
    sent = failed = 0
    status_msg = await message.answer("⏳ Рассылка выполняется…")
    for row in rows:
        try:
            await bot.copy_message(row["user_id"], message.chat.id, message.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\nДоставлено: {sent}\nНе доставлено (бот заблокирован): {failed}"
    )


@dp.callback_query(F.data == "admin_sub_info")
async def admin_sub_info(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    await call.message.answer(
        "Команда для ручной выдачи подписки:\n"
        "<code>/give_sub ID ДНИ</code>\n\n"
        "Например, выдать на 30 дней:\n"
        "<code>/give_sub 123456789 30</code>"
    )
    await call.answer()


@dp.message(Command("give_sub"))
async def admin_give_sub(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: <code>/give_sub ID ДНИ</code>")
        return
    try:
        target_id, days = int(parts[1]), int(parts[2])
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("ID и дни должны быть положительными числами.")
        return

    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (target_id,), fetchone=True, commit=False,
    )
    if not user:
        await message.answer("Пользователь не найден в базе данных.")
        return

    now = datetime.now(timezone.utc)
    base = now
    if user["paid_until"]:
        try:
            paid_until = datetime.fromisoformat(user["paid_until"])
            if paid_until.tzinfo is None:
                paid_until = paid_until.replace(tzinfo=timezone.utc)
            if paid_until > now:
                base = paid_until
        except Exception:
            pass

    until = base + timedelta(days=days)
    until_iso = until.isoformat()

    await db_execute(
        "UPDATE users SET paid_until=? WHERE user_id=?",
        (until_iso, target_id),
    )
    await message.answer(
        f"✅ Выдано <b>{days}</b> дн. пользователю <code>{target_id}</code> до <b>{format_msk(until_iso)}</b>."
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 <b>Вам активирован доступ к просмотру авторов!</b>\n\nСрок: до <b>{format_msk(until_iso)}</b>.",
            reply_markup=main_keyboard(),
        )
    except Exception:
        pass


# ================= ЗАПУСК =================

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
