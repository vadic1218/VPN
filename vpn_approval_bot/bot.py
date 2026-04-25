# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import paramiko


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
POLL_TIMEOUT = 30
SHARING_CHECK_INTERVAL_SECONDS = 60
SUBSCRIPTION_CHECK_INTERVAL_SECONDS = 600
SHARING_LOOKBACK_MINUTES = 10
SHARING_ALERT_COOLDOWN_MINUTES = 30
DEFAULT_SUBSCRIPTION_DAYS = 30
XRAY_USAGE_RE = re.compile(
    r"from (?:tcp:)?(?P<ip>\d+\.\d+\.\d+\.\d+):\d+ accepted .* email: (?P<email>\S+)"
)
TG_CLIENT_EMAIL_RE = re.compile(r"^tg-(?P<chat_id>\d+)-(?P<request_id>\d+)$")


def default_db_path() -> Path:
    if Path("/data").is_dir() or os.getenv("RAILWAY_ENVIRONMENT"):
        return Path("/data/vpn_approval.json")
    return BASE_DIR / "vpn_approval.json"


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
    default_subscription_days: int


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def subscription_until(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


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
    db_path_raw = _get(raw, "DB_PATH", str(default_db_path()))
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
        default_subscription_days=int(_get(raw, "VPN_DEFAULT_SUBSCRIPTION_DAYS", str(DEFAULT_SUBSCRIPTION_DAYS))),
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

    def import_approved_clients(self, clients: list[dict[str, str]]) -> int:
        data = self._read()
        existing_emails = {str(item.get("client_email") or "") for item in data["requests"]}
        existing_ids = {int(item["id"]) for item in data["requests"] if str(item.get("id") or "").isdigit()}
        imported = 0
        now = utc_now_iso()
        initial_subscription_until = subscription_until(DEFAULT_SUBSCRIPTION_DAYS)

        for client in clients:
            email = str(client.get("email") or "")
            client_uuid = str(client.get("uuid") or "")
            match = TG_CLIENT_EMAIL_RE.match(email)
            if not match or not client_uuid or email in existing_emails:
                continue

            request_id = int(match.group("request_id"))
            while request_id in existing_ids:
                request_id += 1

            data["requests"].append(
                {
                    "id": request_id,
                    "chat_id": int(match.group("chat_id")),
                    "username": "-",
                    "full_name": "restored from xray",
                    "status": "approved",
                    "profile_type": "default",
                    "client_email": email,
                    "uuid": client_uuid,
                    "created_at": now,
                    "decided_at": now,
                    "subscription_status": "active",
                    "subscription_until": initial_subscription_until,
                    "restored_from_xray": True,
                }
            )
            existing_emails.add(email)
            existing_ids.add(request_id)
            data["last_id"] = max(int(data.get("last_id", 0)), request_id)
            imported += 1

        if imported:
            self._write(data)
        return imported

    def update_user_info(self, chat_id: int, username: str, full_name: str) -> bool:
        data = self._read()
        changed = False
        for request in data["requests"]:
            if int(request.get("chat_id") or 0) != chat_id:
                continue
            if username and str(request.get("username") or "") in {"", "-"}:
                request["username"] = username
                changed = True
            if full_name and str(request.get("full_name") or "") in {"", "-", "restored from xray"}:
                request["full_name"] = full_name
                changed = True
        if changed:
            self._write(data)
        return changed

    def finish_request(
        self,
        request_id: int,
        status: str,
        profile_type: str,
        client_email: str,
        client_uuid: str,
        subscription_days: int = DEFAULT_SUBSCRIPTION_DAYS,
    ) -> None:
        now = utc_now_iso()
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["status"] = status
                request["profile_type"] = profile_type
                request["client_email"] = client_email
                request["uuid"] = client_uuid
                request["decided_at"] = now
                if status == "approved":
                    request["subscription_status"] = "active"
                    request["subscription_until"] = subscription_until(subscription_days)
                self._write(data)
                return

    def update_profile(self, request_id: int, profile_type: str, client_email: str, client_uuid: str) -> None:
        now = utc_now_iso()
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["status"] = "approved"
                request["profile_type"] = profile_type
                request["client_email"] = client_email
                request["uuid"] = client_uuid
                request["decided_at"] = now
                request.setdefault("subscription_status", "active")
                request.setdefault("subscription_until", subscription_until(DEFAULT_SUBSCRIPTION_DAYS))
                self._write(data)
                return

    def disable_request(self, request_id: int) -> None:
        now = utc_now_iso()
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["status"] = "disabled"
                request["subscription_status"] = "disabled"
                request["disabled_at"] = now
                self._write(data)
                return

    def expire_request(self, request_id: int) -> None:
        now = utc_now_iso()
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["status"] = "expired"
                request["subscription_status"] = "expired"
                request["expired_at"] = now
                self._write(data)
                return

    def extend_subscription(self, request_id: int, days: int) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            current_until = parse_iso_time(str(request.get("subscription_until") or ""))
            if not current_until:
                current_until = now
            if current_until.tzinfo is None:
                current_until = current_until.replace(tzinfo=timezone.utc)
            base = max(current_until, now)
            request["subscription_until"] = (base + timedelta(days=days)).isoformat(timespec="seconds")
            request["subscription_status"] = "active"
            if request.get("status") == "expired":
                request["status"] = "approved"
            self._write(data)
            return request
        return None

    def find_expired_requests(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        result: list[dict[str, Any]] = []
        for request in self.list_approved_requests():
            until = parse_iso_time(str(request.get("subscription_until") or ""))
            if not until:
                continue
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until <= now:
                result.append(request)
        return result

    def mark_sharing_alert(self, client_email: str) -> None:
        data = self._read()
        alerts = data.setdefault("sharing_alerts", {})
        entry = alerts.setdefault(client_email, {})
        entry["last_alert_at"] = datetime.utcnow().isoformat(timespec="seconds")
        self._write(data)

    def ignore_sharing_alert(self, client_email: str, minutes: int = SHARING_ALERT_COOLDOWN_MINUTES) -> None:
        data = self._read()
        alerts = data.setdefault("sharing_alerts", {})
        entry = alerts.setdefault(client_email, {})
        ignored_until = datetime.utcnow() + timedelta(minutes=minutes)
        entry["ignored_until"] = ignored_until.isoformat(timespec="seconds")
        self._write(data)

    def should_send_sharing_alert(self, client_email: str) -> bool:
        data = self._read()
        entry = data.get("sharing_alerts", {}).get(client_email, {})
        now = datetime.utcnow()
        for field in ("ignored_until", "last_alert_at"):
            value = str(entry.get(field) or "")
            if not value:
                continue
            dt = parse_iso_time(value)
            if not dt:
                continue
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            if field == "ignored_until" and dt > now:
                return False
            if field == "last_alert_at" and now - dt < timedelta(minutes=SHARING_ALERT_COOLDOWN_MINUTES):
                return False
        return True


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

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self._request("getChat", {"chat_id": chat_id}).get("result", {})

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

    def remove_client(self, client_email: str) -> None:
        client = self._connect()
        sftp = client.open_sftp()
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        backup_path = f"{self.config.backup_dir}/config.json.backup-vpn-disable-{stamp}"
        try:
            with sftp.open(self.config.xray_config_path, "r") as fh:
                xray_config = json.load(fh)

            rc, out, err = self._run(
                client,
                f"cp {self.config.xray_config_path} {backup_path} && chmod 600 {backup_path}",
            )
            if rc != 0:
                raise RuntimeError(f"Backup failed: {out}{err}")

            removed = 0
            for inbound in xray_config.get("inbounds", []):
                if inbound.get("protocol") != "vless":
                    continue
                clients = inbound.setdefault("settings", {}).setdefault("clients", [])
                before = len(clients)
                clients[:] = [item for item in clients if item.get("email") != client_email]
                removed += before - len(clients)

            if removed == 0:
                raise RuntimeError(f"Client {client_email} was not found in Xray config")

            candidate = "/tmp/xray-config-vpn-disable.json"
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
                raise RuntimeError(f"Xray is not active after disable; rolled back: {out}{err}")
        finally:
            if sftp is not None:
                sftp.close()
            client.close()

    def list_bot_clients(self) -> list[dict[str, str]]:
        client = self._connect()
        sftp = client.open_sftp()
        try:
            with sftp.open(self.config.xray_config_path, "r") as fh:
                xray_config = json.load(fh)
        finally:
            sftp.close()
            client.close()

        by_email: dict[str, dict[str, str]] = {}
        for inbound in xray_config.get("inbounds", []):
            if inbound.get("protocol") != "vless":
                continue
            for item in inbound.get("settings", {}).get("clients", []):
                email = str(item.get("email") or "")
                client_uuid = str(item.get("id") or "")
                if TG_CLIENT_EMAIL_RE.match(email) and client_uuid:
                    by_email[email] = {"email": email, "uuid": client_uuid}
        return sorted(by_email.values(), key=lambda item: item["email"])

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

    def get_recent_ips_by_email(self, emails: list[str], minutes: int = SHARING_LOOKBACK_MINUTES) -> dict[str, set[str]]:
        if not emails:
            return {}
        client = self._connect()
        try:
            command = f"journalctl -u xray --since '{minutes} minutes ago' -o cat --no-pager || true"
            rc, out, err = self._run(client, command)
            if rc != 0:
                raise RuntimeError((err or "Could not read Xray journal").strip()[:500])
        finally:
            client.close()

        email_set = set(emails)
        result: dict[str, set[str]] = {email: set() for email in email_set}
        for line in out.splitlines():
            match = XRAY_USAGE_RE.search(line)
            if not match:
                continue
            email = match.group("email")
            if email in email_set:
                result[email].add(match.group("ip"))
        return result


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
                [{"text": "Статус заявки"}, {"text": "Моя подписка"}],
                [{"text": "Список клиентов"}],
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
                [{"text": "Моя подписка"}],
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
    rows = [
        [
            {"text": "+7 дней", "callback_data": f"extend:7:{request_id}"},
            {"text": "+30 дней", "callback_data": f"extend:30:{request_id}"},
            {"text": "+90 дней", "callback_data": f"extend:90:{request_id}"},
        ]
    ]
    rows.extend(profile_button_rows("reissue", request_id))
    rows.append([{"text": "Отключить пользователя", "callback_data": f"disable:{request_id}"}])
    return json.dumps(
        {"inline_keyboard": rows},
        ensure_ascii=False,
    )


def sharing_alert_markup(request_id: int, profile_type: str) -> str:
    if not is_profile_type(profile_type):
        profile_type = "default"
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Отключить", "callback_data": f"disable:{request_id}"},
                    {"text": "Перевыпустить", "callback_data": f"reissue:{profile_type}:{request_id}"},
                ],
                [{"text": "Игнорировать", "callback_data": f"ignore_share:{request_id}"}],
            ]
        },
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


