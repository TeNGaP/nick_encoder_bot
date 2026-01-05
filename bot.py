import os
import random
import base64
import binascii
import urllib.parse
from datetime import time
from typing import Tuple, Optional, List

import psycopg
from psycopg.rows import dict_row

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")

# В Railway обычно будет DATABASE_URL, если ты так назвал переменную.
# Если ты подключал Postgres через {{ Postgres.DATABASE_URL }},
# то создай переменную DATABASE_URL и вставь туда это значение.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("Postgres_DATABASE_URL")

# Основной чат (группа) куда постим
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))

# ID ветки (topic) Mini-CTF / Игры
MINI_CTF_THREAD_ID = int(os.getenv("MINI_CTF_THREAD_ID", "0"))

# ТЗ (для job queue; Railway / Linux обычно читает TZ)
# Поставь в Variables: TZ=America/Los_Angeles
DAILY_POST_TIME = time(hour=9, minute=0)  # 09:00

METHODS = ["caesar", "rot13", "base64", "hex", "url", "xor", "reverse"]
ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Ранги по количеству решений
RANKS = [
    (0,  "🆕 Новичок"),
    (1,  "🧩 Solver"),
    (5,  "🔐 Hacker"),
    (10, "🏆 Elite"),
    (20, "👑 Legend"),
]


# =========================
# DB helpers
# =========================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL env var (Railway Postgres)")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db() -> None:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    solves INT NOT NULL DEFAULT 0,
                    rank TEXT NOT NULL DEFAULT '🆕 Новичок',
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS queue_items (
                    id SERIAL PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS challenges (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    thread_id BIGINT NOT NULL,
                    message_id BIGINT,
                    method TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    encoded TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    hint TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS challenge_solves (
                    challenge_id INT REFERENCES challenges(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    solved_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (challenge_id, user_id)
                );
            """)
        conn.commit()

def get_rank(solves: int) -> str:
    rank = RANKS[0][1]
    for threshold, name in RANKS:
        if solves >= threshold:
            rank = name
        else:
            break
    return rank

def upsert_user(user_id: int, username: str, first_name: str) -> None:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    updated_at = NOW();
            """, (user_id, username, first_name))
        conn.commit()

def add_solve(user_id: int) -> Tuple[int, str, str]:
    """
    +1 solve, пересчитать ранг
    returns: (new_solves, old_rank, new_rank)
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT solves, rank FROM users WHERE user_id=%s;", (user_id,))
            row = cur.fetchone()
            if not row:
                # если вдруг нет — создадим с 0 и потом добавим
                cur.execute("""
                    INSERT INTO users (user_id, solves, rank)
                    VALUES (%s, 0, %s)
                    ON CONFLICT (user_id) DO NOTHING;
                """, (user_id, get_rank(0)))
                old_solves = 0
                old_rank = get_rank(0)
            else:
                old_solves = int(row["solves"])
                old_rank = row["rank"] or get_rank(old_solves)

            new_solves = old_solves + 1
            new_rank = get_rank(new_solves)

            cur.execute("""
                UPDATE users
                SET solves=%s, rank=%s, updated_at=NOW()
                WHERE user_id=%s;
            """, (new_solves, new_rank, user_id))
        conn.commit()

    return new_solves, old_rank, new_rank

def queue_push(payload: str) -> int:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO queue_items (payload) VALUES (%s);", (payload,))
            cur.execute("SELECT COUNT(*) AS c FROM queue_items;")
            c = int(cur.fetchone()["c"])
        conn.commit()
    return c

def queue_count() -> int:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM queue_items;")
            c = int(cur.fetchone()["c"])
    return c

def queue_pop_fifo() -> Optional[str]:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, payload
                FROM queue_items
                ORDER BY id ASC
                LIMIT 1;
            """)
            row = cur.fetchone()
            if not row:
                return None
            item_id = row["id"]
            payload = row["payload"]
            cur.execute("DELETE FROM queue_items WHERE id=%s;", (item_id,))
        conn.commit()
    return payload

def deactivate_old_challenges(chat_id: int, thread_id: int) -> None:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE challenges
                SET is_active=FALSE
                WHERE chat_id=%s AND thread_id=%s AND is_active=TRUE;
            """, (chat_id, thread_id))
        conn.commit()

def create_challenge(chat_id: int, thread_id: int, message_id: int,
                     method: str, payload: str, encoded: str, answer: str, hint: str) -> int:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO challenges
                    (chat_id, thread_id, message_id, method, payload, encoded, answer, hint, is_active)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id;
            """, (chat_id, thread_id, message_id, method, payload, encoded, answer, hint))
            cid = int(cur.fetchone()["id"])
        conn.commit()
    return cid

