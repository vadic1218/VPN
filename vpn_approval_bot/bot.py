import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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
    alt_port: int
    backup_dir: str


OPERATOR_PROFILES: dict[str, dict[str, str]] = {
    "default": {"button": "Обычный оператор", "label": "Обычный 443", "short": "443", "port": "default"},
    "mts": {"button": "МТС", "label": "МТС 8443", "short": "MTS", "port": "mts"},
    "megafon": {"button": "МегаФон", "label": "МегаФон 8443", "short": "MegaFon", "port": "mts"},
    "beeline": {"button": "Билайн", "label": "Билайн 443", "short": "Beeline", "port": "default"},
    "tele2": {"button": "Tele2", "label": "Tele2 8443", "short": "Tele2", "port": "mts"},
    "yota": {"button": "Yota", "label": "Yota 8443", "short": "Yota", "port": "mts"},
    "rostelecom": {"button": "Ростелеком", "label": "Ростелеком 443", "short": "RTK", "port": "default"},
    "tbank": {"button": "Т-Мобайл", "label": "Т-Мобайл 8443", "short": "T-Mobile", "port": "mts"},
    "tmobile_us": {"button": "T-Mobile", "label": "T-Mobile 2053", "short": "T-Mobile", "port": "alt"},
}


def is_profile_type(profile_type: str) -> bool:
    return profile_type in OPERATOR_PROFILES


def profile_info(profile_type: str) -> dict[str, str]:
    return OPERATOR_PROFILES.get(profile_type, OPERATOR_PROFILES["default"])


def profile_label(profile_type: str) -> str:
    return profile_info(profile_type)["label"]


def profile_short(profile_type: str) -> str:
    return profile_info(profile_type)["short"]


def profile_port(config: Config, profile_type: str) -> int:
    port_key = profile_info(profile_type)["port"]
    if port_key == "mts":
        return config.mts_port
    if port_key == "alt":
        return config.alt_port
    return config.default_port


