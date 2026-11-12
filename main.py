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
DB_FILE = os.getenv("DB_FILE", "bot.sqlite3")
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/").strip()

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Не указана переменная BOT_TOKEN")
if not SHOP_ID or not SECRET_KEY:
    raise RuntimeError("Не указаны YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")

Configuration.account_id = SHOP_ID
Configuration.secret_key = SECRET_KEY

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
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
        CREATE TABLE IF NOT EXISTS payments (
            telegram_charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)

    columns = await db_execute(
        "PRAGMA table_info(messages)", fetchall=True, commit=False
    )
    if "recipient_message_id" not in {row["name"] for row in columns}:
        await db_execute(
            "ALTER TABLE messages ADD COLUMN recipient_message_id INTEGER"
        )

    columns = await db_execute(
        "PRAGMA table_info(payments)", fetchall=True, commit=False
    )
    if "status" not in {row["name"] for row in columns}:
        await db_execute(
            "ALTER TABLE payments ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )


async def save_user(user):
    await db_execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        datetime.now(timezone.utc).isoformat(),
    ))


async def get_link_code(user_id):
    row = await db_execute(
        "SELECT code FROM personal_links WHERE user_id=?",
        (user_id,), fetchone=True, commit=False
    )
    if row:
        return row["code"]

    while True:
        code = secrets.token_urlsafe(8)
        exists = await db_execute(
            "SELECT 1 FROM personal_links WHERE code=?",
            (code,), fetchone=True, commit=False
        )
        if not exists:
            break

    await db_execute(
        "INSERT INTO personal_links(code, user_id, created_at) VALUES (?, ?, ?)",
        (code, user_id, datetime.now(timezone.utc).isoformat()),
    )
    return code


async def user_by_code(code):
    row = await db_execute(
        "SELECT user_id FROM personal_links WHERE code=?",
        (code,), fetchone=True, commit=False
    )
    return row["user_id"] if row else None


async def message_by_id(message_id):
    return await db_execute(
        "SELECT * FROM messages WHERE id=?",
        (message_id,), fetchone=True, commit=False
    )


async def is_blocked(receiver_id, sender_id):
    row = await db_execute(
        "SELECT 1 FROM blocks WHERE receiver_id=? AND sender_id=?",
        (receiver_id, sender_id), fetchone=True, commit=False
    )
    return row is not None


async def is_paid(user_id):
    row = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (user_id,), fetchone=True, commit=False
    )
    if not row or not row["paid_until"]:
        return False
    return datetime.fromisoformat(row["paid_until"]) > datetime.now(timezone.utc)


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="my_link")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="💎 Цены на просмотр", callback_data="prices")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def message_keyboard(message_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨", callback_data=f"who:{message_id}")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block:{message_id}")],
    ])


