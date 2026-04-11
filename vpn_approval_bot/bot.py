import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import paramiko


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
POLL_TIMEOUT = 30


@dataclass
class Config:
    telegram_token: str
    admin_chat_ids: set[int]
    db_path: Path
    ssh_host: str
    ssh_user: str
    ssh_password: str
    ssh_port: int
    xray_config_path: str
    vpn_host: str
    vpn_sni: str
    vpn_public_key: str
    vpn_short_id: str
    default_port: int
    mts_port: int
    backup_dir: str


def _get(raw: dict[str, Any], env_name: str, default: str = "") -> str:
    return os.getenv(env_name, str(raw.get(env_name.lower(), default))).strip()


def load_config() -> Config:
    raw: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

    token = _get(raw, "TELEGRAM_BOT_TOKEN")
    admin_ids_raw = _get(raw, "ADMIN_CHAT_IDS")
    ssh_host = _get(raw, "VPN_SSH_HOST", "72.56.18.77")
    ssh_user = _get(raw, "VPN_SSH_USER", "root")
    ssh_password = _get(raw, "VPN_SSH_PASSWORD")
    vpn_public_key = _get(raw, "VPN_PUBLIC_KEY")
    vpn_short_id = _get(raw, "VPN_SHORT_ID")

    missing = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not admin_ids_raw:
        missing.append("ADMIN_CHAT_IDS")
    if not ssh_password:
        missing.append("VPN_SSH_PASSWORD")
    if not vpn_public_key:
        missing.append("VPN_PUBLIC_KEY")
    if not vpn_short_id:
        missing.append("VPN_SHORT_ID")
    if missing:
        raise ValueError("Missing config values: " + ", ".join(missing))

    admin_chat_ids = {int(item.strip()) for item in admin_ids_raw.split(",") if item.strip()}
    db_path_raw = _get(raw, "DB_PATH", str(BASE_DIR / "vpn_approval.db"))
    db_path = Path(db_path_raw)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        telegram_token=token,
        admin_chat_ids=admin_chat_ids,
        db_path=db_path,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
        ssh_port=int(_get(raw, "VPN_SSH_PORT", "22")),
        xray_config_path=_get(raw, "XRAY_CONFIG_PATH", "/usr/local/etc/xray/config.json"),
        vpn_host=_get(raw, "VPN_HOST", ssh_host),
        vpn_sni=_get(raw, "VPN_SNI", "www.cloudflare.com"),
        vpn_public_key=vpn_public_key,
        vpn_short_id=vpn_short_id,
        default_port=int(_get(raw, "VPN_DEFAULT_PORT", "443")),
        mts_port=int(_get(raw, "VPN_MTS_PORT", "8443")),
        backup_dir=_get(raw, "VPN_BACKUP_DIR", "/usr/local/etc/xray"),
    )