def profile_button_rows(callback_prefix: str, request_id: int | None = None) -> list[list[dict[str, str]]]:
    buttons: list[dict[str, str]] = []
    for profile_type, info in OPERATOR_PROFILES.items():
        callback_data = f"{callback_prefix}:{profile_type}"
        if request_id is not None:
            callback_data = f"{callback_data}:{request_id}"
        buttons.append({"text": info["button"], "callback_data": callback_data})
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


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
        alt_port=int(_get(raw, "VPN_ALT_PORT", "2053")),
        backup_dir=_get(raw, "VPN_BACKUP_DIR", "/usr/local/etc/xray"),
    )


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self._write({"last_id": 0, "requests": []})

    def _read(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"last_id": 0, "requests": []}
        with self.db_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, data: dict[str, Any]) -> None:
        with self.db_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def create_request(self, chat_id: int, username: str, full_name: str, profile_type: str) -> int:
        now = datetime.utcnow().isoformat(timespec="seconds")
        data = self._read()
        for request in reversed(data["requests"]):
            if int(request["chat_id"]) == chat_id and request["status"] == "pending":
                request["profile_type"] = profile_type
                self._write(data)
                return int(request["id"])

        request_id = int(data.get("last_id", 0)) + 1
        data["last_id"] = request_id
        data["requests"].append(
            {
                "id": request_id,
                "chat_id": chat_id,
                "username": username,
                "full_name": full_name,
                "status": "pending",
                "profile_type": profile_type,
                "client_email": "",
                "uuid": "",
                "created_at": now,
                "decided_at": None,
            }
        )
        self._write(data)
        return request_id

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                return request
        return None

    def get_active_request_by_chat_id(self, chat_id: int) -> dict[str, Any] | None:
        data = self._read()
        for request in reversed(data["requests"]):
            if int(request["chat_id"]) == chat_id and request.get("status") in {"pending", "approved"}:
                return request
        return None

    def list_approved_requests(self) -> list[dict[str, Any]]:
        data = self._read()
        latest_by_chat_id: dict[int, dict[str, Any]] = {}
        for request in data["requests"]:
            if request.get("status") == "approved" and request.get("client_email"):
                latest_by_chat_id[int(request["chat_id"])] = request
        return sorted(latest_by_chat_id.values(), key=lambda item: int(item["id"]))

    def finish_request(self, request_id: int, status: str, profile_type: str, client_email: str, client_uuid: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["status"] = status
                request["profile_type"] = profile_type
                request["client_email"] = client_email
                request["uuid"] = client_uuid
                request["decided_at"] = now
                self._write(data)
                return

    def update_profile(self, request_id: int, profile_type: str, client_email: str, client_uuid: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["status"] = "approved"
                request["profile_type"] = profile_type
                request["client_email"] = client_email
                request["uuid"] = client_uuid
                request["decided_at"] = now
                self._write(data)
                return


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

    def safe_answer_callback_query(self, callback_query_id: str, text: str) -> None:
        try:
            self.answer_callback_query(callback_query_id, text)
        except Exception:
            logging.warning("Could not answer callback query", exc_info=True)


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

    def save_client(self, client_email: str, client_uuid: str) -> None:
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
                clients[:] = [item for item in clients if item.get("email") != client_email]
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

    def reset_profile_guard_binding(self, client_email: str) -> None:
        client = self._connect()
        try:
            email_literal = json.dumps(client_email)
            command = f"""
python3 - <<'PY'
import json
from pathlib import Path

email = {email_literal}
state_path = Path('/var/lib/xray-profile-guard/state.json')
if state_path.exists():
    state = json.loads(state_path.read_text(encoding='utf-8'))
else:
    state = {{}}
state.setdefault('bindings', {{}}).pop(email, None)
state['blocked_attempts'] = [
    item for item in state.get('blocked_attempts', [])
    if item.get('email') != email
]
prefix = f'{{email}}|'
state['last_attempt_keys'] = {{
    key: value for key, value in state.get('last_attempt_keys', {{}}).items()
    if not key.startswith(prefix)
}}
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
state_path.chmod(0o600)
PY
if systemctl is-active --quiet xray-profile-guard; then
    systemctl restart xray-profile-guard
fi
"""
            rc, out, err = self._run(client, command)
            if rc != 0:
                raise RuntimeError(f"Profile guard reset failed: {out}{err}")
        finally:
            client.close()

    def get_last_seen_by_email(self, emails: list[str]) -> dict[str, str]:
        if not emails:
            return {}
        client = self._connect()
        try:
            command = "journalctl -u xray -n 5000 -o short-iso --no-pager || true"
            rc, out, err = self._run(client, command)
            if rc != 0:
                raise RuntimeError((err or "Could not read Xray journal").strip()[:500])
        finally:
            client.close()

        last_seen: dict[str, str] = {}
        email_set = set(emails)
        for line in out.splitlines():
            for email in email_set:
                if f"email: {email}" in line:
                    parts = line.split(maxsplit=1)
                    if parts:
                        last_seen[email] = parts[0]
        return last_seen


def build_vless_link(config: Config, client_uuid: str, profile_type: str, label: str) -> str:
    port = profile_port(config, profile_type)
    fragment = quote(label)
    return (
        f"vless://{client_uuid}@{config.vpn_host}:{port}"
        f"?encryption=none&flow=xtls-rprx-vision&security=reality"
        f"&sni={quote(config.vpn_sni)}&fp=chrome&pbk={quote(config.vpn_public_key)}"
        f"&sid={quote(config.vpn_short_id)}&type=tcp&headerType=none&spx=%2F#{fragment}"
    )


def admin_reply_markup() -> str:
    return json.dumps(
        {
            "keyboard": [
                [{"text": "Получить VPN"}, {"text": "Перевыпустить ссылку"}],
                [{"text": "Статус заявки"}, {"text": "Список клиентов"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        },
        ensure_ascii=False,
    )


def user_reply_markup() -> str:
    return json.dumps(
        {
            "keyboard": [
                [{"text": "Получить VPN"}],
                [{"text": "Перевыпустить ссылку"}, {"text": "Статус заявки"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        },
        ensure_ascii=False,
    )


def profile_choice_markup() -> str:
    return json.dumps(
        {"inline_keyboard": profile_button_rows("request")},
        ensure_ascii=False,
    )


def admin_request_markup(request_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Одобрить", "callback_data": f"approve:{request_id}"},
                    {"text": "Отклонить", "callback_data": f"reject:{request_id}"},
                ]
            ]
        },
        ensure_ascii=False,
    )


def reissue_choice_markup(request_id: int) -> str:
    return json.dumps(
        {"inline_keyboard": profile_button_rows("reissue_request", request_id)},
        ensure_ascii=False,
    )


def admin_reissue_request_markup(request_id: int, profile_type: str) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Одобрить перевыпуск", "callback_data": f"reissue:{profile_type}:{request_id}"},
                    {"text": "Отклонить", "callback_data": f"reject_reissue:{request_id}"},
                ]
            ]
        },
        ensure_ascii=False,
    )


def admin_client_markup(request_id: int) -> str:
    return json.dumps(
        {"inline_keyboard": profile_button_rows("reissue", request_id)},
        ensure_ascii=False,
    )


def parse_iso_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_age(value: str | None) -> str:
    dt = parse_iso_time(value or "")
    if not dt:
        return "не было подключений"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def format_client_list(rows: list[dict[str, Any]], last_seen: dict[str, str]) -> str:
    if not rows:
        return "Одобренных клиентов пока нет."

    lines = ["Клиенты VPN:"]
    for row in rows:
        profile_type = profile_label(str(row.get("profile_type") or "default"))
        email = str(row.get("client_email") or "")
        last_seen_text = format_age(last_seen.get(email))
        approved_text = format_age(str(row.get("decided_at") or row.get("created_at") or ""))
        username = str(row.get("username") or "-")
        lines.append(
            f"#{row['id']} @{username} | {profile_type} | создан {approved_text} | активность: {last_seen_text}"
        )
    return "\n".join(lines)


def format_client_card(row: dict[str, Any], last_seen: dict[str, str]) -> str:
    profile_type = profile_label(str(row.get("profile_type") or "default"))
    email = str(row.get("client_email") or "")
    username = str(row.get("username") or "-")
    return (
        f"Клиент #{row['id']}\n"
        f"Username: @{username}\n"
        f"Тип: {profile_type}\n"
        f"Создан: {format_age(str(row.get('decided_at') or row.get('created_at') or ''))}\n"
        f"Активность: {format_age(last_seen.get(email))}"
    )


def user_display(user: dict[str, Any]) -> tuple[str, str]:
    username = str(user.get("username") or "").strip()
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return username, full_name


def handle_message(bot: TelegramBot, store: Store, config: Config, manager: XrayManager, message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = int(chat.get("id") or 0)
    text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        reply_markup = admin_reply_markup() if chat_id in config.admin_chat_ids else user_reply_markup()
        bot.send_message(
            chat_id,
            "Привет. Этот бот выдаёт VPN после одобрения админом.\n\n"
            "Команды:\n"
            "/vpn - выбрать оператора и отправить заявку на VPN\n"
            "/vpn_status ID - проверить заявку\n"
            "/reissue - перевыпуск ссылки",
            reply_markup,
        )
        return

    if text.startswith("/clients") or text.lower() == "список клиентов":
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        rows = store.list_approved_requests()
        try:
            last_seen = manager.get_last_seen_by_email([str(row["client_email"]) for row in rows])
        except Exception as exc:
            logging.exception("Failed to load client activity")
            error_text = str(exc)
            if len(error_text) > 500:
                error_text = error_text[:500] + "..."
            bot.send_message(chat_id, f"Не смог получить активность с сервера: {error_text}", admin_reply_markup())
            return
        bot.send_message(chat_id, format_client_list(rows, last_seen), admin_reply_markup())
        for row in rows:
            bot.send_message(chat_id, format_client_card(row, last_seen), admin_client_markup(int(row["id"])))
        return

    if text.startswith("/reissue") or text.lower() == "перевыпустить ссылку":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing or existing.get("status") != "approved":
            bot.send_message(chat_id, "У тебя ещё нет активного VPN-профиля. Сначала напиши /vpn.")
            return
        bot.send_message(
            chat_id,
            f"Выбери, какую ссылку перевыпустить для профиля #{existing['id']}.",
            reissue_choice_markup(int(existing["id"])),
        )
        return

    if text.lower() == "статус заявки":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing:
            bot.send_message(chat_id, "Заявок пока нет. Нажми «Получить VPN».", user_reply_markup())
            return
        bot.send_message(chat_id, f"Статус заявки #{existing['id']}: {existing['status']}", user_reply_markup())
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

    if text.startswith("/vpn") or text.lower() == "получить vpn":
        existing = store.get_active_request_by_chat_id(chat_id)
        if existing:
            if existing.get("status") == "pending":
                bot.send_message(
                    chat_id,
                    f"У тебя уже есть заявка #{existing['id']} на рассмотрении. Дождись решения админа.",
                )
                return
            if existing.get("status") == "approved":
                profile_type = str(existing.get("profile_type") or "default")
                label = f"VPN {existing['id']} {profile_short(profile_type)}"
                link = build_vless_link(config, str(existing["uuid"]), profile_type, label)
                bot.send_message(
                    chat_id,
                    "У тебя уже есть активный VPN-профиль. Новую заявку создавать нельзя.\n\n"
                    "Твоя ссылка:\n\n" + link,
                )
                return
        bot.send_message(
            chat_id,
            "Выбери своего оператора. Если не знаешь, что выбрать, нажми «Обычный оператор».",
            profile_choice_markup(),
        )
        return

    bot.send_message(chat_id, "Напиши /vpn, чтобы запросить VPN-доступ.")


def handle_callback(bot: TelegramBot, store: Store, config: Config, manager: XrayManager, callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    from_user = callback_query.get("from") or {}
    user_chat_id = int(from_user.get("id") or 0)
    payload = str(callback_query.get("data") or "")

    parts = payload.split(":")
    if not parts:
        return

    if parts[0] == "request" and len(parts) == 2:
        profile_type = parts[1]
        if not is_profile_type(profile_type):
            bot.answer_callback_query(callback_id, "Неизвестный тип профиля.")
            return

        existing = store.get_active_request_by_chat_id(user_chat_id)
        if existing:
            if existing.get("status") == "pending":
                bot.answer_callback_query(callback_id, "Заявка уже ожидает решения.")
                bot.send_message(user_chat_id, f"У тебя уже есть заявка #{existing['id']} на рассмотрении.")
                return
            if existing.get("status") == "approved":
                existing_type = str(existing.get("profile_type") or "default")
                label = f"VPN {existing['id']} {profile_short(existing_type)}"
                link = build_vless_link(config, str(existing["uuid"]), existing_type, label)
                bot.answer_callback_query(callback_id, "Профиль уже есть.")
                bot.send_message(user_chat_id, "У тебя уже есть активный VPN-профиль:\n\n" + link)
                return

        username, full_name = user_display(from_user)
        request_id = store.create_request(user_chat_id, username, full_name, profile_type)
        selected_profile_label = profile_label(profile_type)
        bot.answer_callback_query(callback_id, "Заявка отправлена.")
        bot.send_message(user_chat_id, f"Заявка #{request_id} отправлена админу. Тип: {selected_profile_label}.")

        admin_text = (
            f"Новая VPN-заявка #{request_id}\n"
            f"Тип: {selected_profile_label}\n"
            f"Chat ID: {user_chat_id}\n"
            f"Username: @{username if username else '-'}\n"
            f"Name: {full_name or '-'}"
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, admin_text, admin_request_markup(request_id))
        return

    if parts[0] == "reissue_request" and len(parts) == 3 and parts[2].isdigit():
        profile_type = parts[1]
        request_id = int(parts[2])
        if not is_profile_type(profile_type):
            bot.answer_callback_query(callback_id, "Неизвестный тип профиля.")
            return
        row = store.get_request(request_id)
        if not row or int(row["chat_id"]) != user_chat_id or row["status"] != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return

        selected_profile_label = profile_label(profile_type)
        bot.answer_callback_query(callback_id, "Заявка на перевыпуск отправлена.")
        bot.send_message(user_chat_id, f"Заявка на перевыпуск профиля #{request_id} как {selected_profile_label} отправлена админу.")
        username, full_name = user_display(from_user)
        admin_text = (
            f"Заявка на перевыпуск VPN #{request_id}\n"
            f"Тип: {selected_profile_label}\n"
            f"Chat ID: {user_chat_id}\n"
            f"Username: @{username if username else '-'}\n"
            f"Name: {full_name or '-'}"
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, admin_text, admin_reissue_request_markup(request_id, profile_type))
        return

    if user_chat_id not in config.admin_chat_ids:
        bot.answer_callback_query(callback_id, "Нет доступа.")
        return

    if parts[0] == "reject_reissue" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or row["status"] != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Перевыпуск отклонён.")
        bot.send_message(int(row["chat_id"]), f"Админ отклонил перевыпуск профиля #{request_id}.")
        return

    if parts[0] == "reissue" and len(parts) == 3 and parts[2].isdigit():
        profile_type = parts[1]
        request_id = int(parts[2])
        if not is_profile_type(profile_type):
            bot.answer_callback_query(callback_id, "Неизвестный тип профиля.")
            return
        row = store.get_request(request_id)
        if not row or row["status"] != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return

        client_uuid = str(uuid.uuid4())
        client_email = str(row.get("client_email") or f"tg-{row['chat_id']}-{request_id}")
        label = f"VPN {request_id} {profile_short(profile_type)}"
        link = build_vless_link(config, client_uuid, profile_type, label)
        bot.safe_answer_callback_query(callback_id, "Перевыпускаю ссылку...")
        bot.send_message(
            int(row["chat_id"]),
            "Новая VPN-ссылка уже создана. Сейчас применяю её на сервере; старая ссылка может отключиться на несколько секунд.\n\n"
            + link,
        )
        try:
            manager.save_client(client_email, client_uuid)
        except Exception as exc:
            logging.exception("Failed to reissue VPN profile")
            bot.safe_answer_callback_query(callback_id, "Ошибка, конфиг не изменён или откатан.")
            bot.send_message(
                user_chat_id,
                f"Не смог применить перевыпуск профиля #{request_id}: {exc}\n"
                "Если пользователь уже получил новую ссылку, она может не заработать. Старую ссылку сервер должен был сохранить или откатить.",
            )
            return

        store.update_profile(request_id, profile_type, client_email, client_uuid)
        bot.send_message(int(row["chat_id"]), "Готово. Новая VPN-ссылка применена на сервере. Старая ссылка больше не работает.")
        bot.send_message(user_chat_id, f"Профиль #{request_id} перевыпущен как {profile_label(profile_type)}.")
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

    if parts[0] == "approve" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or row["status"] != "pending":
            bot.answer_callback_query(callback_id, "Заявка уже обработана.")
            return
        profile_type = str(row.get("profile_type") or "default")
        if not is_profile_type(profile_type):
            profile_type = "default"

        client_uuid = str(uuid.uuid4())
        client_email = f"tg-{row['chat_id']}-{request_id}"
        bot.safe_answer_callback_query(callback_id, "Создаю профиль...")
        try:
            manager.save_client(client_email, client_uuid)
        except Exception as exc:
            logging.exception("Failed to create VPN profile")
            bot.safe_answer_callback_query(callback_id, "Ошибка, конфиг не изменён или откатан.")
            bot.send_message(user_chat_id, f"Не смог создать профиль для заявки #{request_id}: {exc}")
            return

        store.finish_request(request_id, "approved", profile_type, client_email, client_uuid)
        label = f"VPN {request_id} {profile_short(profile_type)}"
        link = build_vless_link(config, client_uuid, profile_type, label)
        bot.send_message(int(row["chat_id"]), "Заявка одобрена. Твоя VPN-ссылка:\n\n" + link)
        bot.send_message(user_chat_id, f"Готово. Заявка #{request_id} одобрена как {profile_label(profile_type)}.")


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
                    handle_message(bot, store, config, manager, update["message"])
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