def prices_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 99 ₽", callback_data="buy:1d")],
        [InlineKeyboardButton(text="7 дней — 299 ₽", callback_data="buy:7d")],
        [InlineKeyboardButton(text="30 дней — 799 ₽", callback_data="buy:30d")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
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
        receiver_id = await user_by_code(payload)
        if receiver_id is None:
            await message.answer("❌ Ссылка недействительна.")
            return
        if receiver_id == message.from_user.id:
            await message.answer("🙂 Это твоя собственная ссылка.")
            return
        if await is_blocked(receiver_id, message.from_user.id):
            await message.answer("🚫 Ты заблокирован у этого пользователя.")
            return

        await db_execute(
            "INSERT INTO link_visits(receiver_id, visitor_id, created_at) VALUES (?, ?, ?)",
            (receiver_id, message.from_user.id,
             datetime.now(timezone.utc).isoformat()),
        )
        await state.set_state(UserStates.waiting_question)
        await state.update_data(receiver_id=receiver_id)
        await message.answer(
            "✍️ Напиши анонимное сообщение одним сообщением.\n\n"
            "Получатель не увидит твой профиль."
        )
        return

    me = await bot.get_me()
    code = await get_link_code(message.from_user.id)
    link = f"https://t.me/{me.username}?start={code}"
    await message.answer(
        "👀 <b>Начни получать анонимные вопросы прямо сейчас</b> 👀\n\n"
        f"🔗 Твоя ссылка:\n\n<code>{link}</code>\n\n"
        "Размести ссылку в описании профиля.",
        reply_markup=main_keyboard(),
    )


@dp.message(UserStates.waiting_question)
async def receive_question(message: Message, state: FSMContext):
    data = await state.get_data()
    receiver_id = data.get("receiver_id")
    sender_id = message.from_user.id

    if not receiver_id:
        await state.clear()
        await message.answer("⚠️ Открой ссылку заново.")
        return
    if await is_blocked(receiver_id, sender_id):
        await state.clear()
        await message.answer("🚫 Ты заблокирован у этого пользователя.")
        return

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    count = await db_execute(
        "SELECT COUNT(*) AS n FROM messages WHERE sender_id=? AND receiver_id=? AND created_at>?",
        (sender_id, receiver_id, since), fetchone=True, commit=False
    )
    if count["n"] >= RATE_LIMIT:
        await state.clear()
        await message.answer("⚠️ Лимит сообщений исчерпан. Попробуй позже.")
        return

    row = await db_execute("""
        INSERT INTO messages
        (sender_id, receiver_id, text, telegram_message_id, created_at)
        VALUES (?, ?, ?, ?, ?) RETURNING id
    """, (
        sender_id,
        receiver_id,
        message.text or message.caption or "",
        message.message_id,
        datetime.now(timezone.utc).isoformat(),
    ), fetchone=True)
    message_id = row["id"]
    await state.clear()
    await message.answer("✅ Сообщение отправлено анонимно!")

    try:
        header = "💬 <b>Новое анонимное сообщение!</b>\n\n"
        footer = "\n\n↩️ <i>Свайпни для ответа.</i>"
        if message.content_type == "text":
            body = f"<blockquote>{html.escape(message.text or '')}</blockquote>"
            sent = await bot.send_message(
                receiver_id, header + body + footer,
                reply_markup=message_keyboard(message_id),
            )
        else:
            caption = html.escape(message.caption or "")
            body = f"<blockquote>{caption}</blockquote>" if caption else ""
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
        print("Ошибка доставки:", repr(error))


@dp.message(F.reply_to_message)
async def reply_to_anonymous(message: Message):
    if message.chat.type != "private":
        return
    row = await db_execute(
        "SELECT * FROM messages WHERE receiver_id=? AND recipient_message_id=?",
        (message.from_user.id, message.reply_to_message.message_id),
        fetchone=True, commit=False
    )
    if not row:
        return
    try:
        await bot.send_message(row["sender_id"], "💬 <b>Тебе ответили анонимно:</b>")
        await bot.copy_message(row["sender_id"], message.chat.id, message.message_id)
        await message.answer("✅ Ответ отправлен анонимно.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить ответ.")


@dp.callback_query(F.data == "my_link")
async def my_link(call: CallbackQuery):
    me = await bot.get_me()
    code = await get_link_code(call.from_user.id)
    await call.message.answer(
        f"🔗 Твоя ссылка:\n\n<code>https://t.me/{me.username}?start={code}</code>"
    )
    await call.answer()


@dp.callback_query(F.data.in_({"stats", "profile_stats"}))
async def stats(call: CallbackQuery):
    row = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (call.from_user.id,), fetchone=True, commit=False
    )
    paid = "❌ нет"
    if row and row["paid_until"]:
        end = datetime.fromisoformat(row["paid_until"])
        if end > datetime.now(timezone.utc):
            paid = f"✅ до {end:%d.%m.%Y %H:%M UTC}"
    count = await db_execute(
        "SELECT COUNT(*) AS n FROM messages WHERE receiver_id=?",
        (call.from_user.id,), fetchone=True, commit=False
    )
    await call.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"💬 Получено сообщений: {count['n']}\n"
        f"💎 Доступ к авторам: <b>{paid}</b>"
    )
    await call.answer()


@dp.callback_query(F.data == "prices")
async def prices(call: CallbackQuery):
    await call.message.answer(
        "💎 <b>Доступ к просмотру авторов</b>\n"
        "Оплата через ЮKassa:",
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
        "❓ <b>Помощь</b>\n\n"
        "Поделись своей ссылкой, получай анонимные сообщения, "
        "нажимай ✨ и покупай доступ к автору сообщения."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("block:"))
