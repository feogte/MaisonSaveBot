
import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "business_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("business-archive")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

FAQ_DEFAULT = "ℹ️ FAQ"
MAX_SPAM = 100



# This bot is multi-tenant:
# every Telegram Business account that connects the bot gets its own
# business_connection_id and independent chat settings/messages.
# OWNER_ID is only for the bot operator's global /admin panel.

# ---------------- DATABASE ----------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS owners (
        business_connection_id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        user_chat_id INTEGER,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS messages (
        business_connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        owner_user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        content_type TEXT,
        text TEXT,
        caption TEXT,
        reply_to_message_id INTEGER,
        sender_user_id INTEGER,
        sender_username TEXT,
        sender_first_name TEXT,
        sender_last_name TEXT,
        PRIMARY KEY (business_connection_id, chat_id, message_id)
    );

    CREATE TABLE IF NOT EXISTS chat_settings (
        business_connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        muted INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0,
        fast_seconds INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (business_connection_id, chat_id)
    );

    CREATE TABLE IF NOT EXISTS notification_map (
        owner_user_id INTEGER NOT NULL,
        notification_message_id INTEGER NOT NULL,
        business_connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        original_message_id INTEGER,
        sender_user_id INTEGER,
        PRIMARY KEY (owner_user_id, notification_message_id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    # Upgrade databases created by earlier versions.
    owner_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(owners)").fetchall()
    }
    if "user_chat_id" not in owner_columns:
        conn.execute("ALTER TABLE owners ADD COLUMN user_chat_id INTEGER")

    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
        ("faq_button_text", FAQ_DEFAULT),
    )
    conn.commit()
    conn.close()


def touch_user(user_id: int):
    conn = db()
    t = now()
    conn.execute("""
        INSERT INTO users(user_id, first_seen, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
    """, (user_id, t, t))
    conn.commit()
    conn.close()



def get_user_count() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    conn.close()
    return int(row["c"]) if row else 0




def get_active_connections_count() -> int:
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM owners WHERE is_enabled=1"
    ).fetchone()
    conn.close()
    return int(row["c"]) if row else 0



def save_connection(connection: BusinessConnection):
    conn = db()
    conn.execute("""
        INSERT INTO owners(
            business_connection_id, owner_user_id, user_chat_id,
            is_enabled, updated_at
        )
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(business_connection_id) DO UPDATE SET
            owner_user_id=excluded.owner_user_id,
            user_chat_id=excluded.user_chat_id,
            is_enabled=1,
            updated_at=excluded.updated_at
    """, (
        connection.id,
        connection.user.id,
        connection.user_chat_id,
        now(),
    ))
    conn.commit()
    conn.close()


def disable_connection(connection_id: str):
    conn = db()
    conn.execute(
        "UPDATE owners SET is_enabled=0, updated_at=? "
        "WHERE business_connection_id=?",
        (now(), connection_id),
    )
    conn.commit()
    conn.close()


def get_owner(bc_id: str):
    conn = db()
    row = conn.execute(
        "SELECT * FROM owners "
        "WHERE business_connection_id=? AND is_enabled=1",
        (bc_id,),
    ).fetchone()
    conn.close()
    return row


def save_message(message: Message):
    bc_id = message.business_connection_id
    if not bc_id or not message.chat:
        return

    owner = get_owner(bc_id)
    if not owner:
        return

    sender = message.from_user

    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO messages(
            business_connection_id, chat_id, message_id, owner_user_id,
            created_at, content_type, text, caption, reply_to_message_id,
            sender_user_id, sender_username, sender_first_name, sender_last_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        bc_id,
        message.chat.id,
        message.message_id,
        owner["owner_user_id"],
        now(),
        message.content_type.value if message.content_type else None,
        message.text,
        message.caption,
        message.reply_to_message.message_id if message.reply_to_message else None,
        sender.id if sender else None,
        sender.username if sender else None,
        sender.first_name if sender else None,
        sender.last_name if sender else None,
    ))
    conn.commit()
    conn.close()


def get_saved_message(bc_id: str, chat_id: int, message_id: int):
    conn = db()
    row = conn.execute("""
        SELECT * FROM messages
        WHERE business_connection_id=? AND chat_id=? AND message_id=?
    """, (bc_id, chat_id, message_id)).fetchone()
    conn.close()
    return row


def delete_saved_message(bc_id: str, chat_id: int, message_id: int):
    conn = db()
    conn.execute("""
        DELETE FROM messages
        WHERE business_connection_id=? AND chat_id=? AND message_id=?
    """, (bc_id, chat_id, message_id))
    conn.commit()
    conn.close()