def format_subscription(value: str | None) -> str:
    dt = parse_iso_time(value or "")
    if not dt:
        return "срок не задан"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds = int((dt - now).total_seconds())
    date_text = dt.strftime("%d.%m.%Y")
    if seconds <= 0:
        return f"истекла {date_text}"
    days = seconds // 86400
    if days <= 0:
        hours = max(1, seconds // 3600)
        return f"до {date_text}, осталось {hours} ч"
    return f"до {date_text}, осталось {days} дн"


def format_client_list(rows: list[dict[str, Any]], last_seen: dict[str, str]) -> str:
    if not rows:
        return "Одобренных клиентов пока нет."

    lines = [f"Клиенты VPN: {len(rows)}"]
    for number, row in enumerate(rows, start=1):
        profile_type = profile_label(str(row.get("profile_type") or "default"))
        email = str(row.get("client_email") or "")
        username = format_username(str(row.get("username") or ""))
        subscription = format_subscription(str(row.get("subscription_until") or ""))
        lines.append(
            f"{number}. {username} | ID профиля: {row['id']} | {profile_type} | подписка: {subscription} | активность: {format_age(last_seen.get(email))}"
        )
    return "\n".join(lines)


def format_client_card(row: dict[str, Any], last_seen: dict[str, str], number: int) -> str:
    profile_type = profile_label(str(row.get("profile_type") or "default"))
    email = str(row.get("client_email") or "")
    username = format_username(str(row.get("username") or ""))
    full_name = str(row.get("full_name") or "-")
    chat_id = str(row.get("chat_id") or "-")
    client_uuid = str(row.get("uuid") or "-")
    status = str(row.get("status") or "-")
    created_at = str(row.get("created_at") or "-")
    decided_at = str(row.get("decided_at") or "-")
    subscription_status = str(row.get("subscription_status") or "active")
    subscription_text = format_subscription(str(row.get("subscription_until") or ""))
    restored = "да" if row.get("restored_from_xray") else "нет"
    return (
        f"Клиент #{number}\n"
        f"ID профиля: {row['id']}\n"
        f"Статус: {status}\n"
        f"Подписка: {subscription_status}, {subscription_text}\n"
        f"Chat ID: {chat_id}\n"
        f"Username: {username}\n"
        f"Имя: {full_name}\n"
        f"Тип: {profile_type}\n"
        f"Email в Xray: {email or '-'}\n"
        f"UUID: {client_uuid}\n"
        f"Создан: {created_at} ({format_age(created_at)})\n"
        f"Одобрен/обновлён: {decided_at} ({format_age(decided_at)})\n"
        f"Активность: {format_age(last_seen.get(email))}\n"
        f"Восстановлен из Xray: {restored}"
    )


def user_display(user: dict[str, Any]) -> tuple[str, str]:
    username = str(user.get("username") or "").strip()
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return username, full_name


def format_username(username: str) -> str:
    username = username.strip().lstrip("@")
    if not username or username == "-":
        return "нет username"
    return f"@{username}"


def chat_user_display(chat: dict[str, Any]) -> tuple[str, str]:
    username = str(chat.get("username") or "").strip()
    first_name = str(chat.get("first_name") or "").strip()
    last_name = str(chat.get("last_name") or "").strip()
    title = str(chat.get("title") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip() or title
    return username, full_name


def refresh_missing_user_info(bot: TelegramBot, store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changed = False
    seen_chat_ids: set[int] = set()
    for row in rows:
        chat_id = int(row.get("chat_id") or 0)
        if not chat_id or chat_id in seen_chat_ids:
            continue
        seen_chat_ids.add(chat_id)
        has_username = str(row.get("username") or "") not in {"", "-"}
        has_name = str(row.get("full_name") or "") not in {"", "-", "restored from xray"}
        if has_username and has_name:
            continue
        try:
            username, full_name = chat_user_display(bot.get_chat(chat_id))
        except Exception:
            logging.exception("Could not refresh Telegram user info for chat_id=%s", chat_id)
            continue
        if store.update_user_info(chat_id, username, full_name):
            changed = True
    return store.list_approved_requests() if changed else rows


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
            "/reissue - перевыпуск ссылки\n"
            "/subscription - срок подписки",
            reply_markup,
        )
        return

    if text.startswith("/clients") or text.lower() == "список клиентов":
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        rows = store.list_approved_requests()
        if not rows:
            try:
                imported = store.import_approved_clients(manager.list_bot_clients())
                if imported:
                    rows = store.list_approved_requests()
                    bot.send_message(chat_id, f"Восстановил клиентов из Xray: {imported}.")
            except Exception:
                logging.exception("Failed to restore clients from Xray")
        rows = refresh_missing_user_info(bot, store, rows)
        try:
            last_seen = manager.get_last_seen_by_email([str(row["client_email"]) for row in rows])
        except Exception as exc:
            logging.exception("Failed to load client activity")
            error_text = str(exc)
            if len(error_text) > 500:
                error_text = error_text[:500] + "..."
            bot.send_message(chat_id, f"Не смог получить активность с сервера: {error_text}", admin_reply_markup())
            return
        if not rows:
            bot.send_message(chat_id, format_client_list(rows, last_seen), admin_reply_markup())
            return
        bot.send_message(chat_id, format_client_list(rows, last_seen), admin_reply_markup())
        for number, row in enumerate(rows, start=1):
            bot.send_message(chat_id, format_client_card(row, last_seen, number), admin_client_markup(int(row["id"])))
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

    if text.startswith("/subscription") or text.lower() == "моя подписка":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing or existing.get("status") != "approved":
            bot.send_message(chat_id, "У тебя пока нет активной VPN-подписки.")
            return
        bot.send_message(
            chat_id,
            f"Твоя VPN-подписка: {format_subscription(str(existing.get('subscription_until') or ''))}.\n"
            f"Профиль: #{existing['id']}, {profile_label(str(existing.get('profile_type') or 'default'))}.",
            user_reply_markup(),
        )
        return

    if text.lower() == "статус заявки":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing:
            bot.send_message(chat_id, "Заявок пока нет. Нажми «Получить VPN».", user_reply_markup())
            return
        bot.send_message(
            chat_id,
            f"Статус заявки #{existing['id']}: {existing['status']}\n"
            f"Подписка: {format_subscription(str(existing.get('subscription_until') or ''))}",
            user_reply_markup(),
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
        bot.send_message(
            chat_id,
            f"Статус заявки #{row['id']}: {row['status']}\n"
            f"Подписка: {format_subscription(str(row.get('subscription_until') or ''))}",
        )
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

    if parts[0] == "disable" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or row["status"] != "approved" or not row.get("client_email"):
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return

        client_email = str(row["client_email"])
        bot.safe_answer_callback_query(callback_id, "Отключаю пользователя...")
        try:
            manager.remove_client(client_email)
        except Exception as exc:
            logging.exception("Failed to disable VPN profile")
            bot.send_message(user_chat_id, f"Не смог отключить профиль #{request_id}: {exc}")
            return

        store.disable_request(request_id)
        bot.send_message(int(row["chat_id"]), "Твой VPN-профиль отключён админом. Старая ссылка больше не работает.")
        bot.send_message(user_chat_id, f"Профиль #{request_id} отключён. Его старая VPN-ссылка больше не работает.")
        return

    if parts[0] == "extend" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        days = int(parts[1])
        request_id = int(parts[2])
        if days not in {7, 30, 90}:
            bot.answer_callback_query(callback_id, "Неверный срок продления.")
            return
        row = store.get_request(request_id)
        if not row or row.get("status") not in {"approved", "expired"}:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        if row.get("status") == "expired":
            try:
                manager.save_client(str(row["client_email"]), str(row["uuid"]))
            except Exception as exc:
                logging.exception("Failed to restore expired VPN profile")
                bot.send_message(user_chat_id, f"Не смог включить просроченный профиль #{request_id}: {exc}")
                return
        updated = store.extend_subscription(request_id, days)
        if not updated:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        subscription_text = format_subscription(str(updated.get("subscription_until") or ""))
        bot.answer_callback_query(callback_id, f"Продлено на {days} дней.")
        bot.send_message(user_chat_id, f"Подписка профиля #{request_id} продлена на {days} дней: {subscription_text}.")
        bot.send_message(int(updated["chat_id"]), f"Твоя VPN-подписка продлена на {days} дней: {subscription_text}.")
        return

    if parts[0] == "ignore_share" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or not row.get("client_email"):
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        store.ignore_sharing_alert(str(row["client_email"]))
        bot.answer_callback_query(callback_id, "Ок, временно игнорирую.")
        bot.send_message(user_chat_id, f"Подозрение по профилю #{request_id} временно скрыто.")
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

        store.finish_request(request_id, "approved", profile_type, client_email, client_uuid, config.default_subscription_days)
        label = f"VPN {request_id} {profile_short(profile_type)}"
        link = build_vless_link(config, client_uuid, profile_type, label)
        subscription_text = format_subscription(subscription_until(config.default_subscription_days))
        bot.send_message(int(row["chat_id"]), f"Заявка одобрена. Подписка: {subscription_text}.\n\nТвоя VPN-ссылка:\n\n" + link)
        bot.send_message(user_chat_id, f"Готово. Заявка #{request_id} одобрена как {profile_label(profile_type)}. Подписка: {subscription_text}.")


def check_sharing_alerts(bot: TelegramBot, store: Store, config: Config, manager: XrayManager) -> None:
    rows = store.list_approved_requests()
    email_to_row = {str(row.get("client_email") or ""): row for row in rows if row.get("client_email")}
    if not email_to_row:
        return

    recent_ips = manager.get_recent_ips_by_email(list(email_to_row), SHARING_LOOKBACK_MINUTES)
    for email, ips in recent_ips.items():
        if len(ips) < 2 or not store.should_send_sharing_alert(email):
            continue
        row = email_to_row[email]
        request_id = int(row["id"])
        username = str(row.get("username") or "-")
        profile_type = str(row.get("profile_type") or "default")
        ip_list = ", ".join(sorted(ips))
        text = (
            f"Подозрение на шаринг VPN #{request_id}\n"
            f"Пользователь: @{username}\n"
            f"Тип: {profile_label(profile_type)}\n"
            f"За последние {SHARING_LOOKBACK_MINUTES} мин один профиль был с разных IP:\n"
            f"{ip_list}\n\n"
            "Это может быть пересланная ссылка. Проверь и выбери действие."
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, text, sharing_alert_markup(request_id, profile_type))
        store.mark_sharing_alert(email)


def check_expired_subscriptions(bot: TelegramBot, store: Store, config: Config, manager: XrayManager) -> None:
    for row in store.find_expired_requests():
        request_id = int(row["id"])
        client_email = str(row.get("client_email") or "")
        if not client_email:
            continue
        try:
            manager.remove_client(client_email)
        except Exception:
            logging.exception("Failed to disable expired VPN profile #%s", request_id)
            continue
        store.expire_request(request_id)
        username = format_username(str(row.get("username") or ""))
        text = f"Подписка профиля #{request_id} истекла. VPN-ссылка отключена."
        bot.send_message(int(row["chat_id"]), text)
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, f"{text}\nПользователь: {username}\nChat ID: {row['chat_id']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    store = Store(config.db_path)
    bot = TelegramBot(config.telegram_token)
    manager = XrayManager(config)
    try:
        imported = store.import_approved_clients(manager.list_bot_clients())
        if imported:
            logging.info("Restored %s VPN clients from Xray config", imported)
    except Exception:
        logging.exception("Could not restore VPN clients from Xray config")
    logging.info("VPN approval bot started; db_path=%s", config.db_path)
    offset = None
    last_sharing_check = 0.0
    last_subscription_check = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_subscription_check >= SUBSCRIPTION_CHECK_INTERVAL_SECONDS:
                try:
                    check_expired_subscriptions(bot, store, config, manager)
                except Exception:
                    logging.exception("Subscription expiration check failed")
                last_subscription_check = now

            if now - last_sharing_check >= SHARING_CHECK_INTERVAL_SECONDS:
                try:
                    check_sharing_alerts(bot, store, config, manager)
                except Exception:
                    logging.exception("Sharing alert check failed")
                last_sharing_check = now

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
