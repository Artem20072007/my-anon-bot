# -*- coding: utf-8 -*-
import asyncio
import html
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
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
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.getenv(
    "DB_FILE",
    os.path.join(DATA_DIR, "new_bot.sqlite3")
)

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/").strip()

if not BOT_TOKEN:
    raise RuntimeError("В файле .env не указан BOT_TOKEN")
if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    raise RuntimeError("В .env не указаны YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class UserStates(StatesGroup):
    waiting_question = State()
    waiting_broadcast = State()


PLANS = {
    "1d": {"title": "1 день", "days": 1, "rubles": 99},
    "7d": {"title": "7 дней", "days": 7, "rubles": 299},
    "30d": {"title": "30 дней", "days": 30, "rubles": 799},
}


async def db_execute(sql, params=(), fetchone=False, fetchall=False, commit=True):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        result = None
        if fetchone:
            result = await cursor.fetchone()
        elif fetchall:
            result = await cursor.fetchall()
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

    columns = await db_execute("PRAGMA table_info(payments)", fetchall=True, commit=False)
    names = {row["name"] for row in columns}
    if "status" not in names:
        await db_execute(
            "ALTER TABLE payments ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )


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


async def user_is_paid(user_id):
    row = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (user_id,), fetchone=True, commit=False,
    )
    if not row or not row["paid_until"]:
        return False
    return datetime.fromisoformat(row["paid_until"]) > datetime.now(timezone.utc)


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


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
        [InlineKeyboardButton(text="📊 Статистика профиля", callback_data="profile_stats")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="my_blocked")],
        [InlineKeyboardButton(text="💎 Цены на просмотр", callback_data="prices")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def message_keyboard(message_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨", callback_data=f"who:{message_id}")],
        [InlineKeyboardButton(text="Заблокировать", callback_data=f"block:{message_id}")],
    ])


def blocked_user_keyboard(sender_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"unblock:{sender_id}")]
    ])


