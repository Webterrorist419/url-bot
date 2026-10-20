#!/usr/bin/env python3
"""URL Bot. Python 3.10+, standard library only.

Fill BOT_TOKEN and ADMIN_ID in config.env, then run: python3 -B bot.py
For a bundled Linux executable, place config.env next to the executable.
On Ubuntu/Debian use: sudo bash install.sh --install
The installer provides the url-bot-admin command and a systemd service.
DATABASE_PATH may be empty; by default data is stored outside this folder.
"""

from __future__ import annotations

import json
from http.client import HTTPException
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


LOG = logging.getLogger("url_bot")
APP_PATH = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
CONFIG_PATH = APP_PATH.with_name("config.env")
STOP = threading.Event()
BUTTON_COUNT = 5


def read_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    """Read literal values without executing shell code or expanding variables."""
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or key not in {"BOT_TOKEN", "ADMIN_ID", "DATABASE_PATH"}:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    for key in ("BOT_TOKEN", "ADMIN_ID", "DATABASE_PATH"):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def database_path(value: str = "") -> Path:
    if value:
        path = Path(value).expanduser()
        return path if path.is_absolute() else CONFIG_PATH.parent / path
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "url-bot" / "bot.sqlite3"


def pick(lang: str, ru: str, en: str) -> str:
    return en if lang == "en" else ru