def set_chat_setting(bc_id: str, chat_id: int, **values):
    conn = db()
    current = conn.execute("""
        SELECT * FROM chat_settings
        WHERE business_connection_id=? AND chat_id=?
    """, (bc_id, chat_id)).fetchone()

    if current:
        parts = []
        params = []
        for key, value in values.items():
            parts.append(f"{key}=?")
            params.append(value)
        params.extend([bc_id, chat_id])
        conn.execute(
            f"UPDATE chat_settings SET {', '.join(parts)} "
            "WHERE business_connection_id=? AND chat_id=?",
            params,
        )
    else:
        muted = int(values.get("muted", 0))
        paused = int(values.get("paused", 0))
        fast_seconds = int(values.get("fast_seconds", 0))
        conn.execute("""
            INSERT INTO chat_settings(
                business_connection_id, chat_id, muted, paused, fast_seconds
            )
            VALUES (?, ?, ?, ?, ?)
        """, (bc_id, chat_id, muted, paused, fast_seconds))

    conn.commit()
    conn.close()


def get_chat_setting(bc_id: str, chat_id: int):
    conn = db()
    row = conn.execute("""
        SELECT * FROM chat_settings
        WHERE business_connection_id=? AND chat_id=?
    """, (bc_id, chat_id)).fetchone()
    conn.close()
    return row


def map_notification(
    owner_id: int,
    notification_id: int,
    bc_id: str,
    chat_id: int,
    original_message_id: int | None,
    sender_user_id: int | None,
):
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO notification_map(
            owner_user_id, notification_message_id,
            business_connection_id, chat_id,
            original_message_id, sender_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        owner_id, notification_id, bc_id, chat_id,
        original_message_id, sender_user_id,
    ))
    conn.commit()
    conn.close()


def resolve_target(owner_id: int, reply_message_id: int | None):
    if not reply_message_id:
        return None

    conn = db()
    row = conn.execute("""
        SELECT * FROM notification_map
        WHERE owner_user_id=? AND notification_message_id=?
    """, (owner_id, reply_message_id)).fetchone()
    conn.close()
    return row


def get_setting(key: str, default: str = ""):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = db()
    conn.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


# ---------------- UI ----------------

def faq_button_text():
    return get_setting("faq_button_text", FAQ_DEFAULT)


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=faq_button_text(),
            callback_data="faq_main"
        )]
    ])


def faq_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=".spam", callback_data="faq_spam"),
            InlineKeyboardButton(text=".mute", callback_data="faq_mute"),
        ],
        [
            InlineKeyboardButton(text=".unmute", callback_data="faq_unmute"),
            InlineKeyboardButton(text=".info", callback_data="faq_info"),
        ],
        [
            InlineKeyboardButton(text=".fast", callback_data="faq_fast"),
            InlineKeyboardButton(text=".pause", callback_data="faq_pause"),
        ],
        [
            InlineKeyboardButton(text=".resume", callback_data="faq_resume"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="faq_back"),
        ],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔗 Business-подключения",
            callback_data="admin_connections"
        )],
        [InlineKeyboardButton(
            text="✏️ Изменить текст кнопки",
            callback_data="admin_faq_text"
        )],
    ])


def admin_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="admin_back"
        )]
    ])


def quote(text: str | None):
    if not text:
        return "<blockquote>(без текста)</blockquote>"
    return "<blockquote>" + html.escape(text) + "</blockquote>"


FAQ_TEXT = {
    "faq_spam": (
        "<b>.spam X</b>\n\n"
        "Отправляет пользователю X сообщений от Business-аккаунта.\n"
        "Максимум — <b>100</b> сообщений за одну команду.\n\n"
        "Пример: <code>.spam 20</code>"
    ),
    "faq_mute": (
        "<b>.mute</b>\n\n"
        "Включает вечный мут для выбранного пользователя.\n"
        "Его новые сообщения будут удаляться автоматически.\n\n"
        "Снять мут: <code>.unmute</code>"
    ),
    "faq_unmute": (
        "<b>.unmute</b>\n\n"
        "Снимает вечный мут с выбранного пользователя."
    ),
    "faq_info": (
        "<b>.info</b>\n\n"
        "Показывает ID и username пользователя.\n"
        "Команду нужно отправить ответом на сообщение бота, "
        "относящееся к нужному пользователю."
    ),
    "faq_fast": (
        "<b>.fast X</b>\n\n"
        "Устанавливает время, через которое новые сообщения и фото "
        "выбранного пользователя будут удаляться автоматически.\n\n"
        "Примеры:\n"
        "<code>.fast 10s</code> — через 10 секунд\n"
        "<code>.fast 2m</code> — через 2 минуты\n"
        "<code>.fast 30</code> — через 30 секунд\n\n"
        "Чтобы отключить режим, используй <code>.fast 0</code>."
    ),
    "faq_pause": (
        "<b>.pause</b>\n\n"
        "Полностью приостанавливает автоматическую обработку "
        "выбранного чата: мут и fast для него не применяются."
    ),
    "faq_resume": (
        "<b>.resume</b>\n\n"
        "Возобновляет автоматическую обработку выбранного чата."
    ),
}