def prices_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 99 ₽", callback_data="buy:1d")],
        [InlineKeyboardButton(text="7 дней — 299 ₽", callback_data="buy:7d")],
        [InlineKeyboardButton(text="30 дней — 799 ₽", callback_data="buy:30d")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💎 Управлять подписками", callback_data="admin_sub_info")],
    ])


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
            await message.answer("🙂 Это твоя собственная ссылка.")
            return
        if await is_blocked(receiver_id, message.from_user.id):
            await message.answer("🚫 Ты заблокирован у этого пользователя.")
            return

        await db_execute(
            "INSERT INTO link_visits(receiver_id, visitor_id, created_at) VALUES (?, ?, ?)",
            (receiver_id, message.from_user.id, datetime.now(timezone.utc).isoformat()),
        )
        await state.set_state(UserStates.waiting_question)
        await state.update_data(receiver_id=receiver_id)
        await message.answer(
            "✍️ Напиши анонимное сообщение одним сообщением.\n\n"
            "Получатель не увидит твой профиль."
        )
        return

    me = await bot.get_me()
    code = await get_or_create_link_code(message.from_user.id)
    link = f"https://t.me/{me.username}?start={code}"
    await message.answer(
        "👀 <b>Начни получать анонимные вопросы прямо сейчас</b> 👀\n\n"
        f"🔗 Ссылка для получения анонимных вопросов:\n\n<code>{link}</code>\n\n"
        "Размести ссылку в описании профиля, чтобы тебе могли написать.",
        reply_markup=main_keyboard(),
    )


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

    today_count = today_messages["n"]
    total_count = all_messages["n"]
    today_level = min(10, today_count // 10)
    total_level = min(10, total_count // 10)
    me = await bot.get_me()
    code = await get_or_create_link_code(user_id)
    link = f"https://t.me/{me.username}?start={code}"
    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (user_id,), fetchone=True, commit=False,
    )

    paid_until = None
    if user and user["paid_until"]:
        parsed = datetime.fromisoformat(user["paid_until"])
        if parsed > datetime.now(timezone.utc):
            paid_until = parsed.strftime("%d.%m.%Y %H:%M UTC")

    subscription = f"✅ до {paid_until}" if paid_until else "❌ нет"
    return (
        "📊 <b>Статистика профиля</b>\n\n"
        "📤 <b>Сегодня:</b>\n"
        f"💬 Сообщений: {today_count}\n"
        f"👀 Переходов по ссылке: {today_visits['n']}\n"
        f"👑 Популярность: {today_level}/10\n\n"
        "📤 <b>За всё время:</b>\n"
        f"💬 Сообщений: {total_count}\n"
        f"👀 Переходов по ссылке: {all_visits['n']}\n"
        f"👑 Популярность: {total_level}/10\n\n"
        f"💎 Доступ к просмотру автора: <b>{subscription}</b>\n\n"
        "Чтобы повысить популярность, распространяй персональную ссылку:\n"
        f"👉 <code>{link}</code>\n\n"
        "<i>Уровень повышается на 1 за каждые 10 полученных сообщений, максимум — 10.</i>"
    )


@dp.callback_query(F.data == "profile_stats")
async def profile_stats_cb(call: CallbackQuery):
    await call.message.answer(await build_profile_stats(call.from_user.id))
    await call.answer()


@dp.message(Command("stats"))
async def stats_command(message: Message):
    if message.chat.type == "private":
        await message.answer(await build_profile_stats(message.from_user.id))


@dp.message(Command("help"))
async def help_command(message: Message):
    if message.chat.type == "private":
        await message.answer(
            "❓ <b>Помощь</b>\n\n"
            "🔗 Моя ссылка — получить персональную ссылку.\n"
            "📊 Статистика — сообщения, переходы и подписка.\n"
            "🚫 Заблокированные — управление блокировками.\n"
            "💎 Цены на просмотр — покупка доступа через ЮKassa.\n\n"
            "Открой персональную ссылку пользователя и отправь сообщение или медиа."
        )


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
        await message.answer("🚫 Ты заблокирован у этого пользователя.")
        return

    count = await db_execute(
        "SELECT COUNT(*) AS count FROM messages WHERE sender_id=? AND receiver_id=? AND created_at>?",
        (sender_id, receiver_id,
         (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),
        fetchone=True, commit=False,
    )
    if count["count"] >= RATE_LIMIT:
        await state.clear()
        await message.answer("⚠️ Лимит сообщений исчерпан. Попробуй позже.")
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
    await message.answer("✅ Сообщение отправлено анонимно!")

    try:
        header = "💬 <b>У тебя новое анонимное сообщение!</b>\n\n"
        footer = "\n\n↩️ <i>Свайпни для ответа.</i>"
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
        await message.answer("⚠️ Не удалось доставить сообщение получателю.")


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
        await bot.send_message(row["sender_id"], "💬 <b>Тебе ответили анонимно:</b>")
        await bot.copy_message(row["sender_id"], message.chat.id, message.message_id)
        await db_execute("UPDATE messages SET replied=1 WHERE id=?", (row["id"],))
        await message.answer("✅ Ответ отправлен автору анонимно.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить ответ.")


@dp.callback_query(F.data == "my_link")
async def my_link(call: CallbackQuery):
    me = await bot.get_me()
    code = await get_or_create_link_code(call.from_user.id)
    link = f"https://t.me/{me.username}?start={code}"
    await call.message.answer(f"🔗 Твоя ссылка:\n\n<code>{link}</code>")
    await call.answer()


@dp.callback_query(F.data == "my_blocked")
async def my_blocked(call: CallbackQuery):
    blocked = await get_my_blocked_users(call.from_user.id)
    if not blocked:
        await call.message.answer(
            "✅ У тебя нет заблокированных пользователей.",
            reply_markup=main_keyboard(),
        )
        await call.answer()
        return

    await call.message.answer(
        f"🚫 <b>Заблокировано пользователей: {len(blocked)}</b>\n"
        "Нажми кнопку, чтобы разблокировать:"
    )
    for user in blocked:
        name = html.escape(user["first_name"] or "Без имени")
        username = (
            f"@{html.escape(user['username'])}"
            if user["username"] else "не указан"
        )
        await call.message.answer(
            f"👤 <b>{name}</b>\n🔗 Username: {username}",
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
        await call.answer("Этот пользователь уже разблокирован.", show_alert=True)
        return
    await unblock_user(call.from_user.id, sender_id)
    await call.message.edit_text(f"✅ Пользователь <code>{sender_id}</code> разблокирован.")
    await call.answer("Разблокирован")


@dp.callback_query(F.data == "prices")
async def prices(call: CallbackQuery):
    await call.message.answer(
        "💎 <b>Цены на просмотр автора</b>\n"
        "Оплата выполняется через ЮKassa.",
        reply_markup=prices_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery):
    await call.message.answer("🏠 Главное меню", reply_markup=main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_button(call: CallbackQuery):
    await call.message.answer(
        "❓ <b>Как пользоваться</b>\n\n"
        "1. Нажми «Моя ссылка».\n"
        "2. Поделись ею.\n"
        "3. Получай анонимные сообщения.\n"
        "4. Нажми ✨ под сообщением и выбери тариф.\n"
        "5. Оплати через ЮKassa.\n"
        "6. После оплаты автор сообщения откроется автоматически."
    )
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
        await call.answer("Недоступно.", show_alert=True)
        return
    await db_execute(
        "INSERT OR IGNORE INTO blocks(receiver_id, sender_id) VALUES (?, ?)",
        (row["receiver_id"], row["sender_id"]),
    )
    await call.message.answer("🚫 Автор заблокирован. Управлять блокировками можно в меню.")
    await call.answer()


@dp.callback_query(F.data.startswith("who:"))
async def who_author(call: CallbackQuery):
    try:
        message_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный запрос.", show_alert=True)
        return
    row = await get_message(message_id)
    if not row or row["receiver_id"] != call.from_user.id:
        await call.answer("Недоступно.", show_alert=True)
        return
    if not await user_is_paid(call.from_user.id):
        await call.answer("Сначала купи доступ к просмотру.", show_alert=True)
        return

    user = await db_execute(
        "SELECT * FROM users WHERE user_id=?",
        (row["sender_id"],), fetchone=True, commit=False,
    )
    username = (
        f"@{html.escape(user['username'])}"
        if user and user["username"] else "не указан"
    )
    await call.message.answer(
        "👤 <b>Автор сообщения</b>\n"
        f"ID: <code>{row['sender_id']}</code>\n"
        f"Username: {username}"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    plan = PLANS.get(key)

    if not plan:
        await call.answer("Тариф не найден.", show_alert=True)
        return

    try:
        payment = await asyncio.to_thread(
            Payment.create,
            {
                "amount": {
                    "value": f"{plan['rubles']:.2f}",
                    "currency": "RUB",
                },
                "capture": True,
                "confirmation": {
                    "type": "redirect",
                    "return_url": RETURN_URL,
                },
                "description": f"Доступ к просмотру авторов — {plan['title']}",
                "metadata": {
                    "user_id": str(call.from_user.id),
                    "plan": key,
                },
            },
            str(uuid.uuid4()),
        )

        await db_execute(
            """
            INSERT OR IGNORE INTO payments
            (telegram_charge_id, user_id, plan, created_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (
                payment.id,
                call.from_user.id,
                key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💳 Оплатить",
                        url=payment.confirmation.confirmation_url,
                    )
                ]
            ]
        )

        await call.message.answer(
            f"💎 Тариф: <b>{plan['title']}</b>\n"
            f"Стоимость: <b>{plan['rubles']} ₽</b>\n\n"
            "После оплаты доступ активируется автоматически.",
            reply_markup=keyboard,
        )
        await call.answer()

    except Exception as error:
        print(f"ОШИБКА ЮKASSA: {type(error).__name__}: {error!r}")
        await call.answer(
            "Не удалось создать платёж. Проверьте логи.",
            show_alert=True,
        )


async def activate_payment(payment_id: str):

    row = await db_execute(
        "SELECT * FROM payments WHERE telegram_charge_id=?",
        (payment_id,), fetchone=True, commit=False,
    )
    if not row or row["status"] == "succeeded":
        return False

    plan = PLANS.get(row["plan"])
    if not plan:
        return False

    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (row["user_id"],), fetchone=True, commit=False,
    )
    now = datetime.now(timezone.utc)
    base = now

    if user and user["paid_until"]:
        old_until = datetime.fromisoformat(user["paid_until"])
        if old_until > now:
            base = old_until

    until = base + timedelta(days=plan["days"])
    await db_execute(
        "UPDATE users SET paid_until=? WHERE user_id=?",
        (until.isoformat(), row["user_id"]),
    )
    await db_execute(
        "UPDATE payments SET status='succeeded' WHERE telegram_charge_id=?",
        (payment_id,),
    )

    try:
        await bot.send_message(
            row["user_id"],
            "✅ Оплата получена!\n"
            "Доступ к просмотру авторов активирован до:\n"
            f"<b>{until:%d.%m.%Y %H:%M UTC}</b>",
        )
    except Exception as error:
        print("Не удалось отправить уведомление об оплате:", error)

    return True


async def yookassa_webhook(request: web.Request):
    try:
        data = await request.json()
        if data.get("event") != "payment.succeeded":
            return web.json_response({"ok": True})

        payment_object = data.get("object", {})
        payment_id = payment_object.get("id")
        if not payment_id:
            return web.json_response({"ok": True})

        payment = await asyncio.to_thread(Payment.find_one, payment_id)
        if payment.status != "succeeded":
            return web.json_response({"ok": True})

        await activate_payment(payment_id)
        return web.json_response({"ok": True})
    except Exception as error:
        print("Ошибка webhook ЮKassa:", error)
        return web.json_response({"ok": False}, status=500)


@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔️ Нет доступа.")
        return
    users = await db_execute("SELECT COUNT(*) AS n FROM users", fetchone=True, commit=False)
    messages = await db_execute("SELECT COUNT(*) AS n FROM messages", fetchone=True, commit=False)
    await message.answer(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: {users['n']}\n"
        f"💬 Сообщений: {messages['n']}",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔️ Нет доступа.", show_alert=True)
        return
    await state.set_state(UserStates.waiting_broadcast)
    await call.message.answer("📢 Отправь сообщение для рассылки. Для отмены введи /cancel.")
    await call.answer()


@dp.message(UserStates.waiting_broadcast, Command("cancel"))
async def cancel_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.")


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
        await asyncio.sleep(0.05)
    await status_msg.edit_text(
        f"✅ Рассылка завершена. Доставлено: {sent}; не доставлено: {failed}."
    )


@dp.callback_query(F.data == "admin_sub_info")
async def admin_sub_info(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔️ Нет доступа.", show_alert=True)
        return
    await call.message.answer(
        "Выдать подписку: <code>/give_sub ID ДНИ</code>\n"
        "Например: <code>/give_sub 123456789 30</code>"
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
        await message.answer("ID должен быть числом, дни — положительным целым числом.")
        return

    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (target_id,), fetchone=True, commit=False,
    )
    if not user:
        await message.answer("Пользователь не найден: он должен хотя бы раз запустить бота.")
        return

    now = datetime.now(timezone.utc)
    base = now
    if user["paid_until"]:
        paid_until = datetime.fromisoformat(user["paid_until"])
        if paid_until > now:
            base = paid_until
    until = base + timedelta(days=days)
    await db_execute(
        "UPDATE users SET paid_until=? WHERE user_id=?",
        (until.isoformat(), target_id),
    )
    await message.answer(
        f"✅ Выдано {days} дн. доступа пользователю <code>{target_id}</code>, "
        f"до <b>{until:%d.%m.%Y %H:%M UTC}</b>."
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 Вам выдан доступ к просмотру автора до "
            f"<b>{until:%d.%m.%Y %H:%M UTC}</b>.",
        )
    except Exception:
        pass


async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="stats", description="Статистика и подписка"),
        BotCommand(command="help", description="Помощь"),
    ])

    app = web.Application()
    app.router.add_post("/yookassa/webhook", yookassa_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()

    print(f"Бот запущен, webhook слушает порт {WEBHOOK_PORT}")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
