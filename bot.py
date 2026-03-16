import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
POLL_TIMEOUT = 30
SCHEDULER_INTERVAL = 20
REPEAT_MINUTES = 10
STATE_ADD_TIME = "add_time"
STATE_ADD_NAME = "add_name"
STATE_ADD_TEXT = "add_text"
STATE_COMMANDS = {"/start", "/help", "/list", "/off", "/delete", "/cancel"}


@dataclass
class Config:
    token: str
    timezone: str
    db_path: Path


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    timezone = os.getenv("BOT_TIMEZONE", "Europe/Volgograd").strip()
    db_path_raw = os.getenv("DB_PATH", "").strip()

    if not token and CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        token = str(raw.get("token", "")).strip()
        timezone = str(raw.get("timezone", timezone)).strip()
        db_path_raw = str(raw.get("db_path", db_path_raw)).strip()

    if not token:
        raise ValueError(
            "Не задан TELEGRAM_BOT_TOKEN. Для локального запуска можно создать "
            "config.json по образцу config.example.json."
        )

    db_path = Path(db_path_raw) if db_path_raw else BASE_DIR / "reminders.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(token=token, timezone=timezone, db_path=db_path)


class ReminderStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _managed_connection(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _column_exists(self, conn: sqlite3.Connection, column_name: str) -> bool:
        columns = conn.execute("PRAGMA table_info(reminders)").fetchall()
        return any(column["name"] == column_name for column in columns)

    def _init_db(self) -> None:
        with self._managed_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    time_text TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_sent_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_states (
                    chat_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    payload TEXT
                )
                """
            )

            if not self._column_exists(conn, "last_sent_at"):
                conn.execute("ALTER TABLE reminders ADD COLUMN last_sent_at TEXT")
            if not self._column_exists(conn, "title"):
                conn.execute("ALTER TABLE reminders ADD COLUMN title TEXT NOT NULL DEFAULT ''")

            conn.commit()

    def add_reminder(self, chat_id: int, time_text: str, title: str, message: str) -> int:
        with self._managed_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (chat_id, time_text, title, message)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, time_text, title, message),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def list_reminders(self, chat_id: int) -> list[sqlite3.Row]:
        with self._managed_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, time_text, title, message, active
                FROM reminders
                WHERE chat_id = ?
                ORDER BY active DESC, time_text, id
                """,
                (chat_id,),
            )
            return cursor.fetchall()

    def list_active_reminders(self) -> list[sqlite3.Row]:
        with self._managed_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, time_text, title, message, active, last_sent_at
                FROM reminders
                WHERE active = 1
                ORDER BY chat_id, time_text, id
                """
            )
            return cursor.fetchall()

    def disable_reminder(self, chat_id: int, reminder_id: int) -> bool:
        with self._managed_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET active = 0
                WHERE chat_id = ? AND id = ? AND active = 1
                """,
                (chat_id, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_reminder(self, chat_id: int, reminder_id: int) -> bool:
        with self._managed_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM reminders WHERE chat_id = ? AND id = ?",
                (chat_id, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_all_reminders(self, chat_id: int) -> int:
        with self._managed_connection() as conn:
            cursor = conn.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,))
            conn.commit()
            return int(cursor.rowcount)

    def mark_sent(self, reminder_id: int, slot_key: str) -> None:
        with self._managed_connection() as conn:
            conn.execute(
                "UPDATE reminders SET last_sent_at = ? WHERE id = ?",
                (slot_key, reminder_id),
            )
            conn.commit()

    def set_state(self, chat_id: int, state: str, payload: dict[str, Any] | None = None) -> None:
        payload_text = json.dumps(payload or {}, ensure_ascii=False)
        with self._managed_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_states (chat_id, state, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET state = excluded.state, payload = excluded.payload
                """,
                (chat_id, state, payload_text),
            )
            conn.commit()

    def get_state(self, chat_id: int) -> tuple[str, dict[str, Any]] | None:
        with self._managed_connection() as conn:
            row = conn.execute(
                "SELECT state, payload FROM user_states WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if not row:
                return None
            return str(row["state"]), json.loads(row["payload"] or "{}")

    def clear_state(self, chat_id: int) -> None:
        with self._managed_connection() as conn:
            conn.execute("DELETE FROM user_states WHERE chat_id = ?", (chat_id,))
            conn.commit()


class TelegramBot:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    @staticmethod
    def _default_reply_markup() -> str:
        return json.dumps(
            {
                "keyboard": [
                    [{"text": "Начать"}, {"text": "Помощь"}],
                    [{"text": "Мои напоминания"}],
                    [{"text": "Добавить напоминание"}],
                    [{"text": "Выключить напоминание"}],
                    [{"text": "Удалить напоминание"}],
                ],
                "resize_keyboard": True,
                "is_persistent": True,
            },
            ensure_ascii=False,
        )

    def _request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{method}"
        data = None
        headers = {}

        if payload is not None:
            data = urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=POLL_TIMEOUT + 10) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API returned error: {parsed}")
        return parsed

    def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            payload["offset"] = offset
        result = self._request("getUpdates", payload)
        return result.get("result", [])

    def send_message(self, chat_id: int, text: str, reply_markup: str | None = None) -> None:
        self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup or self._default_reply_markup(),
            },
        )

    def send_inline_message(self, chat_id: int, text: str, inline_markup: str) -> None:
        self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": inline_markup,
            },
        )

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self._request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
            },
        )


def validate_time(value: str) -> str | None:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return None
    return value


def build_help() -> str:
    return (
        "Я бот-напоминалка про таблетки.\n\n"
        "Как работает напоминание:\n"
        f"- стартует в указанное время\n"
        f"- повторяется каждые {REPEAT_MINUTES} минут\n"
        "- продолжает повторяться, пока ты сам его не выключишь\n\n"
        "Команды:\n"
        "/start - приветствие\n"
        "/help - справка\n"
        "/add HH:MM название | текст - добавить напоминание\n"
        "/list - показать список\n"
        "/off ID - выключить напоминание\n"
        "/delete - удалить все напоминания\n\n"
        "Пример:\n"
        "/add 09:00 Утро | Выпить таблетки после завтрака"
    )


def reminder_title(row: sqlite3.Row) -> str:
    title = str(row["title"] or "").strip()
    return title or f"Напоминание #{row['id']}"


def format_reminders(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "Напоминаний пока нет. Добавь первое через кнопку или команду /add."

    lines = ["Твои напоминания:"]
    for row in rows:
        status = "включено" if row["active"] else "выключено"
        lines.append("")
        lines.append(f"ID: {row['id']}")
        lines.append(f"Название: {reminder_title(row)}")
        lines.append(f"Статус: {status}")
        lines.append(f"Время старта: {row['time_text']}")
        lines.append(f"Текст: {row['message']}")
        lines.append(f"Повтор: каждые {REPEAT_MINUTES} минут")
    return "\n".join(lines)


def build_add_help() -> str:
    return (
        "Чтобы добавить напоминание, отправь сообщение в формате:\n"
        "/add HH:MM название | текст\n\n"
        "Пример:\n"
        "/add 09:00 Утро | Выпить таблетки после завтрака"
    )


def build_disable_help(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "Выключать пока нечего. У тебя ещё нет напоминаний."

    return (
        format_reminders(rows)
        + "\n\nЧтобы выключить напоминание, отправь:\n"
        "/off ID\n\n"
        "Пример:\n"
        "/off 2"
    )


def build_delete_help(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "Удалять пока нечего. У тебя ещё нет напоминаний."

    return (
        format_reminders(rows)
        + "\n\nКнопка «Удалить напоминание» удаляет все напоминания сразу.\n"
        "Для точечного удаления открой «Мои напоминания» и нажми кнопку под нужным напоминанием."
    )


def build_inline_disable_markup(reminder_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {
                        "text": f"Выключить напоминание #{reminder_id}",
                        "callback_data": f"off:{reminder_id}",
                    }
                ]
            ]
        },
        ensure_ascii=False,
    )


def build_inline_delete_markup(reminder_id: int, title: str) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {
                        "text": f"Удалить «{title}»",
                        "callback_data": f"delete:{reminder_id}",
                    }
                ]
            ]
        },
        ensure_ascii=False,
    )


def parse_command(text: str) -> tuple[str, str]:
    if not text:
        return "", ""

    aliases = {
        "начать": "/start",
        "помощь": "/help",
        "мои напоминания": "/list",
        "добавить напоминание": "/add",
        "выключить напоминание": "/off",
        "удалить напоминание": "/delete",
    }

    normalized = text.strip()
    alias_command = aliases.get(normalized.lower())
    if alias_command:
        return alias_command, ""

    parts = normalized.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return command, args


def is_due_reminder(reminder: sqlite3.Row, now: datetime) -> tuple[bool, str]:
    scheduled = datetime.strptime(str(reminder["time_text"]), "%H:%M")
    scheduled_minutes = scheduled.hour * 60 + scheduled.minute
    current_minutes = now.hour * 60 + now.minute

    if current_minutes < scheduled_minutes:
        return False, ""

    delta_minutes = current_minutes - scheduled_minutes
    if delta_minutes % REPEAT_MINUTES != 0:
        return False, ""

    slot_key = now.strftime("%Y-%m-%d %H:%M")
    if reminder["last_sent_at"] == slot_key:
        return False, ""

    return True, slot_key


def handle_add_flow(bot: TelegramBot, store: ReminderStore, chat_id: int, text: str) -> bool:
    state_info = store.get_state(chat_id)
    if not state_info:
        return False

    state, payload = state_info
    if state == STATE_ADD_TIME:
        time_text = validate_time(text.strip())
        if not time_text:
            bot.send_message(chat_id, "Введи время в формате HH:MM, например 09:00.")
            return True

        store.set_state(chat_id, STATE_ADD_NAME, {"time_text": time_text})
        bot.send_message(chat_id, "Теперь введи название напоминания.")
        return True

    if state == STATE_ADD_NAME:
        title = text.strip()
        if not title:
            bot.send_message(chat_id, "Название не должно быть пустым. Напиши короткое имя напоминания.")
            return True

        time_text = str(payload.get("time_text", "")).strip()
        if not validate_time(time_text):
            store.clear_state(chat_id)
            bot.send_message(chat_id, "Состояние сбилось. Нажми «Добавить напоминание» ещё раз.")
            return True

        store.set_state(chat_id, STATE_ADD_TEXT, {"time_text": time_text, "title": title})
        bot.send_message(chat_id, "Теперь отправь текст напоминания.")
        return True

    if state == STATE_ADD_TEXT:
        reminder_text = text.strip()
        if not reminder_text:
            bot.send_message(chat_id, "Текст напоминания не должен быть пустым.")
            return True

        time_text = str(payload.get("time_text", "")).strip()
        title = str(payload.get("title", "")).strip()
        if not validate_time(time_text) or not title:
            store.clear_state(chat_id)
            bot.send_message(chat_id, "Состояние сбилось. Нажми «Добавить напоминание» ещё раз.")
            return True

        reminder_id = store.add_reminder(chat_id, time_text, title, reminder_text)
        store.clear_state(chat_id)
        bot.send_message(
            chat_id,
            f"Готово. Напоминание «{title}» #{reminder_id} стартует в {time_text} и потом "
            f"будет повторяться каждые {REPEAT_MINUTES} минут, пока ты его не выключишь.",
        )
        return True

    return False


def has_active_add_flow(store: ReminderStore, chat_id: int) -> bool:
    state_info = store.get_state(chat_id)
    if not state_info:
        return False
    return state_info[0] in {STATE_ADD_TIME, STATE_ADD_NAME, STATE_ADD_TEXT}


def handle_message(
    bot: TelegramBot,
    store: ReminderStore,
    timezone: ZoneInfo,
    message: dict[str, Any],
) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text", "")).strip()
    if not chat_id or not text:
        return

    chat_id = int(chat_id)

    command, args = parse_command(text)

    if command == "/cancel":
        if has_active_add_flow(store, chat_id):
            store.clear_state(chat_id)
            bot.send_message(chat_id, "Добавление напоминания отменено.")
        else:
            bot.send_message(chat_id, "Сейчас нечего отменять.")
        return

    if has_active_add_flow(store, chat_id) and command not in STATE_COMMANDS:
        if handle_add_flow(bot, store, chat_id, text):
            return

    if command in {"/start", "/help"}:
        now = datetime.now(timezone).strftime("%H:%M")
        bot.send_message(
            chat_id,
            build_help() + f"\n\nЧасовой пояс бота: {timezone.key}\nСейчас у меня {now}",
        )
        return

    if command == "/add":
        if args.strip():
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                bot.send_message(chat_id, "После времени нужно указать название и текст напоминания.")
                return

            time_text = validate_time(parts[0])
            if not time_text:
                bot.send_message(chat_id, "Время нужно указать в формате HH:MM, например 09:00.")
                return

            title, separator, reminder_text = parts[1].partition("|")
            title = title.strip()
            reminder_text = reminder_text.strip()
            if not separator or not title or not reminder_text:
                bot.send_message(chat_id, "Используй формат: /add HH:MM название | текст")
                return

            reminder_id = store.add_reminder(chat_id, time_text, title, reminder_text)
            bot.send_message(
                chat_id,
                f"Готово. Напоминание «{title}» #{reminder_id} стартует в {time_text} и потом "
                f"будет повторяться каждые {REPEAT_MINUTES} минут, пока ты его не выключишь.",
            )
            return

        store.set_state(chat_id, STATE_ADD_TIME, {})
        bot.send_message(chat_id, "Введи время напоминания в формате HH:MM.")
        return

    if command == "/add_legacy":
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(chat_id, build_add_help())
            return

        time_text = validate_time(parts[0])
        if not time_text:
            bot.send_message(chat_id, "Время нужно указать в формате HH:MM, например 08:30.")
            return

        reminder_text = parts[1].strip()
        if not reminder_text:
            bot.send_message(chat_id, "После времени нужно написать текст напоминания.")
            return

        title = "Напоминание"
        reminder_id = store.add_reminder(chat_id, time_text, title, reminder_text)
        bot.send_message(
            chat_id,
            f"Готово. Напоминание «{title}» #{reminder_id} стартует в {time_text} и потом "
            f"будет повторяться каждые {REPEAT_MINUTES} минут, пока ты его не выключишь.",
        )
        return

    if command == "/list":
        rows = store.list_reminders(chat_id)
        if not rows:
            bot.send_message(chat_id, format_reminders(rows))
            return

        for row in rows:
            status = "включено" if row["active"] else "выключено"
            bot.send_inline_message(
                chat_id,
                (
                    f"ID: {row['id']}\n"
                    f"Название: {reminder_title(row)}\n"
                    f"Статус: {status}\n"
                    f"Время старта: {row['time_text']}\n"
                    f"Текст: {row['message']}\n"
                    f"Повтор: каждые {REPEAT_MINUTES} минут"
                ),
                build_inline_delete_markup(int(row["id"]), reminder_title(row)),
            )
        return

    if command == "/off":
        rows = store.list_reminders(chat_id)
        reminder_id = args.strip()
        if not reminder_id.isdigit():
            bot.send_message(chat_id, build_disable_help(rows))
            return

        disabled = store.disable_reminder(chat_id, int(reminder_id))
        if disabled:
            bot.send_message(
                chat_id,
                "Напоминание выключено.\n\n" + format_reminders(store.list_reminders(chat_id)),
            )
        else:
            bot.send_message(
                chat_id,
                "Не нашёл включённое напоминание с таким ID.\n\n" + build_disable_help(rows),
            )
        return

    if command == "/delete":
        rows = store.list_reminders(chat_id)
        reminder_id = args.strip()
        if reminder_id:
            if not reminder_id.isdigit():
                bot.send_message(chat_id, build_delete_help(rows))
                return

            deleted = store.delete_reminder(chat_id, int(reminder_id))
            if deleted:
                bot.send_message(
                    chat_id,
                    "Напоминание удалено.\n\n" + format_reminders(store.list_reminders(chat_id)),
                )
            else:
                bot.send_message(
                    chat_id,
                    "Не нашёл напоминание с таким ID.\n\n" + build_delete_help(rows),
                )
            return

        deleted_count = store.delete_all_reminders(chat_id)
        if deleted_count:
            bot.send_message(chat_id, f"Удалил все напоминания. Всего: {deleted_count}.")
        else:
            bot.send_message(chat_id, "Удалять пока нечего. У тебя ещё нет напоминаний.")
        return

    bot.send_message(chat_id, "Не понял команду.\n\n" + build_help())


def handle_callback_query(bot: TelegramBot, store: ReminderStore, callback_query: dict[str, Any]) -> None:
    callback_query_id = str(callback_query.get("id", ""))
    payload = str(callback_query.get("data", ""))
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if not callback_query_id or not chat_id:
        return

    chat_id = int(chat_id)

    action, _, reminder_id = payload.partition(":")
    if not reminder_id.isdigit():
        bot.answer_callback_query(callback_query_id, "Некорректный ID.")
        return

    if action == "off":
        disabled = store.disable_reminder(chat_id, int(reminder_id))
        if disabled:
            bot.answer_callback_query(callback_query_id, f"Напоминание #{reminder_id} выключено.")
            bot.send_message(
                chat_id,
                f"Напоминание #{reminder_id} выключено.\n\n" + format_reminders(store.list_reminders(chat_id)),
            )
        else:
            bot.answer_callback_query(callback_query_id, "Это напоминание уже выключено или не найдено.")
        return

    if action == "delete":
        deleted = store.delete_reminder(chat_id, int(reminder_id))
        if deleted:
            bot.answer_callback_query(callback_query_id, f"Напоминание #{reminder_id} удалено.")
            bot.send_message(
                chat_id,
                f"Напоминание #{reminder_id} удалено.\n\n" + format_reminders(store.list_reminders(chat_id)),
            )
        else:
            bot.answer_callback_query(callback_query_id, "Это напоминание уже удалено или не найдено.")
        return

    bot.answer_callback_query(callback_query_id, "Неизвестное действие.")


def scheduler_loop(bot: TelegramBot, store: ReminderStore, timezone: ZoneInfo) -> None:
    while True:
        try:
            now = datetime.now(timezone)
            reminders = store.list_active_reminders()
            for reminder in reminders:
                try:
                    due, slot_key = is_due_reminder(reminder, now)
                    if not due:
                        continue

                    bot.send_inline_message(
                        int(reminder["chat_id"]),
                        f"Пора выпить таблетки: {reminder_title(reminder)}.\n\n"
                        f"{reminder['message']}\n\n"
                        f"Это напоминание будет повторяться каждые {REPEAT_MINUTES} минут, "
                        "пока ты его не выключишь.",
                        build_inline_disable_markup(int(reminder["id"])),
                    )
                    store.mark_sent(int(reminder["id"]), slot_key)
                except Exception:
                    logging.exception("Failed to process reminder %s", reminder["id"])
        except Exception:
            logging.exception("Scheduler error")

        time.sleep(SCHEDULER_INTERVAL)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config()
    timezone = ZoneInfo(config.timezone)
    store = ReminderStore(config.db_path)
    bot = TelegramBot(config.token)

    scheduler = threading.Thread(
        target=scheduler_loop,
        args=(bot, store, timezone),
        daemon=True,
    )
    scheduler.start()

    logging.info("Bot started in timezone %s", timezone.key)
    offset = None

    while True:
        try:
            updates = bot.get_updates(offset=offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message")
                callback_query = update.get("callback_query")
                if message:
                    handle_message(bot, store, timezone, message)
                if callback_query:
                    handle_callback_query(bot, store, callback_query)
        except KeyboardInterrupt:
            logging.info("Bot stopped by user")
            break
        except Exception:
            logging.exception("Polling error")
            time.sleep(5)


if __name__ == "__main__":
    main()
