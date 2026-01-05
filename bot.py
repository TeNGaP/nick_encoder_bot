import os
import json
import random
import base64
import binascii
import urllib.parse
from pathlib import Path
from datetime import time
from typing import Tuple, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

MINI_CTF_THREAD_ID = int(os.getenv("MINI_CTF_THREAD_ID", "0"))


# ---------- Настройки ----------
DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)
QUEUE_FILE = DATA_DIR / "queue.json"
SCORES_FILE = DATA_DIR / "scores.json"
CURRENT_FILE = DATA_DIR / "current_challenge.json"

RANKS = [
    (0,  "🆕 Новичок"),
    (1,  "🧩 Solver"),
    (5,  "🔐 Hacker"),
    (10, "🏆 Elite"),
    (20, "👑 Legend"),
]

# Challenge
def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return {}
    try:
        return json.loads(CURRENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_current(data: dict) -> None:
    CURRENT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# Roles and Scores
def load_scores() -> dict:
    if not SCORES_FILE.exists():
        return {}
    return json.loads(SCORES_FILE.read_text(encoding="utf-8"))

def save_scores(scores: dict) -> None:
    SCORES_FILE.write_text(
        json.dumps(scores, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def get_rank(solves: int) -> str:
    rank = RANKS[0][1]
    for threshold, name in RANKS:
        if solves >= threshold:
            rank = name
        else:
            break
    return rank


async def solve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Ответь /solve на сообщение с решением.")
        return

    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id
    )
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("⛔ Только админы могут подтверждать решения.")
        return

    user = update.message.reply_to_message.from_user
    user_id = str(user.id)
    username = user.username or user.first_name

    scores = load_scores()

    # если пользователь новый
    if user_id not in scores:
        scores[user_id] = {
        "name": username,
        "solves": 0,
    }

    old_solves = scores[user_id]["solves"]
    old_rank = get_rank(old_solves)

    # увеличиваем счётчик
    scores[user_id]["solves"] += 1

    new_solves = scores[user_id]["solves"]
    new_rank = get_rank(new_solves)

    save_scores(scores)

    await update.message.reply_text(
    f"🧩 *{username}* решил Mini-CTF!\n"
    f"Всего решений: *{new_solves}*",
    parse_mode="Markdown"
    )

    # 🎉 Проверяем ап ранга
    if new_rank != old_rank:
        await update.message.reply_text(
            f"🎉 *Новый ранг:* {new_rank}",
            parse_mode="Markdown"
        )


# Profile
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = load_scores()

    # Если команда отправлена reply — смотрим профиль того пользователя
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user

    user_id = str(target_user.id)
    username = target_user.username or target_user.first_name

    if user_id not in scores:
        await update.message.reply_text(
            f"👤 *{username}*\n"
            f"Ранг: 🆕 Новичок\n"
            f"Решено: 0\n\n"
            f"💡 Решай Mini-CTF, чтобы прокачать ранг!",
            parse_mode="Markdown"
        )
        return

    solves = scores[user_id].get("solves", 0)
    role = get_rank(solves)

    await update.message.reply_text(
        f"👤 *{username}*\n"
        f"Ранг: {role}\n"
        f"Решено: *{solves}*",
        parse_mode="Markdown"
    )

#Leaderboard
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = load_scores()

    if not scores:
        await update.message.reply_text("📭 Пока никто не решил ни одного Mini-CTF.")
        return

    sorted_users = sorted(
        scores.values(),
        key=lambda x: x["solves"],
        reverse=True
    )

    text = "🏆 *Leaderboard*\n\n"
    for i, user in enumerate(sorted_users[:10], start=1):
        role = user.get("role") or get_rank(user.get("solves", 0))
        text += f"{i}. {role} *{user['name']}* — {user['solves']} ✅\n"


    await update.message.reply_text(text, parse_mode="Markdown")

# Время ежедневного поста (Лос-Анджелес)
DAILY_POST_TIME = time(hour=9, minute=0)  # 09:00

METHODS = ["caesar", "rot13", "base64", "hex", "url", "xor", "reverse"]
ALPHABET = "abcdefghijklmnopqrstuvwxyz"

#Checker
def normalize(s: str) -> str:
    return s.strip()

async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ✅ Принимаем ТОЛЬКО личные сообщения
    if update.effective_chat.type != "private":
        return

    msg = update.message
    if not msg or not msg.text:
        return

    current = load_current()
    if not current:
        await msg.reply_text("❌ Сейчас нет активного Mini-CTF.")
        return

    user = update.effective_user
    user_id = str(user.id)
    username = user.username or user.first_name

    # Не засчитываем повторно
    solved_by = current.get("solved_by", [])
    if user_id in solved_by:
        await msg.reply_text("ℹ️ Ты уже решил это задание.")
        return

    user_answer = normalize(msg.text)
    correct = normalize(current.get("answer", ""))

    if user_answer != correct:
        await msg.reply_text("❌ Неверно. Попробуй ещё раз 👀")
        return

    # ✅ Засчитываем решение
    scores = load_scores()
    if user_id not in scores:
        scores[user_id] = {"name": username, "solves": 0, "role": "Solver"}

    scores[user_id]["name"] = username
    scores[user_id]["solves"] += 1
    save_scores(scores)

    solved_by.append(user_id)
    current["solved_by"] = solved_by
    save_current(current)

    await msg.reply_text(
        f"🎉 Верно!\n\n"
        f"🧠 Ты решил Mini-CTF\n"
        f"🏆 Всего решений: {scores[user_id]['solves']}"
    )

# ---------- Хранилище очереди ----------
def load_queue() -> list[str]:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_queue(queue: list[str]) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Шифры ----------
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
    return "".join(f"%{b:02X}" for b in text.encode("utf-8"))

def xor_encode(text: str, key: bytes) -> str:
    data = text.encode("utf-8")
    out = bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])
    return base64.b64encode(out).decode("ascii")