def get_active_challenge() -> Optional[dict]:
    if TARGET_CHAT_ID == 0 or MINI_CTF_THREAD_ID == 0:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM challenges
                WHERE chat_id=%s AND thread_id=%s AND is_active=TRUE
                ORDER BY id DESC
                LIMIT 1;
            """, (TARGET_CHAT_ID, MINI_CTF_THREAD_ID))
            row = cur.fetchone()
    return row

def has_solved(challenge_id: int, user_id: int) -> bool:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM challenge_solves
                WHERE challenge_id=%s AND user_id=%s
                LIMIT 1;
            """, (challenge_id, user_id))
            row = cur.fetchone()
    return bool(row)

def mark_solved(challenge_id: int, user_id: int) -> None:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO challenge_solves (challenge_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING;
            """, (challenge_id, user_id))
        conn.commit()


# =========================
# CIPHERS
# =========================
def caesar_encode(text: str, shift: int) -> str:
    def shift_char(c: str) -> str:
        if c.isalpha():
            idx = ALPHABET.find(c.lower())
            if idx == -1:
                return c
            new = ALPHABET[(idx + shift) % 26]
            return new.upper() if c.isupper() else new
        return c
    return "".join(shift_char(c) for c in text)

def rot13(text: str) -> str:
    return caesar_encode(text, 13)

def b64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")

def hex_encode(text: str) -> str:
    return binascii.hexlify(text.encode("utf-8")).decode("ascii")

def url_encode(text: str) -> str:
    # Важно: percent-encoding. Такой вывод ты хотел (как %2F%3A...)
    return "".join(f"%{b:02X}" for b in text.encode("utf-8"))

def xor_encode(text: str, key: bytes) -> str:
    data = text.encode("utf-8")
    out = bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])
    return base64.b64encode(out).decode("ascii")

def reverse(text: str) -> str:
    return text[::-1]

def encode_text(method: str, text: str) -> Tuple[str, str]:
    method = method.lower()

    if method == "caesar":
        shift = random.randint(1, 25)
        return caesar_encode(text, shift), f"Подсказка: Caesar cipher, сдвиг = {shift}"
    if method == "rot13":
        return rot13(text), "Подсказка: ROT13 (это Caesar со сдвигом 13)"
    if method == "base64":
        return b64_encode(text), "Подсказка: Base64"
    if method == "hex":
        return hex_encode(text), "Подсказка: HEX → UTF-8"
    if method == "url":
        return url_encode(text), "Подсказка: URL encoding (percent-encoding)"
    if method == "xor":
        key = os.urandom(4)
        return xor_encode(text, key), f"Подсказка: XOR + Base64, ключ (hex) = {key.hex()}"
    if method == "reverse":
        return reverse(text), "Подсказка: строка просто перевёрнута"
    raise ValueError("Unknown method")

def build_challenge_message(encoded: str, hint: str) -> str:
    return (
        "🧩 *Mini-CTF дня*\n\n"
        "Расшифруй и получи исходную ссылку/текст 👇\n\n"
        f"`{encoded}`\n\n"
        f"📌 {hint}\n\n"
        "✉️ *Ответ отправляй боту в личку* (чтобы никто не спойлерил):\n"
        "@nick_encoder_bot"
    )

def normalize(s: str) -> str:
    return s.strip()


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "🧠 *Справка по командам бота*\n\n"
    "📌 *Основное*\n"
    "• /help — показать эту справку\n"
    "• /methods — методы шифрования\n"
    "• /chatid — показать chat_id (для настройки)\n\n"
    "🧩 *Mini-CTF*\n"
    "• /add <текст/ссылка> — добавить задание в очередь\n"
    "• /queue — сколько заданий в очереди\n"
    "• /postnow — запостить Mini-CTF прямо сейчас (только админ)\n\n"
    "🏆 *Прогресс*\n"
    "• /profile — твой профиль (ранг + решения)\n"
    "  ↳ можно ответить (reply) на сообщение человека и написать /profile — покажет его профиль\n"
    "• /leaderboard — топ-10 по решениям\n\n"
    "✅ *Как засчитывается решение*\n"
    "Ответ пишем *только в личные сообщения боту*.\n"
    "В группе ответы можно писать, но бот удалит их (если у него есть право удалять)."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для ежедневных Mini-CTF.\n"
        "🧠 *Справка по командам бота*\n\n"
        "📌 *Основное*\n"
        "• /methods — методы шифрования\n"
        "• /chatid — показать chat_id (для настройки)\n\n"
        "🧩 *Mini-CTF*\n"
        "• /add <текст/ссылка> — добавить задание в очередь\n"
        "• /queue — сколько заданий в очереди\n"
        "• /postnow — запостить Mini-CTF прямо сейчас (только админ)\n\n"
        "🏆 *Прогресс*\n"
        "• /profile — твой профиль (ранг + решения)\n"
        "  ↳ можно ответить (reply) на сообщение человека и написать /profile — покажет его профиль\n"
        "• /leaderboard — топ-10 по решениям\n\n"
        "✅ *Как засчитывается решение*\n"
        "Ответ пишем *только в личные сообщения боту*.\n"
        "В группе ответы можно писать, но бот удалит их (если у него есть право удалять)."
    )


async def methods_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Методы: " + ", ".join(METHODS))

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /add <ссылка или текст>")
        return
    c = queue_push(text)
    await update.message.reply_text(f"✅ Добавлено в очередь! Сейчас в очереди: {c}")

async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = queue_count()
    await update.message.reply_text(f"📦 В очереди: {c}")

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # если reply — показываем профиль того пользователя
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    user_id = int(target.id)
    username = target.username or target.first_name or "Unknown"
    upsert_user(user_id, target.username or "", target.first_name or "")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT solves, rank FROM users WHERE user_id=%s;", (user_id,))
            row = cur.fetchone()

    solves = int(row["solves"]) if row else 0
    rank = row["rank"] if row else get_rank(0)

    await update.message.reply_text(
        f"👤 *{username}*\n"
        f"Ранг: {rank}\n"
        f"Решено: *{solves}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, COALESCE(username, first_name) AS name, solves, rank
                FROM users
                WHERE solves > 0
                ORDER BY solves DESC
                LIMIT 10;
            """)
            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("📭 Пока никто не решил ни одного Mini-CTF.")
        return

    text = "🏆 *Leaderboard*\n\n"
    for i, r in enumerate(rows, start=1):
        name = r["name"] or str(r["user_id"])
        rank = r["rank"] or get_rank(int(r["solves"]))
        text += f"{i}. {rank} *{name}* — {r['solves']} ✅\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def post_challenge(app: Application, chat_id: int) -> None:
    if chat_id == 0 or MINI_CTF_THREAD_ID == 0:
        raise RuntimeError("Set TARGET_CHAT_ID and MINI_CTF_THREAD_ID env vars")

    payload = queue_pop_fifo()
    if not payload:
        await app.bot.send_message(
            chat_id=chat_id,
            message_thread_id=MINI_CTF_THREAD_ID,
            text="📭 Сегодня очередь пустая. Добавь задания командой: /add <ссылка/текст>",
        )
        return

    method = random.choice(METHODS)
    encoded, hint = encode_text(method, payload)
    msg = build_challenge_message(encoded, hint)

    sent = await app.bot.send_message(
        chat_id=chat_id,
        message_thread_id=MINI_CTF_THREAD_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN
    )

    # Деактивируем старое активное задание, создаём новое
    deactivate_old_challenges(chat_id, MINI_CTF_THREAD_ID)
    create_challenge(
        chat_id=chat_id,
        thread_id=MINI_CTF_THREAD_ID,
        message_id=sent.message_id,
        method=method,
        payload=payload,
        encoded=encoded,
        answer=payload,
        hint=hint
    )