class Store:
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

    def _init_db(self) -> None:
        with self._managed_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vpn_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    profile_type TEXT NOT NULL DEFAULT '',
                    client_email TEXT NOT NULL DEFAULT '',
                    uuid TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                )
                """
            )
            conn.commit()

    def create_request(self, chat_id: int, username: str, full_name: str) -> int:
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._managed_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM vpn_requests
                WHERE chat_id = ? AND status = 'pending'
                ORDER BY id DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            if existing:
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO vpn_requests (chat_id, username, full_name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, username, full_name, now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_request(self, request_id: int) -> sqlite3.Row | None:
        with self._managed_connection() as conn:
            return conn.execute("SELECT * FROM vpn_requests WHERE id = ?", (request_id,)).fetchone()

    def finish_request(self, request_id: int, status: str, profile_type: str, client_email: str, client_uuid: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._managed_connection() as conn:
            conn.execute(
                """
                UPDATE vpn_requests
                SET status = ?, profile_type = ?, client_email = ?, uuid = ?, decided_at = ?
                WHERE id = ?
                """,
                (status, profile_type, client_email, client_uuid, now, request_id),
            )
            conn.commit()


class TelegramBot:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

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
        return self._request("getUpdates", payload).get("result", [])

    def send_message(self, chat_id: int, text: str, reply_markup: str | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._request("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self._request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


class XrayManager:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.ssh_host,
            username=self.config.ssh_user,
            password=self.config.ssh_password,
            port=self.config.ssh_port,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

    @staticmethod
    def _run(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
        stdin, stdout, stderr = client.exec_command(command)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out, err

    def _connect_with_retry(self, attempts: int = 6, delay: int = 3) -> paramiko.SSHClient:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                return self._connect()
            except Exception as exc:
                last_error = exc
                time.sleep(delay)
        raise RuntimeError(f"Could not reconnect to server: {last_error}")

    def _restore_backup(self, backup_path: str) -> None:
        client = self._connect_with_retry()
        try:
            self._run(
                client,
                f"install -m 600 {backup_path} {self.config.xray_config_path} && systemctl restart xray",
            )
        finally:
            client.close()

    def add_client(self, client_email: str, client_uuid: str) -> None:
        client = self._connect()
        sftp = client.open_sftp()
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        backup_path = f"{self.config.backup_dir}/config.json.backup-vpn-bot-{stamp}"
        try:
            with sftp.open(self.config.xray_config_path, "r") as fh:
                xray_config = json.load(fh)

            rc, out, err = self._run(
                client,
                f"cp {self.config.xray_config_path} {backup_path} && chmod 600 {backup_path}",
            )
            if rc != 0:
                raise RuntimeError(f"Backup failed: {out}{err}")

            new_client = {
                "id": client_uuid,
                "flow": "xtls-rprx-vision",
                "email": client_email,
            }
            for inbound in xray_config.get("inbounds", []):
                if inbound.get("protocol") != "vless":
                    continue
                clients = inbound.setdefault("settings", {}).setdefault("clients", [])
                emails = {item.get("email") for item in clients}
                if client_email not in emails:
                    clients.append(dict(new_client))

            candidate = "/tmp/xray-config-vpn-bot.json"
            with sftp.open(candidate, "w") as fh:
                fh.write(json.dumps(xray_config, ensure_ascii=False, indent=2) + "\n")

            for command in (
                f"python3 -m json.tool {candidate} >/dev/null",
                f"install -m 600 {candidate} {self.config.xray_config_path}",
            ):
                rc, out, err = self._run(client, command)
                if rc != 0:
                    self._restore_backup(backup_path)
                    raise RuntimeError(f"Xray update failed and was rolled back: {out}{err}")

            sftp.close()
            sftp = None
            try:
                self._run(client, "systemctl restart xray")
            except Exception:
                pass
            client.close()

            client = self._connect_with_retry()
            rc, out, err = self._run(client, "systemctl is-active --quiet xray")
            if rc != 0:
                self._restore_backup(backup_path)
                raise RuntimeError(f"Xray is not active after update; rolled back: {out}{err}")
        finally:
            if sftp is not None:
                sftp.close()
            client.close()


def build_vless_link(config: Config, client_uuid: str, profile_type: str, label: str) -> str:
    port = config.mts_port if profile_type == "mts" else config.default_port
    fragment = quote(label)
    return (
        f"vless://{client_uuid}@{config.vpn_host}:{port}"
        f"?encryption=none&flow=xtls-rprx-vision&security=reality"
        f"&sni={quote(config.vpn_sni)}&fp=chrome&pbk={quote(config.vpn_public_key)}"
        f"&sid={quote(config.vpn_short_id)}&type=tcp&headerType=none&spx=%2F#{fragment}"
    )


def request_markup(request_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Обычный 443", "callback_data": f"approve:default:{request_id}"},
                    {"text": "МТС 8443", "callback_data": f"approve:mts:{request_id}"},
                ],
                [{"text": "Отклонить", "callback_data": f"reject:{request_id}"}],
            ]
        },
        ensure_ascii=False,
    )


def user_display(user: dict[str, Any]) -> tuple[str, str]:
    username = str(user.get("username") or "").strip()
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return username, full_name


def handle_message(bot: TelegramBot, store: Store, config: Config, message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = int(chat.get("id") or 0)
    text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        bot.send_message(
            chat_id,
            "Привет. Этот бот выдаёт VPN после одобрения админом.\n\n"
            "Команды:\n"
            "/vpn - отправить заявку на VPN\n"
            "/vpn_status ID - проверить заявку",
        )
        return

    if text.startswith("/vpn_status"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(chat_id, "Напиши так: /vpn_status ID")
            return
        row = store.get_request(int(parts[1]))
        if not row or int(row["chat_id"]) != chat_id:
            bot.send_message(chat_id, "Не нашёл такую заявку для твоего аккаунта.")
            return
        bot.send_message(chat_id, f"Статус заявки #{row['id']}: {row['status']}")
        return

    if text.startswith("/vpn"):
        username, full_name = user_display(user)
        request_id = store.create_request(chat_id, username, full_name)
        bot.send_message(chat_id, f"Заявка #{request_id} отправлена админу. Дождись одобрения.")
        admin_text = (
            f"Новая VPN-заявка #{request_id}\n"
            f"Chat ID: {chat_id}\n"
            f"Username: @{username}" if username else f"Новая VPN-заявка #{request_id}\nChat ID: {chat_id}\nUsername: -"
        )
        admin_text += f"\nName: {full_name or '-'}"
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, admin_text, request_markup(request_id))
        return

    bot.send_message(chat_id, "Напиши /vpn, чтобы запросить VPN-доступ.")


def handle_callback(bot: TelegramBot, store: Store, config: Config, manager: XrayManager, callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    from_user = callback_query.get("from") or {}
    admin_chat_id = int(from_user.get("id") or 0)
    payload = str(callback_query.get("data") or "")

    if admin_chat_id not in config.admin_chat_ids:
        bot.answer_callback_query(callback_id, "Нет доступа.")
        return

    parts = payload.split(":")
    if not parts:
        return

    if parts[0] == "reject" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or row["status"] != "pending":
            bot.answer_callback_query(callback_id, "Заявка уже обработана.")
            return
        store.finish_request(request_id, "rejected", "", "", "")
        bot.answer_callback_query(callback_id, "Отклонено.")
        bot.send_message(int(row["chat_id"]), f"Заявка #{request_id} отклонена.")
        return

    if parts[0] == "approve" and len(parts) == 3 and parts[2].isdigit():
        profile_type = parts[1]
        request_id = int(parts[2])
        if profile_type not in {"default", "mts"}:
            bot.answer_callback_query(callback_id, "Неизвестный тип профиля.")
            return
        row = store.get_request(request_id)
        if not row or row["status"] != "pending":
            bot.answer_callback_query(callback_id, "Заявка уже обработана.")
            return

        client_uuid = str(uuid.uuid4())
        client_email = f"tg-{row['chat_id']}-{request_id}"
        try:
            manager.add_client(client_email, client_uuid)
        except Exception as exc:
            logging.exception("Failed to create VPN profile")
            bot.answer_callback_query(callback_id, "Ошибка, конфиг не изменён или откатан.")
            bot.send_message(admin_chat_id, f"Не смог создать профиль для заявки #{request_id}: {exc}")
            return

        store.finish_request(request_id, "approved", profile_type, client_email, client_uuid)
        label = f"VPN {request_id} {'MTS' if profile_type == 'mts' else '443'}"
        link = build_vless_link(config, client_uuid, profile_type, label)
        bot.answer_callback_query(callback_id, "Профиль создан.")
        bot.send_message(int(row["chat_id"]), "Заявка одобрена. Твоя VPN-ссылка:\n\n" + link)
        bot.send_message(admin_chat_id, f"Готово. Заявка #{request_id} одобрена как {profile_type}.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    store = Store(config.db_path)
    bot = TelegramBot(config.telegram_token)
    manager = XrayManager(config)
    logging.info("VPN approval bot started")
    offset = None
    while True:
        try:
            updates = bot.get_updates(offset=offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                if "message" in update:
                    handle_message(bot, store, config, update["message"])
                if "callback_query" in update:
                    handle_callback(bot, store, config, manager, update["callback_query"])
        except KeyboardInterrupt:
            logging.info("Stopped by user")
            break
        except Exception:
            logging.exception("Polling error")
            time.sleep(5)


if __name__ == "__main__":
    main()