@dp.callback_query(F.data == "faq_main")
async def faq_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>ℹ️ FAQ — команды</b>\n\nВыбери команду:",
        reply_markup=faq_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "faq_main")
async def faq_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "<b>ℹ️ FAQ — команды</b>\n\nВыбери команду:",
        reply_markup=faq_keyboard(),
    )


@dp.callback_query(F.data == "faq_back")
async def faq_back(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "Бот готов.\n\nВыбери раздел:",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data.in_({
    "faq_spam",
    "faq_mute",
    "faq_unmute",
    "faq_info",
    "faq_fast",
    "faq_pause",
    "faq_resume",
}))
async def faq_item(callback: CallbackQuery):
    text = FAQ_TEXT.get(callback.data)
    if not text:
        await callback.answer("Не найдено", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⬅️ К командам",
                callback_data="faq_main",
            )]
        ]),
    )


@dp.callback_query(F.data == "admin_connections")
async def admin_connections(callback: CallbackQuery):
    if not OWNER_ID or callback.from_user.id != OWNER_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    conn = db()
    rows = conn.execute("""
        SELECT business_connection_id, owner_user_id, is_enabled, updated_at
        FROM owners
        ORDER BY updated_at DESC
    """).fetchall()
    conn.close()

    if not rows:
        body = "<b>🔗 Business-подключения</b>\n\nНет подключений."
    else:
        parts = ["<b>🔗 Business-подключения</b>\n"]
        for i, row in enumerate(rows, 1):
            status = "🟢 активно" if row["is_enabled"] else "🔴 отключено"
            parts.append(
                f"{i}. {status}\n"
                f"ID: <code>{html.escape(row['business_connection_id'])}</code>\n"
                f"Owner ID: <code>{row['owner_user_id']}</code>\n"
            )
        body = "\n".join(parts)

    await callback.answer()
    await callback.message.edit_text(body, reply_markup=admin_back_keyboard())


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not OWNER_ID or callback.from_user.id != OWNER_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        "<b>🛠 Админ-панель</b>\n\n"
        f"👤 Пользователей бота: <b>{get_user_count()}</b>\n"
        f"🔗 Активных Business-подключений: <b>{get_active_connections_count()}</b>\n\n"
        "Выбери действие:",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_faq_text")
async def admin_faq_text(callback: CallbackQuery):
    if not OWNER_ID or callback.from_user.id != OWNER_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    set_setting("faq_edit_mode", "1")
    await callback.answer("Жду новый текст.")
    await callback.message.edit_text(
        "<b>✏️ Изменение текста кнопки</b>\n\n"
        "Отправь следующим сообщением новый текст кнопки.\n"
        "Максимум 40 символов.\n\n"
        f"Сейчас: <b>{html.escape(faq_button_text())}</b>",
        reply_markup=admin_back_keyboard(),
    )


# ---------------- COMMAND HELPERS ----------------

def parse_command(text: str):
    if not text:
        return None, []

    parts = text.strip().split()
    if not parts:
        return None, []

    command = parts[0].lower()
    if not command.startswith("."):
        return None, []

    return command, parts[1:]


def parse_duration(value: str):
    match = re.fullmatch(
        r"(\d+)(?:\s*)(s|sec|сек|m|min|мин|м)?",
        value.lower(),
    )
    if not match:
        return None

    number = int(match.group(1))
    unit = match.group(2) or "s"

    if unit in {"m", "min", "мин", "м"}:
        return number * 60

    return number


async def delete_business_message(
    bc_id: str,
    chat_id: int,
    message_id: int,
):
    try:
        await bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message_id],
        )
        return True
    except Exception:
        log.exception(
            "Could not delete business message %s in chat %s",
            message_id,
            chat_id,
        )
        return False


async def delete_later(
    bc_id: str,
    chat_id: int,
    message_id: int,
    delay: int,
):
    await asyncio.sleep(delay)
    await delete_business_message(bc_id, chat_id, message_id)


async def spam_business(
    bc_id: str,
    chat_id: int,
    count: int,
    text: str,
):
    for _ in range(count):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                business_connection_id=bc_id,
            )
            await asyncio.sleep(0.08)
        except Exception:
            log.exception("Spam failed")
            break