async def postnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админы
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("⛔ Только админы могут делать /postnow")
        return

    await post_challenge(context.application, update.effective_chat.id)

async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    if TARGET_CHAT_ID == 0:
        return
    await post_challenge(context.application, TARGET_CHAT_ID)


# =========================
# ANSWER CHECKER
# =========================
async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # 1) Если человек пишет ответ В ГРУППЕ в Mini-CTF ветке — удаляем (если можем) и просим писать в ЛС
    if update.effective_chat.type != "private":
        # удаляем только если это в нужной ветке
        if msg.message_thread_id == MINI_CTF_THREAD_ID:
            try:
                await msg.delete()
            except Exception:
                pass  # нет прав удалять
            # (по желанию) можно отправить подсказку в личку, если бот уже видел пользователя
        return

    # 2) В ЛС — проверяем ответ
    current = get_active_challenge()
    if not current:
        await msg.reply_text("❌ Сейчас нет активного Mini-CTF.")
        return

    user = update.effective_user
    upsert_user(user.id, user.username or "", user.first_name or "")

    challenge_id = int(current["id"])
    if has_solved(challenge_id, user.id):
        await msg.reply_text("ℹ️ Ты уже решил это задание.")
        return

    user_answer = normalize(msg.text)
    correct = normalize(current["answer"] or "")

    if user_answer != correct:
        await msg.reply_text("❌ Неверно. Попробуй ещё раз 👀")
        return

    # ✅ Засчитываем
    mark_solved(challenge_id, user.id)

    new_solves, old_rank, new_rank = add_solve(user.id)

    await msg.reply_text(
        "🎉 Верно!\n\n"
        f"🏆 Всего решений: {new_solves}\n"
        f"Ранг: {new_rank}"
    )

    if new_rank != old_rank:
        await msg.reply_text(f"🎉 Новый ранг: {new_rank}")


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var")
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL env var")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("methods", methods_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("postnow", postnow_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    # Any text (answers) -> checker
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer))

    # Daily post
    # Важно: для PTB job_queue нужен пакет python-telegram-bot[job-queue]
    app.job_queue.run_daily(daily_job, time=DAILY_POST_TIME)

    app.run_polling()

if __name__ == "__main__":
    main()
