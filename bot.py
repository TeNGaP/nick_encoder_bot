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
from telegram.ext import Application, CommandHandler, ContextTypes

MINI_CTF_THREAD_ID = int(os.getenv("MINI_CTF_THREAD_ID", "0"))


# ---------- Настройки ----------
DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)
QUEUE_FILE = DATA_DIR / "queue.json"

# Время ежедневного поста (Лос-Анджелес)
DAILY_POST_TIME = time(hour=9, minute=0)  # 09:00

METHODS = ["caesar", "rot13", "base64", "hex", "url", "xor", "reverse"]
ALPHABET = "abcdefghijklmnopqrstuvwxyz"


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
    return urllib.parse.quote(text, safe="")

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
        "👋 Привет! Я бот для ежедневных Mini-CTF.\n\n"
        "Команды:\n"
        "• /add <ссылка или текст> — добавить в очередь\n"
        "• /queue — показать размер очереди\n"
        "• /methods — методы шифрования\n"
        "• /chatid — узнать chat_id\n"
        "• /postnow — запостить задание прямо сейчас (админ)\n"
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
        "✅ Ответ можешь написать в чат (ссылкой/текстом)."
    )
    return msg

async def post_challenge(app: Application, chat_id: int):
    queue = load_queue()
    if not queue:
        await app.bot.send_message(
            chat_id=chat_id, message_thread_id=MINI_CTF_THREAD_ID,
            text="📭 Сегодня очередь пустая. Добавь ссылки командой: /add <ссылка>",
        )
        return

    # Берём 1 элемент из очереди (FIFO)
    payload = queue.pop(0)
    save_queue(queue)

    msg = build_challenge_message(payload)
    await app.bot.send_message(chat_id=chat_id, message_thread_id=MINI_CTF_THREAD_ID, text=msg, parse_mode="Markdown")

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