async def send_business_command_result(
    bc_id: str,
    text: str,
):
    owner = get_owner(bc_id)
    if not owner:
        log.warning("Cannot send command result: no owner for %s", bc_id)
        return

    try:
        await bot.send_message(
            chat_id=owner["user_chat_id"] if "user_chat_id" in owner.keys()
            else owner["owner_user_id"],
            text=text,
        )
    except Exception:
        # Fallback to owner user id for older stored connection rows.
        try:
            await bot.send_message(
                chat_id=owner["owner_user_id"],
                text=text,
            )
        except Exception:
            log.exception("Could not send command result to owner")


def business_target_username(message: Message) -> str:
    username = getattr(message.chat, "username", None)
    return f"@{username}" if username else "отсутствует"


async def handle_business_command(
    message: Message,
    command: str,
    args: list[str],
):
    """
    Commands are written directly in the Business chat with the customer.
    The target is always message.chat.
    """
    bc_id = message.business_connection_id
    if not bc_id or not message.chat:
        return

    owner = get_owner(bc_id)
    if not owner:
        return

    # Business message updates include both incoming and outgoing messages.
    # Accept commands only when they were sent by the Business account owner.
    chat_id = message.chat.id
    username = business_target_username(message)

    if command == ".spam":
        if len(args) < 2 or not args[0].isdigit():
            await send_business_command_result(
                bc_id,
                "Использование: <code>.spam 100 текст</code>",
            )
            return

        count = int(args[0])
        spam_text = " ".join(args[1:]).strip()

        if not 1 <= count <= MAX_SPAM:
            await send_business_command_result(
                bc_id,
                "❌ Количество должно быть от 1 до 100.",
            )
            return

        if not spam_text:
            await send_business_command_result(
                bc_id,
                "❌ Укажи текст: <code>.spam 100 Привет</code>",
            )
            return

        await spam_business(
            bc_id,
            chat_id,
            count,
            spam_text,
        )

        await send_business_command_result(
            bc_id,
            f"✅ Отправлено сообщений: <b>{count}</b> пользователю {html.escape(username)}.",
        )
        return

    if command == ".mute":
        set_chat_setting(
            bc_id,
            chat_id,
            muted=1,
        )

        await send_business_command_result(
            bc_id,
            f"🔇 {html.escape(username)} замьючен навсегда.",
        )
        return

    if command == ".unmute":
        set_chat_setting(
            bc_id,
            chat_id,
            muted=0,
        )

        await send_business_command_result(
            bc_id,
            f"🔊 Мут с {html.escape(username)} снят.",
        )
        return

    if command == ".info":
        await send_business_command_result(
            bc_id,
            "<b>Информация о пользователе</b>\n\n"
            f"ID: <code>{chat_id}</code>\n"
            f"Username: <code>{html.escape(username)}</code>",
        )
        return

    if command == ".fast":
        if not args:
            await send_business_command_result(
                bc_id,
                "Использование: <code>.fast 10s</code> или <code>.fast 2m</code>",
            )
            return

        seconds = parse_duration(args[0])

        if seconds is None or seconds < 0:
            await send_business_command_result(
                bc_id,
                "❌ Укажи корректное время: <code>10s</code> или <code>2m</code>.",
            )
            return

        if seconds > 7 * 24 * 60 * 60:
            await send_business_command_result(
                bc_id,
                "❌ Максимум — 7 дней.",
            )
            return

        set_chat_setting(
            bc_id,
            chat_id,
            fast_seconds=seconds,
        )

        result = (
            "⏱ Fast-режим отключён."
            if seconds == 0
            else f"⏱ Fast-режим для {html.escape(username)}: <b>{seconds} сек.</b>"
        )

        await send_business_command_result(bc_id, result)
        return

    if command == ".pause":
        set_chat_setting(
            bc_id,
            chat_id,
            paused=1,
        )

        await send_business_command_result(
            bc_id,
            f"⏸ Обработка чата с {html.escape(username)} приостановлена.",
        )
        return

    if command == ".resume":
        set_chat_setting(
            bc_id,
            chat_id,
            paused=0,
        )

        await send_business_command_result(
            bc_id,
            f"▶️ Обработка чата с {html.escape(username)} возобновлена.",
        )
        return

    await send_business_command_result(
        bc_id,
        "❓ Неизвестная команда. Открой FAQ в личке бота.",
    )


# ---------------- BUSINESS ----------------



