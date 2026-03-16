import json
import logging
import os
import sqlite3
import threading
import time
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

    def _column_exists(self, conn: sqlite3.Connection, column_name: str) -> bool:
        columns = conn.execute("PRAGMA table_info(reminders)").fetchall()
        return any(column["name"] == column_name for column in columns)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    time_text TEXT NOT NULL,
                    message TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_sent_at TEXT
                )
                """
            )

            if not self._column_exists(conn, "last_sent_at"):
                conn.execute("ALTER TABLE reminders ADD COLUMN last_sent_at TEXT")

            conn.commit()

    def add_reminder(self, chat_id: int, time_text: str, message: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (chat_id, time_text, message)
                VALUES (?, ?, ?)
                """,
                (chat_id, time_text, message),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def list_reminders(self, chat_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, time_text, message, active
                FROM reminders
                WHERE chat_id = ?
                ORDER BY active DESC, time_text, id
                """,
                (chat_id,),
            )
            return cursor.fetchall()

    def list_active_reminders(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, chat_id, time_text, message, active, last_sent_at
                FROM reminders
                WHERE active = 1
                ORDER BY chat_id, time_text, id
                """
            )
            return cursor.fetchall()

    def disable_reminder(self, chat_id: int, reminder_id: int) -> bool:
        with self._connect() as conn:
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
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM reminders WHERE chat_id = ? AND id = ?",
                (chat_id, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def mark_sent(self, reminder_id: int, slot_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reminders SET last_sent_at = ? WHERE id = ?",
                (slot_key, reminder_id),
            )
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

    def send_message(self, chat_id: int, text: str) -> None:
        self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": self._default_reply_markup(),
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
        "/add HH:MM текст - добавить напоминание\n"
        "/list - показать список\n"
        "/off ID - выключить напоминание\n"
        "/delete ID - удалить напоминание\n\n"
        "Пример:\n"
        "/add 09:00 Выпить таблетки после завтрака"
    )


def format_reminders(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "Напоминаний пока нет. Добавь первое через кнопку или команду /add."

    lines = ["Твои напоминания:"]
    for row in rows:
        status = "включено" if row["active"] else "выключено"
        lines.append("")
        lines.append(f"ID: {row['id']}")
        lines.append(f"Статус: {status}")
        lines.append(f"Время старта: {row['time_text']}")
        lines.append(f"Текст: {row['message']}")
        lines.append(f"Повтор: каждые {REPEAT_MINUTES} минут")
    return "\n".join(lines)


def build_add_help() -> str:
    return (
        "Чтобы добавить напоминание, отправь сообщение в формате:\n"
        "/add HH:MM текст\n\n"
        "Пример:\n"
        "/add 09:00 Выпить таблетки после завтрака"
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
        + "\n\nЧтобы удалить напоминание, отправь:\n"
        "/delete ID\n\n"
        "Пример:\n"
        "/delete 2"
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


def handle_message(
    bot: TelegramBot,
    store: ReminderStore,
    timezone: ZoneInfo,
    message: dict[str, Any],
) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text", "")
    if not chat_id or not text:
        return

    command, args = parse_command(text)

    if command in {"/start", "/help"}:
        now = datetime.now(timezone).strftime("%H:%M")
        bot.send_message(
            chat_id,
            build_help() + f"\n\nЧасовой пояс бота: {timezone.key}\nСейчас у меня {now}",
        )
        return

    if command == "/add":
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

        reminder_id = store.add_reminder(chat_id, time_text, reminder_text)
        bot.send_message(
            chat_id,
            f"Готово. Напоминание #{reminder_id} стартует в {time_text} и потом "
            f"будет повторяться каждые {REPEAT_MINUTES} минут, пока ты его не выключишь.",
        )
        return

    if command == "/list":
        bot.send_message(chat_id, format_reminders(store.list_reminders(chat_id)))
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

    bot.send_message(chat_id, "Не понял команду.\n\n" + build_help())


def scheduler_loop(bot: TelegramBot, store: ReminderStore, timezone: ZoneInfo) -> None:
    while True:
        now = datetime.now(timezone)

        try:
            reminders = store.list_active_reminders()
            for reminder in reminders:
                due, slot_key = is_due_reminder(reminder, now)
                if not due:
                    continue

                bot.send_message(
                    int(reminder["chat_id"]),
                    "Пора выпить таблетки.\n\n"
                    f"{reminder['message']}\n\n"
                    f"Это напоминание будет повторяться каждые {REPEAT_MINUTES} минут, "
                    "пока ты его не выключишь.",
                )
                store.mark_sent(int(reminder["id"]), slot_key)
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
                if message:
                    handle_message(bot, store, timezone, message)
        except KeyboardInterrupt:
            logging.info("Bot stopped by user")
            break
        except Exception:
            logging.exception("Polling error")
            time.sleep(5)


if __name__ == "__main__":
    main()
