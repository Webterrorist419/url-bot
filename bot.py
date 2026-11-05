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
LANGUAGES = ("ru", "en", "zh", "fa")
LANGUAGE_PATTERN = "|".join(LANGUAGES)
LANGUAGE_PLACEHOLDERS = ",".join("?" for _ in LANGUAGES)
LANGUAGE_LABELS = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "zh": "🇨🇳 中文", "fa": "🇮🇷 فارسی"}
LANGUAGE_NAMES = {
    "ru": {"ru": "Русский", "en": "Английский", "zh": "Китайский", "fa": "Персидский"},
    "en": {"ru": "Russian", "en": "English", "zh": "Chinese", "fa": "Persian"},
    "zh": {"ru": "俄语", "en": "英语", "zh": "中文", "fa": "波斯语"},
    "fa": {"ru": "روسی", "en": "انگلیسی", "zh": "چینی", "fa": "فارسی"},
}
BUTTON_STYLES = ("primary", "success", "danger")


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


def pick(lang: str, ru: str, en: str, zh: str, fa: str) -> str:
    return {"ru": ru, "en": en, "zh": zh, "fa": fa}.get(lang, ru)


def language_name(lang: str, target: str) -> str:
    return LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["ru"])[target]


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
                next_at REAL NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                lang TEXT NOT NULL DEFAULT '')""")
            broadcast_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(broadcasts)")}
            if "lang" not in broadcast_columns:
                self.connection.execute("ALTER TABLE broadcasts ADD COLUMN lang TEXT NOT NULL DEFAULT ''")
            # Old running jobs keep their recipient snapshot; old drafts need a new language-specific preview.
            self.connection.execute("UPDATE broadcasts SET status='cancelled' WHERE lang='' AND status='draft'")
            self.connection.execute("DELETE FROM admin_state WHERE action='broadcast:compose'")
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
                "description_zh": "欢迎！请选择链接。",
                "description_fa": "خوش آمدید! یک پیوند را انتخاب کنید.",
            }
            for n in range(1, BUTTON_COUNT + 1):
                defaults.update({
                    f"button{n}_label_ru": f"Ссылка {n}",
                    f"button{n}_label_en": f"Link {n}",
                    f"button{n}_label_zh": f"链接 {n}",
                    f"button{n}_label_fa": f"پیوند {n}",
                    f"button{n}_url": "https://example.com",
                    f"button{n}_enabled": "1",
                    f"button{n}_style": "",
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
        return row[0] if row and row[0] in LANGUAGES else "ru"

    def has_language(self, user_id: int) -> bool:
        return self.connection.execute(f"SELECT 1 FROM users WHERE telegram_id=? AND lang IN ({LANGUAGE_PLACEHOLDERS})", (user_id, *LANGUAGES)).fetchone() is not None

    def set_language(self, user_id: int, lang: str) -> None:
        if lang not in LANGUAGES:
            raise ValueError("Unsupported language")
        with self.connection:
            self.connection.execute("INSERT INTO users(telegram_id,lang,counted) VALUES(?,?,0) ON CONFLICT(telegram_id) DO UPDATE SET lang=excluded.lang", (user_id, lang))

    def record_start(self, user_id: int) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO users(telegram_id,counted) VALUES(?,1) ON CONFLICT(telegram_id) DO UPDATE SET counted=1", (user_id,))

    def count(self, lang: str | None = None) -> int:
        return self.connection.execute(f"SELECT COUNT(*) FROM users WHERE counted=1 AND lang IN ({LANGUAGE_PLACEHOLDERS}) AND (? IS NULL OR lang=?)", (*LANGUAGES, lang, lang)).fetchone()[0]

    def audience_count(self, lang: str | None = None) -> int:
        return self.connection.execute(f"SELECT COUNT(*) FROM users WHERE lang IN ({LANGUAGE_PLACEHOLDERS}) AND (? IS NULL OR lang=?)", (*LANGUAGES, lang, lang)).fetchone()[0]

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
            headers={"Content-Type": "application/json", "User-Agent": "URLBot/1.4.0"},
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


def callback(text: str, data: str, style: str = "") -> dict:
    button = {"text": text, "callback_data": data}
    if style in BUTTON_STYLES:
        button["style"] = style
    return button


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
                button = {"text": self.db.get(f"button{n}_label_{lang}"), "url": url}
                style = self.db.get(f"button{n}_style")
                if style in BUTTON_STYLES:
                    button["style"] = style
                rows.append([button])
        rows.append([callback(pick(lang, "🌐 Язык: RU / EN / ZH / FA", "🌐 Language: RU / EN / ZH / FA", '🌐 语言：RU / EN / ZH / FA', '🌐 زبان: RU / EN / ZH / FA'), "language")])
        if admin:
            rows.append([callback(pick(lang, "⚙️ Админ меню", "⚙️ Admin menu", '⚙️ 管理菜单', '⚙️ منوی مدیریت'), "admin")])
        return keyboard(rows)

    @staticmethod
    def admin_keyboard(lang: str) -> dict:
        rows = [[callback(pick(lang, f"Ссылка {n}: URL / текст / цвет / вкл-выкл", f"Link {n}: URL / label / color / on-off", f'链接 {n}：网址／文字／颜色／开关', f'پیوند {n}: نشانی / متن / رنگ / فعال\u200cسازی'), f"button_{n}")] for n in range(1, BUTTON_COUNT + 1)]
        rows.extend([
            [callback(pick(lang, "📣 Рассылки", "📣 Broadcasts", '📣 群发消息', '📣 ارسال همگانی'), "broadcast")],
            [callback(pick(lang, "🔐 Обязательная подписка", "🔐 Required subscription", '🔐 强制加入', '🔐 عضویت اجباری'), "subscription")],
            [callback(pick(lang, "Описание бота", "Bot description", '机器人介绍', 'توضیحات ربات'), "description")],
            [callback(pick(lang, "📊 Статистика", "📊 Statistics", '📊 统计', '📊 آمار'), "stats")],
            [callback(pick(lang, "🗑 Сбросить статистику", "🗑 Reset statistics", '🗑 重置统计', '🗑 بازنشانی آمار'), "reset"), callback(pick(lang, "↩️ В меню", "↩️ Main menu", '↩️ 主菜单', '↩️ منوی اصلی'), "home")],
            [callback(pick(lang, "🌐 Язык: RU / EN / ZH / FA", "🌐 Language: RU / EN / ZH / FA", '🌐 语言：RU / EN / ZH / FA', '🌐 زبان: RU / EN / ZH / FA'), "language")],
        ])
        return keyboard(rows)

    def button_keyboard(self, lang: str, n: str) -> dict:
        enabled = self.db.get(f"button{n}_enabled") == "1"
        return keyboard([
            [callback(pick(lang, "Изменить URL", "Change URL", '修改网址', 'تغییر نشانی'), f"edit_url_{n}")],
            *[[callback(pick(lang, f"Текст кнопки ({language_name(lang, target)})", f"Button text ({language_name(lang, target)})", f"按钮文字（{language_name(lang, target)}）", f"متن دکمه ({language_name(lang, target)})"), f"edit_label_{n}_{target}")] for target in LANGUAGES],
            [callback(pick(lang, "🎨 Цвет кнопки", "🎨 Button color", '🎨 按钮颜色', '🎨 رنگ دکمه'), f"color_{n}")],
            [callback(pick(lang, "🔴 Выключить", "🔴 Disable", '🔴 停用', '🔴 غیرفعال کردن') if enabled else pick(lang, "🟢 Включить", "🟢 Enable", '🟢 启用', '🟢 فعال کردن'), f"toggle_{n}")],
            [callback(pick(lang, "↩️ Админ меню", "↩️ Admin menu", '↩️ 管理菜单', '↩️ منوی مدیریت'), "admin")],
            [callback(pick(lang, "🌐 Язык: RU / EN / ZH / FA", "🌐 Language: RU / EN / ZH / FA", '🌐 语言：RU / EN / ZH / FA', '🌐 زبان: RU / EN / ZH / FA'), "language")],
        ])

    def show_button(self, user_id: int, n: str, notice: str = "") -> None:
        lang = self.db.language(user_id)
        style = self.db.get(f"button{n}_style")
        colors = {"primary": pick(lang, "синий", "blue", '蓝色', 'آبی'), "success": pick(lang, "зелёный", "green", '绿色', 'سبز'), "danger": pick(lang, "красный", "red", '红色', 'قرمز')}
        labels = "\n".join(f"{code.upper()}: {self.db.get(f'button{n}_label_{code}')}" for code in LANGUAGES)
        color = colors.get(style, pick(lang, "стандартный", "default", "默认", "پیش‌فرض"))
        text = pick(lang,
            f"Настройки кнопки {n}:\nURL: {self.db.get(f'button{n}_url')}\n{labels}\nЦвет: {color}",
            f"Button {n} settings:\nURL: {self.db.get(f'button{n}_url')}\n{labels}\nColor: {color}",
            f"按钮 {n} 设置：\nURL: {self.db.get(f'button{n}_url')}\n{labels}\n颜色：{color}",
            f"تنظیمات دکمه {n}:\nURL: {self.db.get(f'button{n}_url')}\n{labels}\nرنگ: {color}")
        self.send(user_id, f"{notice}\n\n{text}" if notice else text, self.button_keyboard(lang, n))

    def show_button_colors(self, user_id: int, n: str) -> None:
        lang = self.db.language(user_id)
        current = self.db.get(f"button{n}_style") or "default"
        options = [("default", "Стандартный", "Default", "默认", "پیش‌فرض"), ("primary", "🔵 Синий", "🔵 Blue", "🔵 蓝色", "🔵 آبی"),
                   ("success", "🟢 Зелёный", "🟢 Green", "🟢 绿色", "🟢 سبز"), ("danger", "🔴 Красный", "🔴 Red", "🔴 红色", "🔴 قرمز")]
        rows = [[callback(("✓ " if style == current else "") + pick(lang, ru, en, zh, fa), f"set_color_{n}_{style}", style)] for style, ru, en, zh, fa in options]
        rows.append([callback(pick(lang, "↩️ К настройкам кнопки", "↩️ Button settings", '↩️ 按钮设置', '↩️ تنظیمات دکمه'), f"button_{n}")])
        self.send(user_id, pick(lang, f"Выберите цвет кнопки {n}:", f"Choose a color for button {n}:", f'请选择按钮 {n} 的颜色：', f'رنگ دکمه {n} را انتخاب کنید:'), keyboard(rows))

    def show_languages(self, user_id: int) -> None:
        self.db.set_state(user_id, "language:choose")
        choices = [callback(LANGUAGE_LABELS[code], f"language_{code}") for code in LANGUAGES]
        self.send(user_id, "Выберите язык / Choose your language / 请选择语言 / زبان را انتخاب کنید:", keyboard([choices[:2], choices[2:]]))

    def send(self, user_id: int, text: str, markup: dict | None = None) -> None:
        params = {"chat_id": user_id, "text": text}
        if markup is not None:
            params["reply_markup"] = markup
        self.api.call("sendMessage", **params)

    def home(self, user_id: int) -> None:
        if not self.db.has_language(user_id) or self.db.state(user_id) == "language:choose":
            self.show_languages(user_id)
            return
        lang = self.db.language(user_id)
        if not self.has_access(user_id):
            return
        self.send(user_id, self.db.get(f"description_{lang}"), self.main_keyboard(lang, user_id == self.admin_id))

    def subscription_keyboard(self, lang: str) -> dict:
        enabled = self.db.get("subscription_enabled") == "1"
        return keyboard([
            [callback(pick(lang, "Чат / канал", "Chat / channel", '群组／频道', 'گروه / کانال'), "subscription_chat")],
            [callback(pick(lang, "Ссылка для вступления", "Join link", '加入链接', 'پیوند عضویت'), "subscription_url")],
            [callback(pick(lang, "🔴 Выключить", "🔴 Disable", '🔴 停用', '🔴 غیرفعال کردن') if enabled else pick(lang, "🟢 Включить", "🟢 Enable", '🟢 启用', '🟢 فعال کردن'), "subscription_toggle")],
            [callback(pick(lang, "↩️ Админ меню", "↩️ Admin menu", '↩️ 管理菜单', '↩️ منوی مدیریت'), "admin")],
        ])

    def show_subscription(self, user_id: int) -> None:
        lang = self.db.language(user_id)
        enabled = self.db.get("subscription_enabled") == "1"
        status = pick(lang, "включена", "enabled", "已启用", "فعال") if enabled else pick(lang, "выключена", "disabled", "已停用", "غیرفعال")
        chat = self.db.get("subscription_chat") or pick(lang, "не задан", "not set", "未设置", "تنظیم نشده")
        url = self.db.get("subscription_url") or pick(lang, "не задана", "not set", "未设置", "تنظیم نشده")
        self.send(user_id, pick(lang,
            f"Обязательная подписка: {status}\nЧат / канал: {chat}\nСсылка: {url}\n\nДобавьте бота администратором в выбранный чат или канал.",
            f"Required subscription: {status}\nChat / channel: {chat}\nJoin link: {url}\n\nAdd the bot as an administrator in the selected chat or channel.",
            f"强制加入：{status}\n群组／频道：{chat}\n加入链接：{url}\n\n请将机器人设为所选群组或频道的管理员。",
            f"عضویت اجباری: {status}\nگروه / کانال: {chat}\nپیوند عضویت: {url}\n\nربات را مدیر گروه یا کانال انتخابی کنید."), self.subscription_keyboard(lang))

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
            rows.append([{"text": pick(lang, "📢 Подписаться", "📢 Subscribe", '📢 加入', '📢 عضویت'), "url": url}])
        rows.extend([
            [callback(pick(lang, "✅ Проверить подписку", "✅ Check subscription", '✅ 验证成员资格', '✅ بررسی عضویت'), "check_subscription")],
            [callback(pick(lang, "🌐 Язык: RU / EN / ZH / FA", "🌐 Language: RU / EN / ZH / FA", '🌐 语言：RU / EN / ZH / FA', '🌐 زبان: RU / EN / ZH / FA'), "language")],
        ])
        text = pick(lang, "Не удалось проверить подписку. Повторите попытку позже.", "Could not verify your subscription. Please try again later.", "无法验证成员资格，请稍后重试。", "بررسی عضویت ممکن نشد. لطفاً بعداً دوباره تلاش کنید.") if failed else pick(lang, "Чтобы открыть меню бота, сначала вступите в чат или подпишитесь на канал, затем нажмите «Проверить подписку».", "Join the chat or subscribe to the channel, then press Check subscription to open the bot menu.", "请先加入群组或订阅频道，再点击“验证成员资格”打开机器人菜单。", "برای باز کردن منوی ربات، ابتدا عضو گروه یا کانال شوید و سپس «بررسی عضویت» را بزنید.")
        self.send(user_id, text, keyboard(rows))
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
                self.send(chat["id"], "Ошибка базы данных. Повторите действие позже. / Database error. Please try again later. / 数据库错误，请稍后重试。 / خطای پایگاه داده. لطفاً بعداً دوباره تلاش کنید.")

    def on_message(self, message: dict) -> None:
        sender, chat = message.get("from", {}), message.get("chat", {})
        text = message.get("text")
        if chat.get("type") != "private" or not sender.get("id") or sender.get("id") != chat.get("id"):
            return
        user_id = sender["id"]
        lang = self.db.language(user_id)
        action = self.db.state(user_id)
        if user_id == self.admin_id and re.fullmatch(rf"broadcast:compose:({LANGUAGE_PATTERN})", action) and not (isinstance(text, str) and text.startswith("/")):
            self.prepare_broadcast(message)
            return
        if not isinstance(text, str):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
        if command == "/start":
            self.show_languages(user_id)
            return
        if not self.db.has_language(user_id) or action == "language:choose":
            self.show_languages(user_id)
            return
        if user_id != self.admin_id:
            self.has_access(user_id)
            return
        if command == "/cancel":
            self.db.set_state(user_id)
            self.return_from_edit(user_id, action, pick(lang, "Ввод отменён.", "Edit cancelled.", '已取消编辑。', 'ویرایش لغو شد.'))
            return
        if command.startswith("/"):
            self.send(user_id, pick(lang, "Для отмены ввода: /cancel. Главное меню: /start.", "Cancel editing: /cancel. Main menu: /start.", '取消编辑：/cancel。主菜单：/start。', 'لغو ویرایش: /cancel. منوی اصلی: /start.'))
            return
        action = self.db.state(user_id)
        if not action:
            self.send(user_id, pick(lang, "Откройте админ меню кнопкой ниже.", "Open the admin menu below.", '请点击下方按钮打开管理菜单。', 'با دکمه زیر منوی مدیریت را باز کنید.'), self.main_keyboard(lang, True))
            return
        value = text.strip()
        if action == "subscription:chat":
            try:
                value = self.validate_subscription_chat(value)
            except (ValueError, TelegramError):
                self.send(user_id, pick(lang, "Не удалось проверить чат. Отправьте @username или отрицательный ID чата (например -1001234567890) и добавьте бота его администратором. Повторите ввод или /cancel.", "Could not verify chat. Send @username or a negative chat ID (e.g. -1001234567890) and add the bot as its administrator. Try again or /cancel.", '无法验证群组。请发送 @username 或负数群组 ID（例如 -1001234567890），并将机器人设为管理员。请重试或输入 /cancel。', 'بررسی گروه ممکن نشد. @username یا شناسه منفی گروه (مانند -1001234567890) را بفرستید و ربات را مدیر کنید. دوباره تلاش کنید یا /cancel را بزنید.'))
                return
            # Changing the target always requires explicitly enabling the gate again.
            self.db.put("subscription_enabled", "0")
            key = "subscription_chat"
        elif action == "subscription:url":
            if not valid_url(value) or urlsplit(value).scheme != "https" or urlsplit(value).netloc.lower() != "t.me" or not re.fullmatch(r"/(?:[A-Za-z][A-Za-z0-9_]{4,31}|\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)/?", urlsplit(value).path) or urlsplit(value).query or urlsplit(value).fragment:
                self.send(user_id, pick(lang, "Нужна ссылка https://t.me/имя или https://t.me/+приглашение. Повторите ввод или /cancel.", "Enter https://t.me/username or https://t.me/+invite. Try again or /cancel.", '请输入 https://t.me/username 或 https://t.me/+invite 格式的链接。请重试或输入 /cancel。', 'پیوندی به شکل https://t.me/username یا https://t.me/+invite وارد کنید. دوباره تلاش کنید یا /cancel را بزنید.'))
                return
            self.db.put("subscription_enabled", "0")
            key = "subscription_url"
        elif re.fullmatch(r"url:[1-5]", action):
            if not valid_url(value):
                self.send(user_id, pick(lang, "Нужна полная ссылка http:// или https://. Повторите ввод или /cancel.", "Enter a complete http:// or https:// URL, or /cancel.", '请输入以 http:// 或 https:// 开头的完整链接，或输入 /cancel。', 'نشانی کامل با http:// یا https:// وارد کنید یا /cancel را بزنید.'))
                return
            key = f"button{action[-1]}_url"
        elif match := re.fullmatch(rf"label:([1-5]):({LANGUAGE_PATTERN})", action):
            if not 1 <= len(value) <= 64:
                self.send(user_id, pick(lang, "Текст кнопки: от 1 до 64 символов. Повторите ввод или /cancel.", "Button text must contain 1–64 characters. Try again or /cancel.", '按钮文字须为 1–64 个字符。请重试或输入 /cancel。', 'متن دکمه باید ۱ تا ۶۴ نویسه باشد. دوباره تلاش کنید یا /cancel را بزنید.'))
                return
            key = f"button{match[1]}_label_{match[2]}"
        elif match := re.fullmatch(rf"desc:({LANGUAGE_PATTERN})", action):
            if not 1 <= len(value.encode("utf-16-le")) // 2 <= 4096:
                self.send(user_id, pick(lang, "Описание: от 1 до 4096 символов. Повторите ввод или /cancel.", "Description must contain 1–4096 characters. Try again or /cancel.", '介绍须为 1–4096 个字符。请重试或输入 /cancel。', 'توضیحات باید ۱ تا ۴۰۹۶ نویسه باشد. دوباره تلاش کنید یا /cancel را بزنید.'))
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
        self.return_from_edit(user_id, action, pick(lang, "Сохранено.", "Saved.", '已保存。', 'ذخیره شد.'))

    def on_callback(self, query: dict) -> None:
        user_id = query.get("from", {}).get("id")
        message = query.get("message", {})
        chat = message.get("chat", {})
        data = query.get("data", "")
        if not user_id or chat.get("type") != "private" or chat.get("id") != user_id:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        lang = self.db.language(user_id)
        language_actions = {"language", *(f"language_{code}" for code in LANGUAGES)}
        if data not in language_actions | {"home", "check_subscription"} and user_id != self.admin_id:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=pick(lang, "Нет доступа.", "Access denied.", '无访问权限。', 'دسترسی ندارید.'))
            return
        allowed = data in language_actions | {"broadcast", "broadcast_new", "admin", "home", "check_subscription", "stats", "reset", "description", "subscription", "subscription_chat", "subscription_url", "subscription_toggle"} or re.fullmatch(rf"broadcast_(?:(?:send|cancel)_[0-9]+|(?:new_)?(?:{LANGUAGE_PATTERN}))|(?:button|toggle|edit_url|color)_[1-5]|edit_label_[1-5]_(?:{LANGUAGE_PATTERN})|set_color_[1-5]_(?:default|primary|success|danger)", data)
        if not allowed:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        if data not in language_actions and (not self.db.has_language(user_id) or self.db.state(user_id) == "language:choose"):
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            self.show_languages(user_id)
            return
        self.db.set_state(user_id)
        self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        if message.get("message_id"):
            try:
                self.api.call("deleteMessage", chat_id=user_id, message_id=message["message_id"])
            except TelegramError as error:
                if error.code not in {400, 403}:
                    raise
        if data in {"broadcast", "broadcast_new"}:
            self.show_broadcasts()
        elif data in {f"broadcast_{code}" for code in LANGUAGES}:
            self.show_broadcasts(data[-2:])
        elif data in {f"broadcast_new_{code}" for code in LANGUAGES}:
            target = data[-2:]
            self.prompt(user_id, lang, f"broadcast:compose:{target}", pick(lang,
                f"Рассылка для {target.upper()}. Отправьте текст, фото, видео, документ, аудио или анимацию с подписью. Альбомы не поддерживаются; отправьте один файл.",
                f"Broadcast to {target.upper()}. Send text, a photo, video, document, audio or animation with a caption. Send one file, not an album.", f'向 {target.upper()} 用户群发。请发送文字、照片、视频、文档、音频或动画，可附说明。暂不支持相册，请发送单个文件。', f'ارسال همگانی برای {target.upper()}. متن، عکس، ویدئو، سند، صدا یا پویانمایی با توضیح بفرستید. آلبوم پشتیبانی نمی\u200cشود؛ یک فایل بفرستید.'))
        elif data.startswith("broadcast_send_"):
            job_id = int(data.rsplit("_", 1)[1])
            job = self.db.connection.execute("SELECT lang,status FROM broadcasts WHERE id=?", (job_id,)).fetchone()
            if not job or job[0] not in LANGUAGES:
                self.show_broadcasts()
                return
            target, status = job
            with self.db.connection as conn:
                busy = conn.execute("SELECT 1 FROM broadcasts WHERE status='running'").fetchone()
                if not busy:
                    changed = conn.execute("UPDATE broadcasts SET status='running' WHERE id=? AND status='draft'", (job_id,)).rowcount
                    if changed:
                        conn.execute("INSERT INTO broadcast_recipients(job_id,user_id) SELECT ?,telegram_id FROM users WHERE lang=?", (job_id, target))
            if busy and status == "draft":
                self.send(user_id, pick(lang, "Другая рассылка ещё выполняется. Черновик сохранён; отправьте его после завершения текущей рассылки.", "Another broadcast is still running. Your draft is saved; send it after the current broadcast finishes.", '另一条群发仍在进行。草稿已保存，请在当前群发完成后发送。', 'ارسال دیگری در حال انجام است. پیش\u200cنویس ذخیره شد؛ پس از پایان ارسال جاری آن را بفرستید.'))
            self.show_broadcasts(target)
        elif data.startswith("broadcast_cancel_"):
            job_id = int(data.rsplit("_", 1)[1])
            job = self.db.connection.execute("SELECT lang FROM broadcasts WHERE id=?", (job_id,)).fetchone()
            with self.db.connection as conn:
                conn.execute("UPDATE broadcasts SET status='cancelled' WHERE id=? AND status IN ('draft','running')", (job_id,))
            self.show_broadcasts(job[0] if job and job[0] in LANGUAGES else None)
        elif data == "language":
            self.show_languages(user_id)
        elif data in {f"language_{code}" for code in LANGUAGES}:
            self.db.set_language(user_id, data[-2:])
            self.db.record_start(user_id)
            self.home(user_id)
        elif data in {"home", "check_subscription"}:
            self.home(user_id)
        elif data == "subscription":
            self.show_subscription(user_id)
        elif data == "subscription_chat":
            self.prompt(user_id, lang, "subscription:chat", pick(lang, "Добавьте бота администратором и отправьте @username канала/чата или его отрицательный числовой ID (для закрытого: -100...). После изменения подписку нужно включить снова.", "Add the bot as an administrator and send the chat/channel @username or negative numeric ID (private: -100...). Enable the subscription again after changing it.", '请将机器人设为管理员，并发送频道／群组的 @username 或负数 ID（私密频道通常为 -100...）。修改后需重新启用强制加入。', 'ربات را مدیر کنید و @username گروه/کانال یا شناسه منفی آن (برای کانال خصوصی: -100...) را بفرستید. پس از تغییر، عضویت اجباری را دوباره فعال کنید.'))
        elif data == "subscription_url":
            self.prompt(user_id, lang, "subscription:url", pick(lang, "Отправьте ссылку для вступления именно в выбранный чат: https://t.me/имя или https://t.me/+приглашение. После изменения подписку нужно включить снова.", "Send a join link for the selected chat: https://t.me/username or https://t.me/+invite. Enable the subscription again after changing it.", '请发送所选群组的加入链接：https://t.me/username 或 https://t.me/+invite。修改后需重新启用强制加入。', 'پیوند عضویت همان گروه انتخاب\u200cشده را بفرستید: https://t.me/username یا https://t.me/+invite. پس از تغییر، عضویت اجباری را دوباره فعال کنید.'))
        elif data == "subscription_toggle":
            if self.db.get("subscription_enabled") == "1":
                self.db.put("subscription_enabled", "0")
            else:
                try:
                    self.validate_subscription_chat(self.db.get("subscription_chat"))
                    if not valid_url(self.db.get("subscription_url")):
                        raise ValueError("url")
                except (ValueError, TelegramError):
                    self.send(user_id, pick(lang, "Сначала задайте чат и ссылку для вступления. Бот должен быть администратором чата. Подписка не включена.", "Set the chat and join link first. The bot must be a chat administrator. Subscription remains disabled.", '请先设置群组和加入链接，并将机器人设为管理员。强制加入尚未启用。', 'ابتدا گروه و پیوند عضویت را تعیین کنید. ربات باید مدیر گروه باشد. عضویت اجباری فعال نشده است.'), self.subscription_keyboard(lang))
                    return
                self.db.put("subscription_enabled", "1")
            self.show_subscription(user_id)
        elif data == "admin":
            self.send(user_id, pick(lang, "Админ меню:", "Admin menu:", '管理菜单：', 'منوی مدیریت:'), self.admin_keyboard(lang))
        elif data == "stats":
            self.show_stats(user_id)
        elif data == "reset":
            self.db.reset_count()
            self.send(user_id, pick(lang, "Статистика очищена. Пользователи будут учтены снова после выбора языка.", "Statistics cleared. Users will be counted again after choosing a language.", '统计已重置。用户下次选择语言后将重新计入统计。', 'آمار پاک شد. کاربران پس از انتخاب دوباره زبان در آمار شمرده می\u200cشوند.'))
            self.show_stats(user_id)
        elif data == "description":
            self.prompt(user_id, lang, f"desc:{lang}", pick(lang, "Отправьте новое описание на текущем языке:", "Send the new description in the current language:", '请用当前语言发送新的机器人介绍：', 'توضیحات جدید را به زبان فعلی بفرستید:'))
        elif data.startswith("button_"):
            self.show_button(user_id, data[-1])
        elif data.startswith("toggle_"):
            n = data[-1]
            self.db.put(f"button{n}_enabled", "0" if self.db.get(f"button{n}_enabled") == "1" else "1")
            self.show_button(user_id, n, pick(lang, "Состояние кнопки сохранено.", "Button state saved.", '按钮状态已保存。', 'وضعیت دکمه ذخیره شد.'))
        elif data.startswith("color_"):
            self.show_button_colors(user_id, data[-1])
        elif data.startswith("set_color_"):
            _, _, n, style = data.split("_")
            self.db.put(f"button{n}_style", "" if style == "default" else style)
            self.show_button(user_id, n, pick(lang, "Цвет кнопки сохранён.", "Button color saved.", '按钮颜色已保存。', 'رنگ دکمه ذخیره شد.'))
        elif data.startswith("edit_url_"):
            self.prompt(user_id, lang, f"url:{data[-1]}", pick(lang, "Отправьте новый URL (http:// или https://):", "Send a new URL (http:// or https://):", '请发送新网址（http:// 或 https://）：', 'نشانی جدید را بفرستید (http:// یا https://):'))
        elif data.startswith("edit_label_"):
            _, _, n, lc = data.split("_")
            self.prompt(user_id, lang, f"label:{n}:{lc}", pick(lang, f"Отправьте текст кнопки {n} на языке {lc.upper()}:", f"Send the text for button {n} in {lc.upper()}:", f'请用 {lc.upper()} 发送按钮 {n} 的文字：', f'متن دکمه {n} را به زبان {lc.upper()} بفرستید:'))

    @staticmethod
    def edit_destination(action: str) -> str:
        if match := re.fullmatch(rf"(?:url|label):([1-5])(?::(?:{LANGUAGE_PATTERN}))?", action):
            return f"button_{match[1]}"
        if action.startswith("subscription:"):
            return "subscription"
        if match := re.fullmatch(rf"broadcast:compose:({LANGUAGE_PATTERN})", action):
            return f"broadcast_{match[1]}"
        return "admin"

    def return_from_edit(self, user_id: int, action: str, notice: str) -> None:
        destination = self.edit_destination(action)
        if destination.startswith("button_"):
            self.show_button(user_id, destination[-1], notice)
        elif destination == "subscription":
            self.show_subscription(user_id)
        elif destination.startswith("broadcast_"):
            self.show_broadcasts(destination[-2:])
        else:
            self.send(user_id, notice, self.admin_keyboard(self.db.language(user_id)))

    def show_stats(self, user_id: int) -> None:
        lang = self.db.language(user_id)
        counts = "\n".join(f"{LANGUAGE_LABELS[code].split()[0]} {code.upper()}: {self.db.count(code)}" for code in LANGUAGES)
        audiences = "\n".join(f"{LANGUAGE_LABELS[code].split()[0]} {code.upper()}: {self.db.audience_count(code)}" for code in LANGUAGES)
        text = pick(lang,
            f"📊 Статистика\n\nПользователей после сброса: {self.db.count()}\n{counts}\n\nПолучатели рассылок: {self.db.audience_count()}\n{audiences}\n\nУчитываются уникальные пользователи, выбравшие язык. При смене языка пользователь переносится в соответствующую группу.",
            f"📊 Statistics\n\nUsers since reset: {self.db.count()}\n{counts}\n\nBroadcast recipients: {self.db.audience_count()}\n{audiences}\n\nUnique users who chose a language are counted. Changing language moves the user to the corresponding group.",
            f"📊 统计\n\n自上次重置以来的用户：{self.db.count()}\n{counts}\n\n群发接收者：{self.db.audience_count()}\n{audiences}\n\n统计已选择语言的独立用户。更改语言后，用户会移至对应语言分组。",
            f"📊 آمار\n\nکاربران از آخرین بازنشانی: {self.db.count()}\n{counts}\n\nگیرندگان پیام همگانی: {self.db.audience_count()}\n{audiences}\n\nکاربران یکتایی که زبان انتخاب کرده‌اند شمرده می‌شوند. تغییر زبان، کاربر را به گروه زبان مربوط منتقل می‌کند.")
        self.send(user_id, text, keyboard([
            [callback(pick(lang, "Обновить", "Refresh", '刷新', 'تازه\u200cسازی'), "stats")],
            [callback(pick(lang, "🗑 Сбросить статистику", "🗑 Reset statistics", '🗑 重置统计', '🗑 بازنشانی آمار'), "reset")],
            [callback(pick(lang, "↩️ Админ меню", "↩️ Admin menu", '↩️ 管理菜单', '↩️ منوی مدیریت'), "admin")],
        ]))

    def prompt(self, user_id: int, lang: str, action: str, text: str) -> None:
        self.db.set_state(user_id, action)
        self.send(user_id, text + pick(lang, "\nОтмена: /cancel", "\nCancel: /cancel", '\n取消：/cancel', '\nلغو: /cancel'), keyboard([[callback(pick(lang, "↩️ Отмена", "↩️ Cancel", '↩️ 取消', '↩️ لغو'), self.edit_destination(action))]]))

    def prepare_broadcast(self, message: dict) -> None:
        lang = self.db.language(self.admin_id)
        match = re.fullmatch(rf"broadcast:compose:({LANGUAGE_PATTERN})", self.db.state(self.admin_id))
        if not match:
            self.show_broadcasts()
            return
        target = match[1]
        if message.get("media_group_id"):
            self.send(self.admin_id, pick(lang, "Пришлите один файл, без альбома.", "Send one file without an album.", '请发送单个文件，不要发送相册。', 'فقط یک فایل بفرستید، نه آلبوم.'))
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
            self.send(self.admin_id, pick(lang, "Этот тип сообщения не поддерживается. Пришлите текст или файл.", "Unsupported message. Send text or a file.", '暂不支持此类消息，请发送文字或文件。', 'این نوع پیام پشتیبانی نمی\u200cشود. متن یا فایل بفرستید.'))
            return
        params = dict(payload)
        method = params.pop("method")
        self.api.call(method, chat_id=self.admin_id, **params)
        with self.db.connection as conn:
            conn.execute("UPDATE broadcasts SET status='cancelled' WHERE status='draft' AND lang=?", (target,))
            job_id = conn.execute("INSERT INTO broadcasts(payload,lang) VALUES(?,?)", (json.dumps(payload, ensure_ascii=False), target)).lastrowid
            conn.execute("DELETE FROM admin_state WHERE telegram_id=?", (self.admin_id,))
        count = self.db.audience_count(target)
        self.send(self.admin_id, pick(lang,
            f"Предпросмотр выше. Язык рассылки: {target.upper()}. Получателей сейчас: {count}. Отправить пользователям {target.upper()}?",
            f"Preview above. Broadcast language: {target.upper()}. Current recipients: {count}. Send to {target.upper()} users?", f'预览如上。群发语言：{target.upper()}。当前接收者：{count}。发送给 {target.upper()} 用户吗？', f'پیش\u200cنمایش در بالا است. زبان ارسال: {target.upper()}. گیرندگان فعلی: {count}. برای کاربران {target.upper()} ارسال شود؟'), keyboard([
            [callback(pick(lang, f"Отправить для {target.upper()}", f"Send to {target.upper()}", f'发送给 {target.upper()} 用户', f'ارسال برای {target.upper()}'), f"broadcast_send_{job_id}")],
            [callback(pick(lang, "Отменить", "Cancel", '取消', 'لغو'), f"broadcast_cancel_{job_id}")]]))

    def show_broadcasts(self, target: str | None = None) -> None:
        lang = self.db.language(self.admin_id)
        if target in LANGUAGES:
            lines = [pick(lang, f"Рассылки {target.upper()} (последние 5):\nПолучателей сейчас: {self.db.audience_count(target)}", f"{target.upper()} broadcasts (latest 5):\nCurrent recipients: {self.db.audience_count(target)}", f'{target.upper()} 群发（最近 5 条）：\n当前接收者：{self.db.audience_count(target)}', f'ارسال\u200cهای {target.upper()} (۵ مورد آخر):\nگیرندگان فعلی: {self.db.audience_count(target)}')]
            rows = [[callback(pick(lang, f"Создать рассылку для {target.upper()}", f"New broadcast for {target.upper()}", f'创建 {target.upper()} 群发', f'ایجاد ارسال برای {target.upper()}'), f"broadcast_new_{target}")]]
        else:
            target = None
            lines = [pick(lang, "Выберите язык рассылки. Последние 5 заданий:", "Choose the broadcast language. Latest 5 jobs:", '请选择群发语言。最近 5 个任务：', 'زبان ارسال را انتخاب کنید. ۵ کار آخر:')]
            choices = [callback(f"{LANGUAGE_LABELS[code].split()[0]} {code.upper()} · {self.db.audience_count(code)}", f"broadcast_{code}") for code in LANGUAGES]
            rows = [choices[:2], choices[2:]]
        labels = {
            "draft": pick(lang, "черновик", "draft", "草稿", "پیش‌نویس"),
            "running": pick(lang, "отправляется", "running", "发送中", "در حال ارسال"),
            "done": pick(lang, "завершена", "done", "已完成", "پایان‌یافته"),
            "cancelled": pick(lang, "отменена", "cancelled", "已取消", "لغوشده"),
        }
        for job_id, status, job_lang in self.db.connection.execute("SELECT id,status,lang FROM broadcasts WHERE (? IS NULL OR lang=?) ORDER BY id DESC LIMIT 5", (target, target)):
            counts = dict(self.db.connection.execute("SELECT status,COUNT(*) FROM broadcast_recipients WHERE job_id=? GROUP BY status", (job_id,)))
            audience = job_lang.upper() if job_lang in LANGUAGES else pick(lang, "прежняя: все языки", "legacy: all languages", '旧版：所有语言', 'قدیمی: همه زبان\u200cها')
            lines.append(f"#{job_id} [{audience}]: {labels.get(status, status)}\n" + pick(lang,
                f"Доставлено: {counts.get('sent',0)}, ошибки: {counts.get('failed',0)}, неизвестно: {counts.get('unknown',0)}, осталось: {counts.get('pending',0)}",
                f"Sent: {counts.get('sent',0)}, failed: {counts.get('failed',0)}, unknown: {counts.get('unknown',0)}, pending: {counts.get('pending',0)}", f"已发送：{counts.get('sent', 0)}，失败：{counts.get('failed', 0)}，未知：{counts.get('unknown', 0)}，待发送：{counts.get('pending', 0)}", f"ارسال\u200cشده: {counts.get('sent', 0)}، ناموفق: {counts.get('failed', 0)}، نامشخص: {counts.get('unknown', 0)}، باقی\u200cمانده: {counts.get('pending', 0)}"))
            if status in {"draft", "running"}:
                rows.append([callback(pick(lang, f"Отменить #{job_id}", f"Cancel #{job_id}", f'取消 #{job_id}', f'لغو #{job_id}'), f"broadcast_cancel_{job_id}")])
            if status == "draft" and job_lang in LANGUAGES:
                rows.append([callback(pick(lang, f"Отправить #{job_id} для {job_lang.upper()}", f"Send #{job_id} to {job_lang.upper()}", f'将 #{job_id} 发送给 {job_lang.upper()} 用户', f'ارسال #{job_id} برای {job_lang.upper()}'), f"broadcast_send_{job_id}")])
        rows.append([callback(pick(lang, "Обновить", "Refresh", '刷新', 'تازه\u200cسازی'), f"broadcast_{target}" if target else "broadcast")])
        if target:
            rows.append([callback(pick(lang, "↩️ Выбрать язык рассылки", "↩️ Choose broadcast language", '↩️ 选择群发语言', '↩️ انتخاب زبان ارسال'), "broadcast")])
        rows.append([callback(pick(lang, "Админ меню", "Admin menu", '管理菜单', 'منوی مدیریت'), "admin")])
        self.send(self.admin_id, "\n\n".join(lines), keyboard(rows))

    def broadcast_tick(self, stop: threading.Event = STOP) -> None:
        conn = self.db.connection
        job = conn.execute("SELECT id,payload,lang FROM broadcasts WHERE status='running' AND next_at<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
        if not job:
            return
        job_id, raw, target = job
        for _ in range(10):
            if stop.is_set():
                return
            recipient = conn.execute("SELECT user_id FROM broadcast_recipients WHERE job_id=? AND status='pending' ORDER BY user_id LIMIT 1", (job_id,)).fetchone()
            if recipient is None:
                with conn:
                    conn.execute("UPDATE broadcasts SET status='done' WHERE id=?", (job_id,))
                self.show_broadcasts(target if target in LANGUAGES else None)
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