@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    log.info(
        "Business connection: id=%s user=%s enabled=%s rights=%s",
        connection.id,
        connection.user.id,
        connection.is_enabled,
        connection.rights,
    )

    if connection.is_enabled:
        save_connection(connection)
        try:
            await bot.send_message(
                connection.user_chat_id,
                "✅ Business-аккаунт подключён.\n\n"
                "Команды отправляй прямо в Business-чате с пользователем."
            )
        except Exception:
            log.exception("Could not notify business owner")
    else:
        disable_connection(connection.id)


@dp.business_message()
async def on_business_message(message: Message):
    """
    Handles BOTH incoming and outgoing messages from connected Business chats.

    Outgoing owner commands are recognized by the owner user id from the
    BusinessConnection. Incoming customer messages are processed by mute/fast
    and are stored for deletion notifications.
    """
    bc_id = message.business_connection_id
    if not bc_id or not message.chat:
        return

    owner = get_owner(bc_id)
    if not owner:
        log.warning("Business message for unknown connection: %s", bc_id)
        return

    log.info(
        "Business message: chat=%s message=%s text=%r outgoing_by_business_bot=%s",
        message.chat.id,
        message.message_id,
        message.text,
        bool(message.sender_business_bot),
    )

    # The connected Business account's own messages are the command source.
    # Telegram documents business_message updates for incoming AND outgoing
    # messages in connected chats.
    # For outgoing messages sent on behalf of the connected Business account,
    # Bot API exposes sender_business_bot. Do NOT compare from_user to the
    # Business owner's ID: outgoing Business messages are represented with
    # sender_business_bot.
    is_owner_message = (
        message.sender_business_bot is not None
        and message.sender_business_bot.id == bot.id
    )

    if is_owner_message:
        if message.text:
            command, args = parse_command(message.text)

            if command:
                log.info(
                    "Business command %s from owner=%s in chat=%s",
                    command,
                    owner["owner_user_id"],
                    message.chat.id,
                )
                await handle_business_command(
                    message,
                    command,
                    args,
                )

                # Remove the command from the customer chat.
                await delete_business_message(
                    bc_id,
                    message.chat.id,
                    message.message_id,
                )
        return

    # Customer message.
    try:
        save_message(message)
    except Exception:
        log.exception(
            "Could not save business message %s",
            message.message_id,
        )

    settings = get_chat_setting(
        bc_id,
        message.chat.id,
    )

    if settings and settings["paused"]:
        return

    # Permanent mute.
    if settings and settings["muted"]:
        await delete_business_message(
            bc_id,
            message.chat.id,
            message.message_id,
        )
        return

    # Fast deletion.
    if settings and settings["fast_seconds"] > 0:
        asyncio.create_task(
            delete_later(
                bc_id,
                message.chat.id,
                message.message_id,
                settings["fast_seconds"],
            )
        )


@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    try:
        save_message(message)
    except Exception:
        log.exception("Could not update business message %s", message.message_id)


async def send_deleted_notification(
    owner_id: int,
    bc_id: str,
    chat_id: int,
    message_id: int,
    row,
):
    if row and (row["text"] or row["caption"]):
        original = row["text"] or row["caption"]
        username = (
            "@" + row["sender_username"]
            if row["sender_username"]
            else "отсутствует"
        )
        body = (
            "<b>Удаленное сообщение</b>\n"
            + quote(original)
            + f"\nот <code>{html.escape(username)}</code>"
        )
        sent = await bot.send_message(owner_id, body)
        map_notification(
            owner_id,
            sent.message_id,
            bc_id,
            chat_id,
            message_id,
            row["sender_user_id"],
        )
        return

    username = (
        "@" + row["sender_username"]
        if row and row["sender_username"]
        else "отсутствует"
    )
    sent = await bot.send_message(
        owner_id,
        "<b>Удаленное сообщение</b>\n"
        "<blockquote>Текст сообщения не был сохранён.</blockquote>\n"
        f"от <code>{html.escape(username)}</code>",
    )
    map_notification(
        owner_id,
        sent.message_id,
        bc_id,
        chat_id,
        message_id,
        row["sender_user_id"] if row else None,
    )


@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    owner = get_owner(event.business_connection_id)
    if not owner:
        return

    owner_id = owner["owner_user_id"]

    for message_id in event.message_ids:
        row = get_saved_message(
            event.business_connection_id,
            event.chat.id,
            message_id,
        )

        try:
            await send_deleted_notification(
                owner_id,
                event.business_connection_id,
                event.chat.id,
                message_id,
                row,
            )
        except Exception:
            log.exception("Could not send deleted message notification")

        delete_saved_message(
            event.business_connection_id,
            event.chat.id,
            message_id,
        )


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=False)

    log.info("Business archive bot v5 started")

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
