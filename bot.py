import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, timezone

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
DB_PATH = os.getenv("DB_PATH", "business_bot.db")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

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
        PRIMARY KEY (business_connection_id, chat_id, message_id)
    );

    CREATE INDEX IF NOT EXISTS idx_messages_lookup
    ON messages (business_connection_id, chat_id, message_id);
    """)
    conn.commit()
    conn.close()


def touch_user(user_id: int):
    conn = db()
    timestamp = now()
    conn.execute("""
        INSERT INTO users(user_id, first_seen, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
    """, (user_id, timestamp, timestamp))
    conn.commit()
    conn.close()


def get_user_count() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    conn.close()
    return int(row["c"])


def get_saved_count() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
    conn.close()
    return int(row["c"])


def get_active_connections_count() -> int:
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM owners WHERE is_enabled=1"
    ).fetchone()
    conn.close()
    return int(row["c"])


def get_total_connections_count() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM owners").fetchone()
    conn.close()
    return int(row["c"])


def save_connection(connection: BusinessConnection):
    conn = db()
    conn.execute("""
        INSERT INTO owners (
            business_connection_id, owner_user_id, is_enabled, updated_at
        )
        VALUES (?, ?, 1, ?)
        ON CONFLICT(business_connection_id) DO UPDATE SET
            owner_user_id=excluded.owner_user_id,
            is_enabled=1,
            updated_at=excluded.updated_at
    """, (connection.id, connection.user.id, now()))
    conn.commit()
    conn.close()


def disable_connection(connection_id: str):
    conn = db()
    conn.execute(
        "UPDATE owners SET is_enabled=0, updated_at=? WHERE business_connection_id=?",
        (now(), connection_id),
    )
    conn.commit()
    conn.close()


def save_message(message: Message):
    bc_id = message.business_connection_id
    if not bc_id or not message.chat:
        return

    conn = db()
    owner = conn.execute(
        "SELECT owner_user_id FROM owners "
        "WHERE business_connection_id=? AND is_enabled=1",
        (bc_id,),
    ).fetchone()

    if not owner:
        conn.close()
        return

    content_type = message.content_type.value if message.content_type else None
    text = message.text
    caption = message.caption
    reply_to_id = (
        message.reply_to_message.message_id
        if message.reply_to_message
        else None
    )

    conn.execute("""
        INSERT OR REPLACE INTO messages (
            business_connection_id,
            chat_id,
            message_id,
            owner_user_id,
            created_at,
            content_type,
            text,
            caption,
            reply_to_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        bc_id,
        message.chat.id,
        message.message_id,
        owner["owner_user_id"],
        now(),
        content_type,
        text,
        caption,
        reply_to_id,
    ))
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


def quote(text: str | None) -> str:
    if not text:
        return "<blockquote>(без текста)</blockquote>"
    return "<blockquote>" + html.escape(text) + "</blockquote>"


def admin_only(message: Message) -> bool:
    return bool(OWNER_ID and message.from_user and message.from_user.id == OWNER_ID)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔗 Business-подключения",
                callback_data="admin_connections",
            )
        ],
    ])


def stats_text() -> str:
    return (
        "<b>🛠 Админ-панель</b>\n\n"
        f"👤 Пользователей в боте: <b>{get_user_count()}</b>\n"
        f"💾 Сейчас сохранено сообщений: <b>{get_saved_count()}</b>\n"
        f"🔗 Активных Business-подключений: <b>{get_active_connections_count()}</b>\n"
        f"🔗 Всего Business-подключений: <b>{get_total_connections_count()}</b>\n"
    )


@dp.message(CommandStart())
async def start(message: Message):
    if message.from_user:
        touch_user(message.from_user.id)

    await message.answer(
        "Бот готов.\n\n"
        "Подключите его к Telegram Business через настройки Business-аккаунта."
    )


@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user:
        touch_user(message.from_user.id)

    if not admin_only(message):
        await message.answer("⛔ Доступ запрещён.")
        return

    await message.answer(stats_text(), reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id != OWNER_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    action = callback.data

    if action == "admin_connections":
        conn = db()
        rows = conn.execute("""
            SELECT business_connection_id, owner_user_id, is_enabled, updated_at
            FROM owners
            ORDER BY updated_at DESC
        """).fetchall()
        conn.close()

        if not rows:
            text = "<b>🔗 Business-подключения</b>\n\nНет подключений."
        else:
            lines = ["<b>🔗 Business-подключения</b>\n"]
            for i, row in enumerate(rows, 1):
                status = "🟢 активно" if row["is_enabled"] else "🔴 отключено"
                lines.append(
                    f"{i}. {status}\n"
                    f"ID: <code>{html.escape(row['business_connection_id'])}</code>\n"
                    f"Owner ID: <code>{row['owner_user_id']}</code>\n"
                )
            text = "\n".join(lines)

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_refresh")]
            ]),
        )
        await callback.answer()
        return


@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    log.info(
        "Business connection: id=%s user=%s enabled=%s",
        connection.id,
        connection.user.id,
        connection.is_enabled,
    )

    if connection.is_enabled:
        save_connection(connection)
        try:
            await bot.send_message(
                connection.user.id,
                "✅ Business-аккаунт подключён."
            )
        except Exception:
            log.exception("Could not notify owner")
    else:
        disable_connection(connection.id)


@dp.business_message()
async def on_business_message(message: Message):
    try:
        save_message(message)
    except Exception:
        log.exception("Could not save message %s", message.message_id)


@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    try:
        save_message(message)
    except Exception:
        log.exception("Could not update message %s", message.message_id)


@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    owner = get_owner(event.business_connection_id)

    if not owner:
        log.warning(
            "Unknown or disabled business connection: %s",
            event.business_connection_id,
        )
        return

    owner_id = owner["owner_user_id"]

    for message_id in event.message_ids:
        row = get_saved_message(
            event.business_connection_id,
            event.chat.id,
            message_id,
        )

        if row and (row["text"] or row["caption"]):
            original = row["text"] or row["caption"]
            try:
                await bot.send_message(
                    owner_id,
                    "<b>Удаленное сообщение</b>\n" + quote(original),
                )
            except Exception:
                log.exception("Could not send deleted message %s", message_id)

        elif row:
            log.info(
                "Deleted non-text message ignored: chat=%s message=%s type=%s",
                event.chat.id,
                message_id,
                row["content_type"],
            )

        else:
            # The message was deleted before it could be saved or it was
            # outside the retention window.
            try:
                await bot.send_message(
                    owner_id,
                    "<b>Удаленное сообщение</b>\n"
                    "<blockquote>Текст сообщения не был сохранён.</blockquote>",
                )
            except Exception:
                log.exception("Could not send missing-message notice")

        delete_saved_message(
            event.business_connection_id,
            event.chat.id,
            message_id,
        )


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=False)

    log.info("Business archive bot started")

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