def reverse(text: str) -> str:
    return text[::-1]


def encode_text(method: str, text: str) -> Tuple[str, str]:
    """
    Возвращает (encoded, hint).
    Hint мы показываем, потому что ты хочешь формат обучения.
    """
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


# ---------- Telegram команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для ежедневных Mini-CTF.🧠 *Справка по командам бота*\n\n"
        "📌 *Основное*\n"
        "• /start — краткое приветствие\n"
        "• /methods — методы шифрования, которые использует бот\n"
        "• /chatid — показать chat_id текущего чата (для настройки)\n\n"
        "🧩 *Mini-CTF*\n"
        "• /add <ссылка или текст> — добавить задание в очередь\n"
        "• /queue — показать сколько заданий в очереди\n"
        "• /postnow — запостить Mini-CTF прямо сейчас (только админ)\n\n"
        "🏆 *Прогресс*\n"
        "• /profile — твой профиль (ранг + решённые задания)\n"
        "  ↳ также можно ответить (reply) на сообщение человека и написать /profile — покажет его профиль\n"
        "• /leaderboard — топ-10 по количеству решённых Mini-CTF\n\n"
        "✅ *Как засчитывается решение*\n"
        "✉️ Ответ отправляй боту в личные сообщения:\n"
        "@nick_encoder_bot\n"
    )

async def methods(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Методы: " + ", ".join(METHODS))

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /add <ссылка или текст>")
        return

    queue = load_queue()
    queue.append(text)
    save_queue(queue)

    await update.message.reply_text(
        f"✅ Добавлено в очередь! Сейчас в очереди: {len(queue)}"
    )

async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = load_queue()
    await update.message.reply_text(f"📦 В очереди: {len(queue)}")

def build_challenge_message(payload: str) -> str:
    method = random.choice(METHODS)
    encoded, hint = encode_text(method, payload)

    msg = (
        "🧩 *Mini-CTF дня*\n\n"
        "Расшифруй и получи исходную ссылку/текст 👇\n\n"
        f"`{encoded}`\n\n"
        f"📌 {hint}\n\n"
        "✉️ Ответ отправляй боту в личные сообщения:\n"
        "@nick_encoder_bot\n"
    )
    return msg

async def post_challenge(app: Application, chat_id: int):
    queue = load_queue()
    if not queue:
        await app.bot.send_message(
            chat_id=chat_id,
            message_thread_id=MINI_CTF_THREAD_ID,
            text="📭 Сегодня очередь пустая. Добавь ссылки командой: /add <ссылка>",
        )
        return

    # Берём 1 элемент из очереди (FIFO)
    payload = queue.pop(0)
    save_queue(queue)

    msg = build_challenge_message(payload)

    sent = await app.bot.send_message(
        chat_id=chat_id,
        message_thread_id=MINI_CTF_THREAD_ID,
        text=msg,
        parse_mode="Markdown"
    )

    # 🔐 СОХРАНЯЕМ АКТИВНОЕ ЗАДАНИЕ
    save_current({
        "chat_id": chat_id,
        "thread_id": MINI_CTF_THREAD_ID,
        "message_id": sent.message_id,  # ← ключевая строка
        "answer": payload,              # правильный ответ
        "solved_by": []
    })



async def postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Простая защита: разрешаем только админам
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("⛔ Только админы могут делать /postnow")
        return

    chat_id = update.effective_chat.id
    await post_challenge(context.application, chat_id)

async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id_env = os.getenv("TARGET_CHAT_ID")
    if not chat_id_env:
        return
    await post_challenge(context.application, int(chat_id_env))


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env var")

    app = Application.builder().token(token).build()

    # handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("solve", solve))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("methods", methods))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("postnow", postnow))

    # ежедневная задача (по времени)
    # Важно: PTB использует таймзону из переменной TZ ОС. Поставь TZ=America/Los_Angeles при запуске.
    app.job_queue.run_daily(daily_job, time=DAILY_POST_TIME)

    app.run_polling()

if __name__ == "__main__":
    main()