async def block_author(call: CallbackQuery):
    try:
        message_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Ошибка.", show_alert=True)
        return
    row = await message_by_id(message_id)
    if not row or row["receiver_id"] != call.from_user.id:
        await call.answer("Недоступно.", show_alert=True)
        return
    await db_execute(
        "INSERT OR IGNORE INTO blocks(receiver_id, sender_id) VALUES (?, ?)",
        (row["receiver_id"], row["sender_id"]),
    )
    await call.message.answer("🚫 Автор заблокирован.")
    await call.answer()


@dp.callback_query(F.data.startswith("who:"))
async def who_author(call: CallbackQuery):
    try:
        message_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Ошибка.", show_alert=True)
        return
    row = await message_by_id(message_id)
    if not row or row["receiver_id"] != call.from_user.id:
        await call.answer("Недоступно.", show_alert=True)
        return
    if not await is_paid(call.from_user.id):
        await call.answer("Сначала купи доступ.", show_alert=True)
        return

    user = await db_execute(
        "SELECT username FROM users WHERE user_id=?",
        (row["sender_id"],), fetchone=True, commit=False
    )
    username = "не указан"
    if user and user["username"]:
        username = "@" + html.escape(user["username"])
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
                "description": f"Доступ к авторам — {plan['title']}",
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

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💳 Оплатить",
                url=payment.confirmation.confirmation_url,
            )]
        ])
        await call.message.answer(
            f"💎 Тариф: <b>{plan['title']}</b>\n"
            f"Стоимость: <b>{plan['rubles']} ₽</b>\n\n"
            "После оплаты доступ включится автоматически.",
            reply_markup=keyboard,
        )
        await call.answer()
    except Exception as error:
        print("Ошибка создания платежа ЮKassa:", repr(error))
        await call.answer("Не удалось создать платёж.", show_alert=True)


async def activate_payment(payment_id):
    row = await db_execute(
        "SELECT * FROM payments WHERE telegram_charge_id=?",
        (payment_id,), fetchone=True, commit=False
    )
    if not row or row["status"] == "succeeded":
        return

    plan = PLANS.get(row["plan"])
    if not plan:
        return

    user = await db_execute(
        "SELECT paid_until FROM users WHERE user_id=?",
        (row["user_id"],), fetchone=True, commit=False
    )
    now = datetime.now(timezone.utc)
    start_date = now
    if user and user["paid_until"]:
        old_end = datetime.fromisoformat(user["paid_until"])
        if old_end > now:
            start_date = old_end

    end_date = start_date + timedelta(days=plan["days"])
    await db_execute(
        "UPDATE users SET paid_until=? WHERE user_id=?",
        (end_date.isoformat(), row["user_id"]),
    )
    await db_execute(
        "UPDATE payments SET status='succeeded' WHERE telegram_charge_id=?",
        (payment_id,),
    )
    try:
        await bot.send_message(
            row["user_id"],
            "✅ Оплата получена!\n"
            f"Доступ активен до <b>{end_date:%d.%m.%Y %H:%M UTC}</b>.",
        )
    except Exception as error:
        print("Ошибка уведомления об оплате:", repr(error))


async def yookassa_webhook(request: web.Request):
    try:
        data = await request.json()
        if data.get("event") != "payment.succeeded":
            return web.json_response({"ok": True})

        payment_id = data.get("object", {}).get("id")
        if not payment_id:
            return web.json_response({"ok": True})

        payment = await asyncio.to_thread(Payment.find_one, payment_id)
        if payment.status == "succeeded":
            await activate_payment(payment_id)
        return web.json_response({"ok": True})
    except Exception as error:
        print("Ошибка webhook ЮKassa:", repr(error))
        return web.json_response({"ok": False}, status=500)


@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет доступа.")
        return
    users = await db_execute(
        "SELECT COUNT(*) AS n FROM users", fetchone=True, commit=False
    )
    await message.answer(f"🛠 Пользователей: {users['n']}")


async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="stats", description="Статистика"),
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