def valid_url(value: str) -> bool:
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        return False


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=10)
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.execute("""CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY, lang TEXT NOT NULL DEFAULT 'ru',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                counted INTEGER NOT NULL DEFAULT 1)""")
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(users)")}
            if "counted" not in columns:
                self.connection.execute("ALTER TABLE users ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
            self.connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS admin_state (telegram_id INTEGER PRIMARY KEY, action TEXT NOT NULL)")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                next_at REAL NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS broadcast_recipients (
                job_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(job_id,user_id))""")
            # A crash after Telegram accepted a message cannot be resolved automatically.
            self.connection.execute("UPDATE broadcast_recipients SET status='unknown' WHERE status='sending'")
            defaults = {
                "subscription_enabled": "0",
                "subscription_chat": "",
                "subscription_url": "",
                "description_ru": "Добро пожаловать! Выберите нужную ссылку.",
                "description_en": "Welcome! Choose a link.",
            }
            for n in range(1, BUTTON_COUNT + 1):
                defaults.update({
                    f"button{n}_label_ru": f"Ссылка {n}",
                    f"button{n}_label_en": f"Link {n}",
                    f"button{n}_url": "https://example.com",
                    f"button{n}_enabled": "1",
                })
            self.connection.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", defaults.items())

    def get(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def put(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def language(self, user_id: int) -> str:
        row = self.connection.execute("SELECT lang FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        return "en" if row and row[0] == "en" else "ru"

    def set_language(self, user_id: int, lang: str) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO users(telegram_id,lang,counted) VALUES(?,?,0) ON CONFLICT(telegram_id) DO UPDATE SET lang=excluded.lang", (user_id, lang))

    def record_start(self, user_id: int) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO users(telegram_id,counted) VALUES(?,1) ON CONFLICT(telegram_id) DO UPDATE SET counted=1", (user_id,))

    def count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM users WHERE counted=1").fetchone()[0]

    def reset_count(self) -> None:
        with self.connection:
            self.connection.execute("UPDATE users SET counted=0")

    def state(self, user_id: int) -> str:
        row = self.connection.execute("SELECT action FROM admin_state WHERE telegram_id=?", (user_id,)).fetchone()
        return row[0] if row else ""

    def set_state(self, user_id: int, action: str = "") -> None:
        with self.connection:
            if action:
                self.connection.execute("INSERT INTO admin_state(telegram_id,action) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET action=excluded.action", (user_id, action))
            else:
                self.connection.execute("DELETE FROM admin_state WHERE telegram_id=?", (user_id,))

    def save_edit(self, user_id: int, key: str, value: str) -> None:
        # Save the setting and finish the edit together, or roll both back.
        with self.connection:
            self.connection.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.connection.execute("DELETE FROM admin_state WHERE telegram_id=?", (user_id,))

    def close(self) -> None:
        self.connection.close()


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, stop: threading.Event = STOP):
        self.token = token
        self.stop = stop

    def call(self, method: str, **params):
        attempts = 1 if params.pop("_broadcast", False) else 3
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "URLBot/1.3.0"},
        )
        timeout = int(params.get("timeout", 0)) + 15
        for attempt in range(attempts):
            try:
                try:
                    with urlopen(request, timeout=timeout) as response:
                        result = json.load(response)
                except HTTPError as error:
                    try:
                        result = json.loads(error.read())
                    except (ValueError, UnicodeError):
                        raise TelegramError(error.code, f"Telegram HTTP {error.code}") from None
                    finally:
                        error.close()
                if not isinstance(result, dict):
                    raise ValueError("Invalid Telegram response")
                if not result.get("ok"):
                    raise TelegramError(
                        int(result.get("error_code", 500)),
                        str(result.get("description", "Telegram request failed")).replace(self.token, "[redacted]"),
                        int(result.get("parameters", {}).get("retry_after", 0)),
                    )
                if "result" not in result:
                    raise ValueError("Missing Telegram result")
                return result["result"]
            except TelegramError as error:
                if attempt == attempts - 1 or (error.code != 429 and error.code < 500):
                    raise
                if self.stop.wait(max(1, error.retry_after or 2 ** attempt)):
                    raise
            except (URLError, OSError, HTTPException, ValueError) as error:
                # Never log a urllib exception: its URL can contain the bot token.
                if attempt == attempts - 1 or self.stop.wait(2 ** attempt):
                    raise TelegramError(503, f"Network/response error ({type(error).__name__})") from None


def callback(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def keyboard(rows: list) -> dict:
    return {"inline_keyboard": rows}


class TelegramBot:
    def __init__(self, api: TelegramAPI, db: Database, admin_id: int):
        self.api, self.db, self.admin_id = api, db, admin_id

    def main_keyboard(self, lang: str, admin: bool = False) -> dict:
        rows = []
        for n in range(1, BUTTON_COUNT + 1):
            url = self.db.get(f"button{n}_url")
            if self.db.get(f"button{n}_enabled") == "1" and valid_url(url):
                rows.append([{"text": self.db.get(f"button{n}_label_{lang}"), "url": url}])
        rows.append([callback(pick(lang, "🌐 Язык: RU / EN", "🌐 Language: EN / RU"), "language")])
        if admin:
            rows.append([callback(pick(lang, "⚙️ Админ меню", "⚙️ Admin menu"), "admin")])
        return keyboard(rows)

    @staticmethod
    def admin_keyboard(lang: str) -> dict:
        rows = [[callback(pick(lang, f"Ссылка {n}: URL / текст / вкл-выкл", f"Link {n}: URL / label / on-off"), f"button_{n}")] for n in range(1, BUTTON_COUNT + 1)]
        rows.extend([
            [callback(pick(lang, "📣 Рассылки", "📣 Broadcasts"), "broadcast")],
            [callback(pick(lang, "🔐 Обязательная подписка", "🔐 Required subscription"), "subscription")],
            [callback(pick(lang, "Описание бота", "Bot description"), "description")],
            [callback(pick(lang, "📊 Статистика", "📊 Statistics"), "stats")],
            [callback(pick(lang, "🗑 Сбросить статистику", "🗑 Reset statistics"), "reset"), callback(pick(lang, "↩️ В меню", "↩️ Main menu"), "home")],
            [callback(pick(lang, "🌐 Язык: RU / EN", "🌐 Language: EN / RU"), "language")],
        ])
        return keyboard(rows)

    def button_keyboard(self, lang: str, n: str) -> dict:
        enabled = self.db.get(f"button{n}_enabled") == "1"
        return keyboard([
            [callback(pick(lang, "Изменить URL", "Change URL"), f"edit_url_{n}")],
            [callback(pick(lang, "Текст кнопки (RU)", "Button text (RU)"), f"edit_label_{n}_ru")],
            [callback(pick(lang, "Текст кнопки (EN)", "Button text (EN)"), f"edit_label_{n}_en")],
            [callback(pick(lang, "🔴 Выключить", "🔴 Disable") if enabled else pick(lang, "🟢 Включить", "🟢 Enable"), f"toggle_{n}")],
            [callback(pick(lang, "↩️ Админ меню", "↩️ Admin menu"), "admin")],
            [callback(pick(lang, "🌐 Язык: RU / EN", "🌐 Language: EN / RU"), "language")],
        ])

    def send(self, user_id: int, text: str, markup: dict | None = None) -> None:
        params = {"chat_id": user_id, "text": text}
        if markup is not None:
            params["reply_markup"] = markup
        self.api.call("sendMessage", **params)

    def home(self, user_id: int) -> None:
        lang = self.db.language(user_id)
        if not self.has_access(user_id):
            return
        self.send(user_id, self.db.get(f"description_{lang}"), self.main_keyboard(lang, user_id == self.admin_id))

    def subscription_keyboard(self, lang: str) -> dict:
        enabled = self.db.get("subscription_enabled") == "1"
        return keyboard([
            [callback(pick(lang, "Чат / канал", "Chat / channel"), "subscription_chat")],
            [callback(pick(lang, "Ссылка для вступления", "Join link"), "subscription_url")],
            [callback(pick(lang, "🔴 Выключить", "🔴 Disable") if enabled else pick(lang, "🟢 Включить", "🟢 Enable"), "subscription_toggle")],
            [callback(pick(lang, "↩️ Админ меню", "↩️ Admin menu"), "admin")],
        ])

    def show_subscription(self, user_id: int) -> None:
        lang = self.db.language(user_id)
        enabled = self.db.get("subscription_enabled") == "1"
        self.send(user_id, pick(lang,
            f"Обязательная подписка: {'включена' if enabled else 'выключена'}\nЧат / канал: {self.db.get('subscription_chat') or 'не задан'}\nСсылка: {self.db.get('subscription_url') or 'не задана'}\n\nДобавьте бота администратором в выбранный чат или канал.",
            f"Required subscription: {'enabled' if enabled else 'disabled'}\nChat / channel: {self.db.get('subscription_chat') or 'not set'}\nJoin link: {self.db.get('subscription_url') or 'not set'}\n\nAdd the bot as an administrator in the selected chat or channel."), self.subscription_keyboard(lang))

    def validate_subscription_chat(self, value: str) -> str:
        if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|-[1-9][0-9]{0,18}", value):
            raise ValueError("chat")
        chat = self.api.call("getChat", chat_id=value)
        if chat.get("type") not in {"group", "supergroup", "channel"}:
            raise ValueError("chat")
        me = self.api.call("getMe")
        member = self.api.call("getChatMember", chat_id=chat["id"], user_id=me["id"])
        if member.get("status") not in {"creator", "administrator"}:
            raise ValueError("administrator")
        return str(chat["id"])

    def has_access(self, user_id: int) -> bool:
        if user_id == self.admin_id or self.db.get("subscription_enabled") != "1":
            return True
        lang = self.db.language(user_id)
        failed = False
        try:
            member = self.api.call("getChatMember", chat_id=self.db.get("subscription_chat"), user_id=user_id)
            if member.get("status") in {"creator", "administrator", "member"} or (member.get("status") == "restricted" and member.get("is_member") is True):
                return True
        except TelegramError:
            failed = True
            LOG.warning("Subscription check unavailable")
        rows = []
        url = self.db.get("subscription_url")
        if valid_url(url):
            rows.append([{"text": pick(lang, "📢 Подписаться", "📢 Subscribe"), "url": url}])
        rows.extend([
            [callback(pick(lang, "✅ Проверить подписку", "✅ Check subscription"), "check_subscription")],
            [callback(pick(lang, "🌐 Язык: RU / EN", "🌐 Language: EN / RU"), "language")],
        ])
        self.send(user_id, pick(lang,
            "Не удалось проверить подписку. Повторите попытку позже." if failed else "Чтобы открыть меню бота, сначала вступите в чат или подпишитесь на канал, затем нажмите «Проверить подписку».",
            "Could not verify your subscription. Please try again later." if failed else "Join the chat or subscribe to the channel, then press Check subscription to open the bot menu."), keyboard(rows))
        return False

    def handle_update(self, update: dict) -> None:
        try:
            if "message" in update:
                self.on_message(update["message"])
            elif "callback_query" in update:
                self.on_callback(update["callback_query"])
        except sqlite3.Error:
            LOG.exception("Database operation failed for update %s", update.get("update_id"))
            message = update.get("message") or update.get("callback_query", {}).get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") == "private":
                self.send(chat["id"], "Ошибка базы данных. Повторите действие позже. / Database error. Please try again later.")

    def on_message(self, message: dict) -> None:
        sender, chat = message.get("from", {}), message.get("chat", {})
        text = message.get("text")
        if chat.get("type") != "private" or not sender.get("id") or sender.get("id") != chat.get("id"):
            return
        user_id = sender["id"]
        lang = self.db.language(user_id)
        if user_id == self.admin_id and self.db.state(user_id) == "broadcast:compose" and not (isinstance(text, str) and text.startswith("/")):
            self.prepare_broadcast(message)
            return
        if not isinstance(text, str):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
        if command == "/start":
            self.db.set_state(user_id)
            self.db.record_start(user_id)
            self.home(user_id)
            return
        if user_id != self.admin_id:
            self.has_access(user_id)
            return
        if command == "/cancel":
            self.db.set_state(user_id)
            self.send(user_id, pick(lang, "Ввод отменён.", "Edit cancelled."), self.admin_keyboard(lang))
            return
        if command.startswith("/"):
            self.send(user_id, pick(lang, "Для отмены ввода: /cancel. Главное меню: /start.", "Cancel editing: /cancel. Main menu: /start."))
            return
        action = self.db.state(user_id)
        if not action:
            self.send(user_id, pick(lang, "Откройте админ меню кнопкой ниже.", "Open the admin menu below."), self.main_keyboard(lang, True))
            return
        value = text.strip()
        if action == "subscription:chat":
            try:
                value = self.validate_subscription_chat(value)
            except (ValueError, TelegramError):
                self.send(user_id, pick(lang, "Не удалось проверить чат. Отправьте @username или отрицательный ID чата (например -1001234567890) и добавьте бота его администратором. Повторите ввод или /cancel.", "Could not verify chat. Send @username or a negative chat ID (e.g. -1001234567890) and add the bot as its administrator. Try again or /cancel."))
                return
            # Changing the target always requires explicitly enabling the gate again.
            self.db.put("subscription_enabled", "0")
            key = "subscription_chat"
        elif action == "subscription:url":
            if not valid_url(value) or urlsplit(value).scheme != "https" or urlsplit(value).netloc.lower() != "t.me" or not re.fullmatch(r"/(?:[A-Za-z][A-Za-z0-9_]{4,31}|\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)/?", urlsplit(value).path) or urlsplit(value).query or urlsplit(value).fragment:
                self.send(user_id, pick(lang, "Нужна ссылка https://t.me/имя или https://t.me/+приглашение. Повторите ввод или /cancel.", "Enter https://t.me/username or https://t.me/+invite. Try again or /cancel."))
                return
            self.db.put("subscription_enabled", "0")
            key = "subscription_url"
        elif re.fullmatch(r"url:[1-5]", action):
            if not valid_url(value):
                self.send(user_id, pick(lang, "Нужна полная ссылка http:// или https://. Повторите ввод или /cancel.", "Enter a complete http:// or https:// URL, or /cancel."))
                return
            key = f"button{action[-1]}_url"
        elif match := re.fullmatch(r"label:([1-5]):(ru|en)", action):
            if not 1 <= len(value) <= 64:
                self.send(user_id, pick(lang, "Текст кнопки: от 1 до 64 символов. Повторите ввод или /cancel.", "Button text must contain 1–64 characters. Try again or /cancel."))
                return
            key = f"button{match[1]}_label_{match[2]}"
        elif match := re.fullmatch(r"desc:(ru|en)", action):
            if not 1 <= len(value.encode("utf-16-le")) // 2 <= 4096:
                self.send(user_id, pick(lang, "Описание: от 1 до 4096 символов. Повторите ввод или /cancel.", "Description must contain 1–4096 characters. Try again or /cancel."))
                return
            key = f"description_{match[1]}"
        else:
            self.db.set_state(user_id)
            self.home(user_id)
            return
        self.db.save_edit(user_id, key, value)
        if action.startswith("subscription:"):
            self.show_subscription(user_id)
            return
        self.send(user_id, pick(lang, "Сохранено.", "Saved."), self.admin_keyboard(lang))

    def on_callback(self, query: dict) -> None:
        user_id = query.get("from", {}).get("id")
        message = query.get("message", {})
        chat = message.get("chat", {})
        data = query.get("data", "")
        if not user_id or chat.get("type") != "private" or chat.get("id") != user_id:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        lang = self.db.language(user_id)
        if data not in {"language", "home", "check_subscription"} and user_id != self.admin_id:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=pick(lang, "Нет доступа.", "Access denied."))
            return
        allowed = data in {"broadcast", "broadcast_new", "language", "admin", "home", "check_subscription", "stats", "reset", "description", "subscription", "subscription_chat", "subscription_url", "subscription_toggle"} or re.fullmatch(r"broadcast_(?:send|cancel)_[0-9]+|(?:button|toggle|edit_url)_[1-5]|edit_label_[1-5]_(?:ru|en)", data)
        if not allowed:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        self.db.set_state(user_id)
        if data == "stats":
            count = self.db.count()
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=pick(lang, f"Пользователей: {count}", f"Users: {count}"), show_alert=True)
            return
        self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        if message.get("message_id"):
            try:
                self.api.call("deleteMessage", chat_id=user_id, message_id=message["message_id"])
            except TelegramError as error:
                if error.code not in {400, 403}:
                    raise
        if data == "broadcast":
            self.show_broadcasts()
        elif data == "broadcast_new":
            self.prompt(user_id, lang, "broadcast:compose", pick(lang, "Отправьте текст, фото, видео, документ, аудио или анимацию с подписью. Альбомы не поддерживаются; отправьте один файл.", "Send text, a photo, video, document, audio or animation with a caption. Send one file, not an album."))
        elif data.startswith("broadcast_send_"):
            job_id = int(data.rsplit("_", 1)[1])
            with self.db.connection as conn:
                if not conn.execute("SELECT 1 FROM broadcasts WHERE status='running'").fetchone():
                    changed = conn.execute("UPDATE broadcasts SET status='running' WHERE id=? AND status='draft'", (job_id,)).rowcount
                    if changed:
                        conn.execute("INSERT INTO broadcast_recipients(job_id,user_id) SELECT ?,telegram_id FROM users", (job_id,))
            self.show_broadcasts()
        elif data.startswith("broadcast_cancel_"):
            with self.db.connection as conn:
                conn.execute("UPDATE broadcasts SET status='cancelled' WHERE id=? AND status IN ('draft','running')", (int(data.rsplit("_", 1)[1]),))
            self.show_broadcasts()
        elif data == "language":
            self.db.set_language(user_id, "ru" if lang == "en" else "en")
            self.home(user_id)
        elif data in {"home", "check_subscription"}:
            self.home(user_id)
        elif data == "subscription":
            self.show_subscription(user_id)
        elif data == "subscription_chat":
            self.prompt(user_id, lang, "subscription:chat", pick(lang, "Добавьте бота администратором и отправьте @username канала/чата или его отрицательный числовой ID (для закрытого: -100...). После изменения подписку нужно включить снова.", "Add the bot as an administrator and send the chat/channel @username or negative numeric ID (private: -100...). Enable the subscription again after changing it."))
        elif data == "subscription_url":
            self.prompt(user_id, lang, "subscription:url", pick(lang, "Отправьте ссылку для вступления именно в выбранный чат: https://t.me/имя или https://t.me/+приглашение. После изменения подписку нужно включить снова.", "Send a join link for the selected chat: https://t.me/username or https://t.me/+invite. Enable the subscription again after changing it."))
        elif data == "subscription_toggle":
            if self.db.get("subscription_enabled") == "1":
                self.db.put("subscription_enabled", "0")
            else:
                try:
                    self.validate_subscription_chat(self.db.get("subscription_chat"))
                    if not valid_url(self.db.get("subscription_url")):
                        raise ValueError("url")
                except (ValueError, TelegramError):
                    self.send(user_id, pick(lang, "Сначала задайте чат и ссылку для вступления. Бот должен быть администратором чата. Подписка не включена.", "Set the chat and join link first. The bot must be a chat administrator. Subscription remains disabled."), self.subscription_keyboard(lang))
                    return
                self.db.put("subscription_enabled", "1")
            self.show_subscription(user_id)
        elif data == "admin":
            self.send(user_id, pick(lang, "Админ меню:", "Admin menu:"), self.admin_keyboard(lang))
        elif data == "reset":
            self.db.reset_count()
            self.send(user_id, pick(lang, "Статистика очищена. Пользователи будут учтены снова после /start.", "Statistics cleared. Users will be counted again after /start."), self.admin_keyboard(lang))
        elif data == "description":
            self.prompt(user_id, lang, f"desc:{lang}", pick(lang, "Отправьте новое описание на текущем языке:", "Send the new description in the current language:"))
        elif data.startswith("button_"):
            n = data[-1]
            self.send(user_id, pick(lang, f"Настройки кнопки {n}:", f"Button {n} settings:"), self.button_keyboard(lang, n))
        elif data.startswith("toggle_"):
            n = data[-1]
            self.db.put(f"button{n}_enabled", "0" if self.db.get(f"button{n}_enabled") == "1" else "1")
            self.send(user_id, pick(lang, "Состояние кнопки сохранено.", "Button state saved."), self.button_keyboard(lang, n))
        elif data.startswith("edit_url_"):
            self.prompt(user_id, lang, f"url:{data[-1]}", pick(lang, "Отправьте новый URL (http:// или https://):", "Send a new URL (http:// or https://):"))
        elif data.startswith("edit_label_"):
            _, _, n, lc = data.split("_")
            self.prompt(user_id, lang, f"label:{n}:{lc}", pick(lang, f"Отправьте текст кнопки {n} на языке {lc.upper()}:", f"Send the text for button {n} in {lc.upper()}:"))

    def prompt(self, user_id: int, lang: str, action: str, text: str) -> None:
        self.db.set_state(user_id, action)
        self.send(user_id, text + pick(lang, "\nОтмена: /cancel", "\nCancel: /cancel"), keyboard([[callback(pick(lang, "↩️ Отмена", "↩️ Cancel"), "admin")]]))

    def prepare_broadcast(self, message: dict) -> None:
        lang = self.db.language(self.admin_id)
        if message.get("media_group_id"):
            self.send(self.admin_id, pick(lang, "Пришлите один файл, без альбома.", "Send one file without an album."))
            return
        if "text" in message:
            payload = {"method": "sendMessage", "text": message["text"], "entities": message.get("entities", [])}
        else:
            payload = None
            for kind in ("photo", "video", "document", "audio", "animation"):
                if kind in message:
                    media = message[kind][-1] if kind == "photo" else message[kind]
                    payload = {"method": "send" + kind.title(), kind: media["file_id"],
                               "caption": message.get("caption", ""), "caption_entities": message.get("caption_entities", [])}
                    break
        if payload is None:
            self.send(self.admin_id, pick(lang, "Этот тип сообщения не поддерживается. Пришлите текст или файл.", "Unsupported message. Send text or a file."))
            return
        params = dict(payload)
        method = params.pop("method")
        self.api.call(method, chat_id=self.admin_id, **params)
        with self.db.connection as conn:
            conn.execute("UPDATE broadcasts SET status='cancelled' WHERE status='draft'")
            job_id = conn.execute("INSERT INTO broadcasts(payload) VALUES(?)", (json.dumps(payload, ensure_ascii=False),)).lastrowid
            conn.execute("DELETE FROM admin_state WHERE telegram_id=?", (self.admin_id,))
        count = self.db.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.send(self.admin_id, pick(lang, f"Предпросмотр выше. Получателей сейчас: {count}. Отправить всем пользователям?", f"Preview above. Current recipients: {count}. Send to all users?"), keyboard([
            [callback(pick(lang, "Отправить всем", "Send to all"), f"broadcast_send_{job_id}")],
            [callback(pick(lang, "Отменить", "Cancel"), f"broadcast_cancel_{job_id}")]]))

    def show_broadcasts(self) -> None:
        lang = self.db.language(self.admin_id)
        lines = [pick(lang, "Рассылки (последние 5):", "Broadcasts (latest 5):")]
        rows = [[callback(pick(lang, "Создать рассылку", "New broadcast"), "broadcast_new")]]
        labels = {"draft": "черновик", "running": "отправляется", "done": "завершена", "cancelled": "отменена"}
        for job_id, status in self.db.connection.execute("SELECT id,status FROM broadcasts ORDER BY id DESC LIMIT 5"):
            counts = dict(self.db.connection.execute("SELECT status,COUNT(*) FROM broadcast_recipients WHERE job_id=? GROUP BY status", (job_id,)))
            lines.append(f"#{job_id}: {labels.get(status, status) if lang == 'ru' else status}\n" + pick(lang,
                f"Доставлено: {counts.get('sent',0)}, ошибки: {counts.get('failed',0)}, неизвестно: {counts.get('unknown',0)}, осталось: {counts.get('pending',0)}",
                f"Sent: {counts.get('sent',0)}, failed: {counts.get('failed',0)}, unknown: {counts.get('unknown',0)}, pending: {counts.get('pending',0)}"))
            if status in {"draft", "running"}:
                rows.append([callback(pick(lang, f"Отменить #{job_id}", f"Cancel #{job_id}"), f"broadcast_cancel_{job_id}")])
            if status == "draft":
                rows.append([callback(pick(lang, f"Отправить #{job_id} всем", f"Send #{job_id} to all"), f"broadcast_send_{job_id}")])
        rows.extend([[callback(pick(lang, "Обновить", "Refresh"), "broadcast")], [callback(pick(lang, "Админ меню", "Admin menu"), "admin")]])
        self.send(self.admin_id, "\n\n".join(lines), keyboard(rows))

    def broadcast_tick(self, stop: threading.Event = STOP) -> None:
        conn = self.db.connection
        job = conn.execute("SELECT id,payload FROM broadcasts WHERE status='running' AND next_at<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
        if not job:
            return
        job_id, raw = job
        for _ in range(10):
            if stop.is_set():
                return
            recipient = conn.execute("SELECT user_id FROM broadcast_recipients WHERE job_id=? AND status='pending' ORDER BY user_id LIMIT 1", (job_id,)).fetchone()
            if recipient is None:
                with conn:
                    conn.execute("UPDATE broadcasts SET status='done' WHERE id=?", (job_id,))
                self.show_broadcasts()
                return
            user_id = recipient[0]
            with conn:
                conn.execute("UPDATE broadcast_recipients SET status='sending' WHERE job_id=? AND user_id=?", (job_id,user_id))
            params = json.loads(raw)
            method = params.pop("method")
            status = "sent"
            try:
                self.api.call(method, chat_id=user_id, _broadcast=True, **params)
            except TelegramError as error:
                if error.code == 429:
                    with conn:
                        conn.execute("UPDATE broadcast_recipients SET status='pending' WHERE job_id=? AND user_id=?", (job_id,user_id))
                        conn.execute("UPDATE broadcasts SET next_at=? WHERE id=?", (time.time()+max(1,error.retry_after),job_id))
                    return
                status = "unknown" if error.code >= 500 else "failed"
                if error.code in {401,404,409}:
                    raise
            with conn:
                conn.execute("UPDATE broadcast_recipients SET status=? WHERE job_id=? AND user_id=?", (status,job_id,user_id))
            stop.wait(0.1)

    def run(self, stop: threading.Event = STOP) -> None:
        offset = 0
        failures = 0
        while not stop.is_set():
            try:
                active = self.db.connection.execute("SELECT 1 FROM broadcasts WHERE status='running'").fetchone()
                updates = self.api.call("getUpdates", offset=offset, timeout=1 if active else 25, allowed_updates=["message", "callback_query"])
                failures = 0
                for update in updates:
                    if stop.is_set():
                        break
                    try:
                        self.handle_update(update)
                    except TelegramError as error:
                        if error.code in {401, 409}:
                            raise
                        LOG.warning("Update %s: Telegram %s: %s", update.get("update_id"), error.code, error)
                    offset = update["update_id"] + 1
                self.broadcast_tick(stop)
            except TelegramError as error:
                if error.code in {401, 404, 409}:
                    raise
                failures += 1
                delay = max(error.retry_after, min(60, 2 ** min(failures, 6)))
                LOG.warning("Polling: Telegram %s: %s; retry in %ss", error.code, error, delay)
                stop.wait(delay)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.version_info < (3, 10):
        LOG.error("Python 3.10 or newer is required.")
        return 1
    db = None
    try:
        config = read_config()
        token = config.get("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Set a valid BOT_TOKEN in config.env.")
        admin = config.get("ADMIN_ID", "").strip()
        if not re.fullmatch(r"[1-9][0-9]*", admin) or int(admin) > 2 ** 63 - 1:
            raise ValueError("Set ADMIN_ID to a positive numeric Telegram user ID in config.env.")
        if os.name != "nt":
            os.umask(0o077)
        db = Database(database_path(config.get("DATABASE_PATH", "").strip()))
        api = TelegramAPI(token)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: STOP.set())
        me = api.call("getMe")
        api.call("deleteWebhook", drop_pending_updates=False)
        LOG.info("Bot @%s started", me.get("username", ""))
        TelegramBot(api, db, int(admin)).run()
        return 0
    except (OSError, ValueError, sqlite3.Error, TelegramError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
