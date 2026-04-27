# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
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
SHARING_IP_GRACE = 1
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
    payment_qr_url: str
    payment_link: str
    payment_tbank_link: str
    payment_recipient: str
    payment_banks: str
    remote_state_path: str
    public_base_url: str
    web_port: int


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

SUBSCRIPTION_PLANS: dict[str, dict[str, int]] = {
    "1": {"devices": 1, "price": 200},
    "2": {"devices": 2, "price": 300},
    "3": {"devices": 3, "price": 400},
    "4": {"devices": 4, "price": 500},
    "5": {"devices": 5, "price": 600},
}

HAPP_ROUTING_DIRECT_SITES = [
    "geosite:private",
    "geosite:category-ru",
    "geosite:category-gov-ru",
    "domain:ru",
    "domain:su",
    "domain:рф",
    "domain:vk.com",
    "domain:vk.ru",
    "domain:vkuseraudio.net",
    "domain:vkuserlive.net",
    "domain:vkuser.net",
    "domain:vkvideo.ru",
    "domain:userapi.com",
    "domain:mycdn.me",
    "domain:ok.ru",
    "domain:mail.ru",
    "domain:imgsmail.ru",
    "domain:my.mail.ru",
    "domain:cloud.mail.ru",
    "domain:yandex.ru",
    "domain:yandex.net",
    "domain:ya.ru",
    "domain:yastatic.net",
    "domain:yandexcloud.net",
    "domain:dzen.ru",
    "domain:kinopoisk.ru",
    "domain:rutube.ru",
    "domain:premier.one",
    "domain:ivi.ru",
    "domain:okko.tv",
    "domain:more.tv",
    "domain:smotrim.ru",
    "domain:avito.ru",
    "domain:ozon.ru",
    "domain:wildberries.ru",
    "domain:wb.ru",
    "domain:lamoda.ru",
    "domain:2gis.ru",
    "domain:2gis.com",
    "domain:hh.ru",
    "domain:rabota.ru",
    "domain:kp.ru",
    "domain:rbc.ru",
    "domain:lenta.ru",
    "domain:ria.ru",
    "domain:tass.ru",
    "domain:fontanka.ru",
    "domain:mos.ru",
    "domain:mosreg.ru",
    "domain:pfr.gov.ru",
    "domain:sfr.gov.ru",
    "domain:esia.gosuslugi.ru",
    "domain:sberbank.ru",
    "domain:sber.ru",
    "domain:online.sberbank.ru",
    "domain:tbank.ru",
    "domain:tinkoff.ru",
    "domain:tinkoffbank.ru",
    "domain:alfabank.ru",
    "domain:vtb.ru",
    "domain:gazprombank.ru",
    "domain:raiffeisen.ru",
    "domain:pochtabank.ru",
    "domain:mkb.ru",
    "domain:open.ru",
    "domain:psbank.ru",
    "domain:rsb.ru",
    "domain:rncb.ru",
    "domain:uralsib.ru",
    "domain:domrfbank.ru",
    "domain:gosuslugi.ru",
    "domain:nalog.gov.ru",
    "domain:google.com",
    "domain:www.google.com",
    "domain:gstatic.com",
    "domain:googleusercontent.com",
    "domain:recaptcha.net",
    "domain:hcaptcha.com",
]

HAPP_ROUTING_PROXY_SITES = [
    "geosite:tiktok",
    "domain:tiktok.com",
    "domain:tiktokcdn.com",
    "domain:tiktokv.com",
    "domain:tiktokcdn-us.com",
    "domain:musical.ly",
    "domain:byteoversea.com",
    "domain:byteoversea.net",
    "domain:ibytedtos.com",
    "domain:ibyteimg.com",
    "domain:byteimg.com",
    "domain:bytecdn.cn",
]

HAPP_ROUTING_DIRECT_IPS = [
    "geoip:private",
    "geoip:ru",
]


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


def plan_info(plan_id: str | int | None) -> dict[str, int]:
    return SUBSCRIPTION_PLANS.get(str(plan_id or "1"), SUBSCRIPTION_PLANS["1"])


def plan_label(plan_id: str | int | None) -> str:
    plan = plan_info(plan_id)
    return f"{plan['devices']} устр. - {plan['price']} руб/мес"


def price_list_text() -> str:
    lines = ["Прайс лист VPN:"]
    for plan_id in sorted(SUBSCRIPTION_PLANS, key=int):
        lines.append(f"{plan_id}. {plan_label(plan_id)}")
    lines.append("")
    lines.append("После выбора тарифа бот попросит выбрать оператора и отправит заявку админу.")
    return "\n".join(lines)


def payment_comment(request_id: int) -> str:
    return f"VPN #{request_id}"


def payment_methods(config: Config) -> dict[str, dict[str, str]]:
    methods: dict[str, dict[str, str]] = {}
    if config.payment_link or config.payment_qr_url:
        methods["sber"] = {
            "label": "Сбер",
            "link": config.payment_link,
            "qr_url": config.payment_qr_url,
        }
    if config.payment_tbank_link:
        methods["tbank"] = {
            "label": "Т-Банк",
            "link": config.payment_tbank_link,
            "qr_url": "",
        }
    return methods


def payment_method_info(config: Config, payment_method: str) -> dict[str, str]:
    methods = payment_methods(config)
    return methods.get(payment_method) or methods.get("sber") or methods.get("tbank") or {"label": config.payment_banks, "link": "", "qr_url": ""}


def payment_text(config: Config, request_id: int, plan_id: str | int | None, profile_type: str, payment_method: str) -> str:
    plan = plan_info(plan_id)
    method = payment_method_info(config, payment_method)
    payment_link = method.get("link") or ""
    link_text = f"\nСсылка на пополнение: {payment_link}\n" if payment_link else ""
    return (
        f"Оплата VPN #{request_id}\n"
        f"Тариф: {plan_label(plan_id)}\n"
        f"Оператор: {profile_label(profile_type)}\n"
        f"Сумма: {plan['price']} руб\n"
        f"Получатель: {config.payment_recipient}\n"
        f"Банк: {method['label']}\n"
        f"Комментарий: {payment_comment(request_id)}\n\n"
        f"{link_text}"
        "Оплати по QR-коду ниже. После оплаты админ вручную проверит платеж и одобрит VPN."
    )


def qr_url_for_link(link: str) -> str:
    return f"https://api.qrserver.com/v1/create-qr-code/?size=420x420&margin=12&data={quote(link, safe='')}"


def build_happ_routing_link() -> str:
    routing = {
        "Name": "VPN: TikTok через VPN, РФ напрямую",
        "GlobalProxy": "true",
        "RemoteDNSType": "DoH",
        "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
        "RemoteDNSIP": "1.1.1.1",
        "DomesticDNSType": "DoH",
        "DomesticDNSDomain": "https://dns.yandex.ru/dns-query",
        "DomesticDNSIP": "77.88.8.8",
        "Geoipurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
        "Geositeurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
        "LastUpdated": str(int(time.time())),
        "DnsHosts": {
            "cloudflare-dns.com": "1.1.1.1",
            "dns.yandex.ru": "77.88.8.8",
        },
        "DirectSites": HAPP_ROUTING_DIRECT_SITES,
        "DirectIp": HAPP_ROUTING_DIRECT_IPS,
        "ProxySites": HAPP_ROUTING_PROXY_SITES,
        "ProxyIp": [],
        "BlockSites": [],
        "BlockIp": [],
        "DomainStrategy": "IPIfNonMatch",
        "FakeDNS": "false",
    }
    payload = json.dumps(routing, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = b64encode(payload).decode("ascii")
    return f"happ://routing/onadd/{encoded}"


def happ_routing_redirect_url(config: Config) -> str:
    if not config.public_base_url:
        return ""
    return f"{config.public_base_url}/happ-routing"


def plan_change_payment_text(config: Config, request_id: int, plan_id: str | int | None, payment_method: str) -> str:
    plan = plan_info(plan_id)
    method = payment_method_info(config, payment_method)
    payment_link = method.get("link") or ""
    link_text = f"\nСсылка на пополнение: {payment_link}\n" if payment_link else ""
    return (
        f"Оплата смены тарифа VPN #{request_id}\n"
        f"Новый тариф: {plan_label(plan_id)}\n"
        f"Сумма: {plan['price']} руб\n"
        f"Получатель: {config.payment_recipient}\n"
        f"Банк: {method['label']}\n"
        f"Комментарий: {payment_comment(request_id)} тариф\n\n"
        f"{link_text}"
        "Оплати по QR-коду ниже. После оплаты админ вручную проверит платеж и сменит тариф."
    )


def payment_qr_source(config: Config, payment_method: str) -> str:
    method = payment_method_info(config, payment_method)
    if method.get("qr_url"):
        return str(method["qr_url"])
    if method.get("link"):
        return qr_url_for_link(str(method["link"]))
    return ""


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


def plan_button_rows(callback_prefix: str) -> list[list[dict[str, str]]]:
    buttons = [
        {"text": plan_label(plan_id), "callback_data": f"{callback_prefix}:{plan_id}"}
        for plan_id in sorted(SUBSCRIPTION_PLANS, key=int)
    ]
    return [buttons[index : index + 1] for index in range(0, len(buttons), 1)]


def admin_plan_button_rows(request_id: int) -> list[list[dict[str, str]]]:
    buttons = [
        {"text": f"Тариф {plan_label(plan_id)}", "callback_data": f"setplan:{plan_id}:{request_id}"}
        for plan_id in sorted(SUBSCRIPTION_PLANS, key=int)
    ]
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
    railway_public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    default_public_base_url = f"https://{railway_public_domain}" if railway_public_domain else ""

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
        payment_qr_url=_get(raw, "PAYMENT_QR_URL"),
        payment_link=_get(raw, "PAYMENT_LINK"),
        payment_tbank_link=_get(raw, "PAYMENT_TBANK_LINK"),
        remote_state_path=_get(raw, "REMOTE_STATE_PATH", "/usr/local/etc/xray/vpn_approval_state.json"),
        payment_recipient=_get(raw, "PAYMENT_RECIPIENT", "Вадим"),
        payment_banks=_get(raw, "PAYMENT_BANKS", "Сбер / Т-Банк"),
        public_base_url=_get(raw, "PUBLIC_BASE_URL", default_public_base_url).rstrip("/"),
        web_port=int(_get(raw, "PORT", "0") or "0"),
    )


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.remote_backup: Any = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self._write({"last_id": 0, "requests": []})

    def set_remote_backup(self, remote_backup: Any) -> None:
        self.remote_backup = remote_backup

    def _read(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"last_id": 0, "requests": []}
        try:
            with self.db_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError:
            backup_path = self.db_path.with_suffix(self.db_path.suffix + ".broken")
            try:
                self.db_path.replace(backup_path)
            except OSError:
                pass
            logging.exception("Local database is corrupted; moved it to %s", backup_path)
            return {"last_id": 0, "requests": []}
        if not isinstance(data, dict):
            return {"last_id": 0, "requests": []}
        data.setdefault("last_id", 0)
        data.setdefault("requests", [])
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = utc_now_iso()
        tmp_path = self.db_path.with_suffix(self.db_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        tmp_path.replace(self.db_path)
        if self.remote_backup is not None:
            try:
                self.remote_backup(data)
            except Exception:
                logging.exception("Could not save remote database backup")

    def export_data(self) -> dict[str, Any]:
        return self._read()

    def replace_data(self, data: dict[str, Any]) -> None:
        self._write(self._normalize_data(data))

    def pop_admin_list_message_ids(self, chat_id: int) -> list[int]:
        data = self._read()
        lists = data.setdefault("admin_list_messages", {})
        raw_ids = lists.pop(str(chat_id), []) if isinstance(lists, dict) else []
        self._write(data)
        result: list[int] = []
        for item in raw_ids:
            if str(item).isdigit():
                result.append(int(item))
        return result

    def set_admin_list_message_ids(self, chat_id: int, message_ids: list[int]) -> None:
        data = self._read()
        lists = data.setdefault("admin_list_messages", {})
        if not isinstance(lists, dict):
            lists = {}
            data["admin_list_messages"] = lists
        lists[str(chat_id)] = [int(message_id) for message_id in message_ids if int(message_id) > 0]
        self._write(data)

    def merge_remote_data(self, remote_data: dict[str, Any]) -> int:
        if not remote_data:
            return 0
        data = self._read()
        merged = self._merge_data(data, remote_data)
        if merged <= 0:
            return 0
        self._write(data)
        return merged

    @staticmethod
    def _normalize_data(data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data) if isinstance(data, dict) else {}
        requests = normalized.get("requests")
        if not isinstance(requests, list):
            requests = []
        normalized["requests"] = [item for item in requests if isinstance(item, dict)]
        max_id = 0
        for request in normalized["requests"]:
            if str(request.get("id") or "").isdigit():
                max_id = max(max_id, int(request["id"]))
        current_last_id = int(normalized.get("last_id") or 0) if str(normalized.get("last_id") or "").isdigit() else 0
        normalized["last_id"] = max(current_last_id, max_id)
        return normalized

    @staticmethod
    def _request_key(request: dict[str, Any]) -> str:
        email = str(request.get("client_email") or "")
        if email:
            return f"email:{email}"
        chat_id = str(request.get("chat_id") or "")
        request_id = str(request.get("id") or "")
        return f"id:{chat_id}:{request_id}"

    @staticmethod
    def _is_better_request(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        important_fields = ("subscription_until", "plan_id", "plan_devices", "plan_price")
        candidate_score = sum(1 for field in important_fields if candidate.get(field) not in (None, ""))
        current_score = sum(1 for field in important_fields if current.get(field) not in (None, ""))
        if candidate_score != current_score:
            return candidate_score > current_score
        candidate_updated = parse_iso_time(str(candidate.get("updated_at") or candidate.get("decided_at") or candidate.get("created_at") or ""))
        current_updated = parse_iso_time(str(current.get("updated_at") or current.get("decided_at") or current.get("created_at") or ""))
        if candidate_updated and current_updated:
            return candidate_updated > current_updated
        return bool(candidate_updated and not current_updated)

    @classmethod
    def _merge_data(cls, target: dict[str, Any], source: dict[str, Any]) -> int:
        source = cls._normalize_data(source)
        target.setdefault("requests", [])
        existing_by_key = {cls._request_key(request): request for request in target["requests"] if isinstance(request, dict)}
        merged = 0
        for source_request in source["requests"]:
            key = cls._request_key(source_request)
            current = existing_by_key.get(key)
            if current is None:
                target["requests"].append(source_request)
                existing_by_key[key] = source_request
                merged += 1
                continue
            if cls._is_better_request(source_request, current):
                current.update(source_request)
                merged += 1
        target["last_id"] = max(int(target.get("last_id") or 0), int(source.get("last_id") or 0))
        if source.get("sharing_alerts") and not target.get("sharing_alerts"):
            target["sharing_alerts"] = source["sharing_alerts"]
            merged += 1
        return merged

    def create_request(self, chat_id: int, username: str, full_name: str, profile_type: str, plan_id: str = "1") -> int:
        now = utc_now_iso()
        plan = plan_info(plan_id)
        data = self._read()
        for request in reversed(data["requests"]):
            if int(request["chat_id"]) == chat_id and request["status"] == "pending":
                request["profile_type"] = profile_type
                request["plan_id"] = str(plan_id)
                request["plan_devices"] = plan["devices"]
                request["plan_price"] = plan["price"]
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
                "plan_id": str(plan_id),
                "plan_devices": plan["devices"],
                "plan_price": plan["price"],
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

    def list_free_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.list_approved_requests() if request.get("is_free")]

    def import_approved_clients(self, clients: list[dict[str, str]]) -> int:
        data = self._read()
        existing_emails = {str(item.get("client_email") or "") for item in data["requests"]}
        existing_ids = {int(item["id"]) for item in data["requests"] if str(item.get("id") or "").isdigit()}
        imported = 0
        now = utc_now_iso()

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
                    "plan_id": "1",
                    "plan_devices": 1,
                    "plan_price": 200,
                    "client_email": email,
                    "uuid": client_uuid,
                    "created_at": now,
                    "decided_at": now,
                    "subscription_status": "restored_no_deadline",
                    "subscription_until": "",
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

    def set_free_access(self, request_id: int, is_free: bool) -> dict[str, Any] | None:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            request["is_free"] = bool(is_free)
            request["free_access_updated_at"] = utc_now_iso()
            if is_free:
                request["subscription_status"] = "free"
                if not request.get("subscription_until"):
                    request["subscription_until"] = subscription_until(DEFAULT_SUBSCRIPTION_DAYS)
            elif request.get("subscription_status") == "free":
                request["subscription_status"] = "active"
            self._write(data)
            return request
        return None

    def update_payment_status(self, request_id: int, payment_status: str) -> dict[str, Any] | None:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) == request_id:
                request["payment_status"] = payment_status
                self._write(data)
                return request
        return None

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

    def request_plan_change(self, request_id: int, plan_id: str, payment_method: str) -> dict[str, Any] | None:
        plan = plan_info(plan_id)
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            request["pending_plan_id"] = str(plan_id)
            request["pending_plan_devices"] = plan["devices"]
            request["pending_plan_price"] = plan["price"]
            request["pending_plan_payment_method"] = payment_method
            request["pending_plan_requested_at"] = utc_now_iso()
            request["pending_plan_status"] = "waiting_manual_payment"
            self._write(data)
            return request
        return None

    def update_plan_change_payment_status(self, request_id: int, payment_status: str) -> dict[str, Any] | None:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            request["pending_plan_status"] = payment_status
            self._write(data)
            return request
        return None

    def approve_plan_change(self, request_id: int) -> dict[str, Any] | None:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            pending_plan_id = str(request.get("pending_plan_id") or "")
            if pending_plan_id not in SUBSCRIPTION_PLANS:
                return None
            plan = plan_info(pending_plan_id)
            request["plan_id"] = pending_plan_id
            request["plan_devices"] = plan["devices"]
            request["plan_price"] = plan["price"]
            request["plan_changed_at"] = utc_now_iso()
            request["pending_plan_status"] = "approved"
            for field in (
                "pending_plan_id",
                "pending_plan_devices",
                "pending_plan_price",
                "pending_plan_payment_method",
                "pending_plan_requested_at",
            ):
                request.pop(field, None)
            self._write(data)
            return request
        return None

    def set_plan(self, request_id: int, plan_id: str) -> dict[str, Any] | None:
        if plan_id not in SUBSCRIPTION_PLANS:
            return None
        plan = plan_info(plan_id)
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            request["plan_id"] = str(plan_id)
            request["plan_devices"] = plan["devices"]
            request["plan_price"] = plan["price"]
            request["plan_changed_at"] = utc_now_iso()
            for field in (
                "pending_plan_id",
                "pending_plan_devices",
                "pending_plan_price",
                "pending_plan_payment_method",
                "pending_plan_requested_at",
                "pending_plan_status",
            ):
                request.pop(field, None)
            self._write(data)
            return request
        return None

    def reject_plan_change(self, request_id: int) -> dict[str, Any] | None:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            request["pending_plan_status"] = "rejected"
            for field in (
                "pending_plan_id",
                "pending_plan_devices",
                "pending_plan_price",
                "pending_plan_payment_method",
                "pending_plan_requested_at",
            ):
                request.pop(field, None)
            self._write(data)
            return request
        return None

    def find_expired_requests(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        result: list[dict[str, Any]] = []
        for request in self.list_approved_requests():
            if request.get("is_free"):
                continue
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

    def send_message(self, chat_id: int, text: str, reply_markup: str | None = None) -> dict[str, Any]:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload).get("result", {})

    def send_photo(self, chat_id: int, photo: str, caption: str, reply_markup: str | None = None) -> dict[str, Any]:
        payload = {"chat_id": chat_id, "photo": photo, "caption": caption}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendPhoto", payload).get("result", {})

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self._request("getChat", {"chat_id": chat_id}).get("result", {})

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self._request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def safe_answer_callback_query(self, callback_query_id: str, text: str) -> None:
        try:
            self.answer_callback_query(callback_query_id, text)
        except Exception:
            logging.warning("Could not answer callback query", exc_info=True)


def start_routing_web_server(config: Config) -> None:
    if config.web_port <= 0:
        return

    class RoutingHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logging.info("HTTP %s", format % args)

        def _send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in {"/", "/healthz"}:
                self._send_text(200, "ok\n")
                return
            if path != "/happ-routing":
                self._send_text(404, "not found\n")
                return

            routing_link = build_happ_routing_link()
            safe_link = escape(routing_link, quote=True)
            body = (
                "<!doctype html><html><head>"
                '<meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta http-equiv="refresh" content="0;url=' + safe_link + '">'
                "<title>Open Happ</title>"
                "<script>location.replace(" + json.dumps(routing_link) + ");</script>"
                "</head><body>"
                '<p>Открываю Happ...</p>'
                '<p><a href="' + safe_link + '">Нажми здесь, если Happ не открылся автоматически</a></p>'
                "</body></html>"
            )
            self._send_text(200, body, "text/html; charset=utf-8")

    server = HTTPServer(("0.0.0.0", config.web_port), RoutingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Routing web server started on port %s", config.web_port)


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

    def _acquire_config_lock(self, client: paramiko.SSHClient, timeout_seconds: int = 90) -> None:
        lock_dir = "/tmp/vpn-bot-xray-config.lock"
        command = (
            f"end=$(($(date +%s)+{int(timeout_seconds)})); "
            f"while ! mkdir {shlex.quote(lock_dir)} 2>/dev/null; do "
            f"if [ $(date +%s) -ge $end ]; then exit 75; fi; "
            f"sleep 1; "
            f"done"
        )
        rc, out, err = self._run(client, command)
        if rc != 0:
            raise RuntimeError(f"Could not lock Xray config update: {out}{err}")

    def _release_config_lock(self, client: paramiko.SSHClient) -> None:
        try:
            self._run(client, "rmdir /tmp/vpn-bot-xray-config.lock 2>/dev/null || true")
        except Exception:
            try:
                retry_client = self._connect_with_retry(attempts=2, delay=1)
                try:
                    self._run(retry_client, "rmdir /tmp/vpn-bot-xray-config.lock 2>/dev/null || true")
                finally:
                    retry_client.close()
            except Exception:
                logging.warning("Could not release Xray config lock", exc_info=True)

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

    def load_state_backup(self) -> dict[str, Any] | None:
        client = self._connect()
        sftp = client.open_sftp()
        try:
            try:
                with sftp.open(self.config.remote_state_path, "r") as fh:
                    state = json.load(fh)
            except FileNotFoundError:
                return None
            except OSError:
                return None
            if isinstance(state, dict):
                return state
            return None
        finally:
            sftp.close()
            client.close()

    def save_state_backup(self, data: dict[str, Any]) -> None:
        client = self._connect()
        sftp = client.open_sftp()
        remote_path = self.config.remote_state_path
        remote_dir = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "."
        tmp_path = f"{remote_path}.tmp"
        try:
            rc, out, err = self._run(client, f"mkdir -p {shlex.quote(remote_dir)} && chmod 700 {shlex.quote(remote_dir)}")
            if rc != 0:
                raise RuntimeError(f"Could not create remote state directory: {out}{err}")
            with sftp.open(tmp_path, "w") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            rc, out, err = self._run(
                client,
                f"install -m 600 {shlex.quote(tmp_path)} {shlex.quote(remote_path)} && rm -f {shlex.quote(tmp_path)}",
            )
            if rc != 0:
                raise RuntimeError(f"Could not install remote state backup: {out}{err}")
        finally:
            sftp.close()
            client.close()

    def save_client(self, client_email: str, client_uuid: str) -> None:
        client = self._connect()
        sftp = None
        lock_acquired = False
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        backup_path = f"{self.config.backup_dir}/config.json.backup-vpn-bot-{stamp}"
        try:
            self._acquire_config_lock(client)
            lock_acquired = True
            sftp = client.open_sftp()
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
            if lock_acquired:
                self._release_config_lock(client)
            client.close()

    def remove_client(self, client_email: str) -> None:
        client = self._connect()
        sftp = None
        lock_acquired = False
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        backup_path = f"{self.config.backup_dir}/config.json.backup-vpn-disable-{stamp}"
        try:
            self._acquire_config_lock(client)
            lock_acquired = True
            sftp = client.open_sftp()
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
            if lock_acquired:
                self._release_config_lock(client)
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
                [{"text": "Получить VPN"}, {"text": "Моя подписка"}],
                [{"text": "Список клиентов"}, {"text": "Бесплатные клиенты"}],
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
                [{"text": "Получить VPN"}, {"text": "Моя подписка"}],
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


def plan_choice_markup() -> str:
    return json.dumps(
        {"inline_keyboard": plan_button_rows("plan")},
        ensure_ascii=False,
    )


def plan_change_choice_markup() -> str:
    return json.dumps(
        {"inline_keyboard": plan_button_rows("changeplan")},
        ensure_ascii=False,
    )


def profile_choice_for_plan_markup(plan_id: str) -> str:
    return json.dumps(
        {"inline_keyboard": profile_button_rows(f"request:{plan_id}")},
        ensure_ascii=False,
    )


def payment_method_markup(plan_id: str, profile_type: str) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Сбер", "callback_data": f"paymethod:sber:{plan_id}:{profile_type}"},
                    {"text": "Т-Банк", "callback_data": f"paymethod:tbank:{plan_id}:{profile_type}"},
                ]
            ]
        },
        ensure_ascii=False,
    )


def plan_change_payment_method_markup(request_id: int, plan_id: str) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Сбер", "callback_data": f"changeplanpay:sber:{request_id}:{plan_id}"},
                    {"text": "Т-Банк", "callback_data": f"changeplanpay:tbank:{request_id}:{plan_id}"},
                ]
            ]
        },
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


def admin_plan_change_markup(request_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "Одобрить смену тарифа", "callback_data": f"approve_plan:{request_id}"},
                    {"text": "Отклонить", "callback_data": f"reject_plan:{request_id}"},
                ]
            ]
        },
        ensure_ascii=False,
    )


def payment_markup(request_id: int, payment_url: str) -> str:
    rows = []
    if payment_url:
        rows.append([{"text": "Открыть QR оплаты", "url": payment_url}])
    rows.append([{"text": "Я оплатил", "callback_data": f"paid:{request_id}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def plan_change_payment_markup(request_id: int, payment_url: str) -> str:
    rows = []
    if payment_url:
        rows.append([{"text": "Открыть QR оплаты", "url": payment_url}])
    rows.append([{"text": "Я оплатил смену тарифа", "callback_data": f"planpaid:{request_id}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def routing_open_markup(open_url: str) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": "Открыть в Happ", "url": open_url}],
            ]
        },
        ensure_ascii=False,
    )


def subscription_actions_markup(request_id: int) -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": "Перевыпустить ссылку", "callback_data": f"show_reissue:{request_id}"}],
                [{"text": "Сменить тариф", "callback_data": "show_change_plan"}],
                [{"text": "Маршрутизация Happ", "callback_data": "send_routing"}],
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


def admin_client_markup(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    rows = []
    if not row.get("is_free"):
        rows.append(
            [
                {"text": "+7 дней", "callback_data": f"extend:7:{request_id}"},
                {"text": "+30 дней", "callback_data": f"extend:30:{request_id}"},
                {"text": "+90 дней", "callback_data": f"extend:90:{request_id}"},
            ]
        )
    rows.extend(admin_plan_button_rows(request_id))
    rows.extend(profile_button_rows("reissue", request_id))
    if row.get("is_free"):
        rows.append([{"text": "Убрать бесплатный доступ", "callback_data": f"free:off:{request_id}"}])
    else:
        rows.append([{"text": "Сделать бесплатным", "callback_data": f"free:on:{request_id}"}])
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


def subscription_display(row: dict[str, Any]) -> str:
    if row.get("is_free"):
        return "бесплатный доступ, без автоотключения"
    return format_subscription(str(row.get("subscription_until") or ""))


def format_client_list(rows: list[dict[str, Any]], last_seen: dict[str, str]) -> str:
    if not rows:
        return "Одобренных клиентов пока нет."

    lines = [f"Клиенты VPN: {len(rows)}"]
    for number, row in enumerate(rows, start=1):
        profile_type = profile_label(str(row.get("profile_type") or "default"))
        email = str(row.get("client_email") or "")
        username = format_username(str(row.get("username") or ""))
        subscription = subscription_display(row)
        plan = plan_label(row.get("plan_id"))
        lines.append(
            f"{number}. {username} | {profile_type} | тариф: {plan} | подписка: {subscription} | активность: {format_age(last_seen.get(email))}"
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
    subscription_text = subscription_display(row)
    free_text = "да" if row.get("is_free") else "нет"
    plan = plan_label(row.get("plan_id"))
    restored = "да" if row.get("restored_from_xray") else "нет"
    return (
        f"Клиент #{number}\n"
        f"Статус: {status}\n"
        f"Подписка: {subscription_status}, {subscription_text}\n"
        f"Бесплатный доступ: {free_text}\n"
        f"Тариф: {plan}\n"
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


def delete_callback_message(bot: TelegramBot, callback_query: dict[str, Any]) -> None:
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    message_id = int(message.get("message_id") or 0)
    if not chat_id or not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        logging.info("Could not delete callback message chat_id=%s message_id=%s", chat_id, message_id)


def message_id_from_result(result: dict[str, Any]) -> int:
    message_id = result.get("message_id") if isinstance(result, dict) else 0
    return int(message_id or 0) if str(message_id or "").isdigit() else 0


def clear_previous_admin_list_messages(bot: TelegramBot, store: Store, chat_id: int) -> None:
    for message_id in store.pop_admin_list_message_ids(chat_id):
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            logging.info("Could not delete previous admin list message chat_id=%s message_id=%s", chat_id, message_id)


def remember_admin_list_messages(store: Store, chat_id: int, sent_messages: list[dict[str, Any]]) -> None:
    message_ids = [message_id_from_result(item) for item in sent_messages]
    store.set_admin_list_message_ids(chat_id, [message_id for message_id in message_ids if message_id])


def send_admin_result(bot: TelegramBot, admin_chat_id: int, user_chat_id: int, user_text: str, admin_text: str) -> None:
    if admin_chat_id == user_chat_id:
        bot.send_message(admin_chat_id, user_text)
        return
    bot.send_message(user_chat_id, user_text)
    bot.send_message(admin_chat_id, admin_text)


def send_payment_instructions(
    bot: TelegramBot,
    config: Config,
    chat_id: int,
    request_id: int,
    plan_id: str | int | None,
    profile_type: str,
    payment_method: str,
    reply_markup: str | None = None,
) -> None:
    text = payment_text(config, request_id, plan_id, profile_type, payment_method)
    qr_source = payment_qr_source(config, payment_method)
    if qr_source:
        try:
            bot.send_photo(chat_id, qr_source, text, reply_markup)
            return
        except Exception:
            logging.exception("Could not send manual payment QR")
    bot.send_message(chat_id, text + "\n\nQR оплаты пока не настроен. Напиши админу, чтобы он прислал QR вручную.", reply_markup)


def send_plan_change_payment_instructions(
    bot: TelegramBot,
    config: Config,
    chat_id: int,
    request_id: int,
    plan_id: str | int | None,
    payment_method: str,
    reply_markup: str | None = None,
) -> None:
    text = plan_change_payment_text(config, request_id, plan_id, payment_method)
    qr_source = payment_qr_source(config, payment_method)
    if qr_source:
        try:
            bot.send_photo(chat_id, qr_source, text, reply_markup)
            return
        except Exception:
            logging.exception("Could not send plan change payment QR")
    bot.send_message(chat_id, text + "\n\nQR оплаты пока не настроен. Напиши админу, чтобы он прислал QR вручную.", reply_markup)


def send_routing_instructions(bot: TelegramBot, config: Config, chat_id: int, reply_markup: str | None = None) -> None:
    routing_link = build_happ_routing_link()
    open_url = happ_routing_redirect_url(config)
    caption = (
        "Правила маршрутизации для Happ:\n\n"
        "TikTok и его CDN будут идти через VPN.\n"
        "Российские сервисы, банки, VK, Госуслуги, Яндекс и капчи будут идти напрямую, мимо VPN.\n\n"
        "Отсканируй QR через Happ или нажми кнопку ниже, чтобы открыть Happ."
    )
    qr_source = qr_url_for_link(routing_link)
    if not open_url:
        bot.send_photo(chat_id, qr_source, caption, reply_markup)
        bot.send_message(
            chat_id,
            "Кнопка открытия Happ будет доступна после настройки PUBLIC_BASE_URL на Railway.",
            reply_markup,
        )
        return
    try:
        bot.send_photo(chat_id, qr_source, caption, routing_open_markup(open_url))
    except Exception:
        logging.exception("Could not send Happ routing QR")
        bot.send_message(
            chat_id,
            "Не смог отправить QR-код маршрутизации Happ. Нажми кнопку ниже, чтобы открыть Happ.",
            routing_open_markup(open_url),
        )


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


def handle_message(
    bot: TelegramBot,
    store: Store,
    config: Config,
    manager: XrayManager,
    message: dict[str, Any],
) -> None:
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
            + price_list_text()
            + "\n\n"
            "Команды:\n"
            "/vpn - выбрать оператора и отправить заявку на VPN\n"
            "/vpn_status ID - проверить заявку\n"
            "/reissue - перевыпуск ссылки\n"
            "/change_plan - сменить тариф\n"
            "/routing - правила TikTok/Яндекс/банки\n"
            "/subscription - срок подписки",
            reply_markup,
        )
        return

    if text.startswith("/routing") or text.lower() == "маршрутизация":
        send_routing_instructions(bot, config, chat_id)
        return

    if text.startswith("/clients") or text.lower() == "список клиентов":
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        clear_previous_admin_list_messages(bot, store, chat_id)
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
            sent = bot.send_message(chat_id, f"Не смог получить активность с сервера: {error_text}", admin_reply_markup())
            remember_admin_list_messages(store, chat_id, [sent])
            return
        if not rows:
            sent = bot.send_message(chat_id, format_client_list(rows, last_seen), admin_reply_markup())
            remember_admin_list_messages(store, chat_id, [sent])
            return
        sent_messages = [bot.send_message(chat_id, format_client_list(rows, last_seen), admin_reply_markup())]
        for number, row in enumerate(rows, start=1):
            sent_messages.append(bot.send_message(chat_id, format_client_card(row, last_seen, number), admin_client_markup(row)))
        remember_admin_list_messages(store, chat_id, sent_messages)
        return

    if text.startswith("/free_clients") or text.lower() == "бесплатные клиенты":
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        clear_previous_admin_list_messages(bot, store, chat_id)
        rows = refresh_missing_user_info(bot, store, store.list_free_requests())
        try:
            last_seen = manager.get_last_seen_by_email([str(row["client_email"]) for row in rows])
        except Exception:
            logging.exception("Failed to load free client activity")
            last_seen = {}
        if not rows:
            sent = bot.send_message(chat_id, "Бесплатных клиентов пока нет. Добавить можно из карточки клиента в «Список клиентов».", admin_reply_markup())
            remember_admin_list_messages(store, chat_id, [sent])
            return
        sent_messages = [bot.send_message(chat_id, "Бесплатные клиенты:", admin_reply_markup())]
        for number, row in enumerate(rows, start=1):
            sent_messages.append(bot.send_message(chat_id, format_client_card(row, last_seen, number), admin_client_markup(row)))
        remember_admin_list_messages(store, chat_id, sent_messages)
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

    if text.startswith("/change_plan") or text.lower() == "сменить тариф":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing or existing.get("status") != "approved":
            bot.send_message(chat_id, "У тебя пока нет активного VPN-профиля. Сначала оформи VPN через /vpn.")
            return
        current_plan = plan_label(existing.get("plan_id"))
        bot.send_message(
            chat_id,
            f"Текущий тариф профиля #{existing['id']}: {current_plan}.\nВыбери новый тариф:",
            plan_change_choice_markup(),
        )
        return

    if text.startswith("/subscription") or text.lower() == "моя подписка":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing or existing.get("status") != "approved":
            bot.send_message(
                chat_id,
                "У тебя пока нет активной VPN-подписки.\n\n" + price_list_text(),
                user_reply_markup() if chat_id not in config.admin_chat_ids else admin_reply_markup(),
            )
            return
        bot.send_message(
            chat_id,
            f"Твоя VPN-подписка: {subscription_display(existing)}.\n"
            f"Профиль: #{existing['id']}, {profile_label(str(existing.get('profile_type') or 'default'))}.\n"
            f"Тариф: {plan_label(existing.get('plan_id'))}.\n"
            f"Статус заявки: {existing['status']}.\n\n"
            f"{price_list_text()}",
            subscription_actions_markup(int(existing["id"])),
        )
        return

    if text.startswith("/price") or text.lower() == "прайс лист":
        existing = store.get_active_request_by_chat_id(chat_id)
        if existing and existing.get("status") == "approved":
            bot.send_message(
                chat_id,
                f"Твоя VPN-подписка: {subscription_display(existing)}.\n"
                f"Профиль: #{existing['id']}, {profile_label(str(existing.get('profile_type') or 'default'))}.\n"
                f"Тариф: {plan_label(existing.get('plan_id'))}.\n"
                f"Статус заявки: {existing['status']}.\n\n"
                f"{price_list_text()}",
                subscription_actions_markup(int(existing["id"])),
            )
            return
        bot.send_message(chat_id, price_list_text(), user_reply_markup() if chat_id not in config.admin_chat_ids else admin_reply_markup())
        return

    if text.lower() == "статус заявки":
        existing = store.get_active_request_by_chat_id(chat_id)
        if not existing:
            bot.send_message(chat_id, "Заявок пока нет. Нажми «Получить VPN».", user_reply_markup())
            return
        bot.send_message(
            chat_id,
            f"Статус заявки #{existing['id']}: {existing['status']}\n"
            f"Подписка: {subscription_display(existing)}\n"
            f"Тариф: {plan_label(existing.get('plan_id'))}",
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
            f"Подписка: {subscription_display(row)}\n"
            f"Тариф: {plan_label(row.get('plan_id'))}",
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
            "Выбери тариф подписки:\n\n" + price_list_text(),
            plan_choice_markup(),
        )
        return

    bot.send_message(chat_id, "Напиши /vpn, чтобы запросить VPN-доступ.")


def handle_callback(
    bot: TelegramBot,
    store: Store,
    config: Config,
    manager: XrayManager,
    callback_query: dict[str, Any],
) -> None:
    callback_id = str(callback_query.get("id") or "")
    from_user = callback_query.get("from") or {}
    user_chat_id = int(from_user.get("id") or 0)
    payload = str(callback_query.get("data") or "")

    parts = payload.split(":")
    if not parts:
        return
    delete_callback_message(bot, callback_query)

    if parts[0] == "plan" and len(parts) == 2:
        plan_id = parts[1]
        if plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Неизвестный тариф.")
            return
        bot.answer_callback_query(callback_id, "Тариф выбран.")
        bot.send_message(
            user_chat_id,
            f"Тариф: {plan_label(plan_id)}.\nТеперь выбери своего оператора. Если не знаешь, что выбрать, нажми «Обычный оператор».",
            profile_choice_for_plan_markup(plan_id),
        )
        return

    if parts[0] == "request" and len(parts) in {2, 3}:
        plan_id = parts[1] if len(parts) == 3 else "1"
        profile_type = parts[2] if len(parts) == 3 else parts[1]
        if not is_profile_type(profile_type):
            bot.answer_callback_query(callback_id, "Неизвестный тип профиля.")
            return
        if plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Неизвестный тариф.")
            return

        bot.answer_callback_query(callback_id, "Оператор выбран.")
        bot.send_message(
            user_chat_id,
            "Выбери, через какой банк тебе удобнее оплатить:",
            payment_method_markup(plan_id, profile_type),
        )
        return

    if parts[0] == "send_routing":
        bot.answer_callback_query(callback_id, "Отправляю правила.")
        send_routing_instructions(bot, config, user_chat_id)
        return

    if parts[0] == "show_change_plan":
        existing = store.get_active_request_by_chat_id(user_chat_id)
        if not existing or existing.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Выбери тариф.")
        bot.send_message(
            user_chat_id,
            f"Текущий тариф профиля #{existing['id']}: {plan_label(existing.get('plan_id'))}.\nВыбери новый тариф:",
            plan_change_choice_markup(),
        )
        return

    if parts[0] == "show_reissue" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or int(row.get("chat_id") or 0) != user_chat_id or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Выбери тип ссылки.")
        bot.send_message(
            user_chat_id,
            f"Выбери, какую ссылку перевыпустить для профиля #{request_id}.",
            reissue_choice_markup(request_id),
        )
        return

    if parts[0] == "paymethod" and len(parts) == 4:
        payment_method = parts[1]
        plan_id = parts[2]
        profile_type = parts[3]
        if payment_method not in payment_methods(config):
            bot.answer_callback_query(callback_id, "Этот способ оплаты не настроен.")
            return
        if plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Неизвестный тариф.")
            return
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
        request_id = store.create_request(user_chat_id, username, full_name, profile_type, plan_id)
        selected_profile_label = profile_label(profile_type)
        selected_plan_label = plan_label(plan_id)
        bot.answer_callback_query(callback_id, "Заявка отправлена.")
        store.update_payment_status(request_id, "waiting_manual_payment")
        payment_method_name = payment_method_info(config, payment_method)["label"]
        payment_info = payment_method_info(config, payment_method)
        send_payment_instructions(
            bot,
            config,
            user_chat_id,
            request_id,
            plan_id,
            profile_type,
            payment_method,
            payment_markup(request_id, payment_info.get("link", "") or payment_info.get("qr_url", "")),
        )

        admin_text = (
            f"Новая VPN-заявка #{request_id}\n"
            f"Тип: {selected_profile_label}\n"
            f"Тариф: {selected_plan_label}\n"
            f"Оплата через: {payment_method_name}\n"
            f"Сумма: {plan_info(plan_id)['price']} руб\n"
            f"Комментарий оплаты: {payment_comment(request_id)}\n"
            "Перед одобрением вручную проверь оплату по QR/СБП.\n"
            f"Chat ID: {user_chat_id}\n"
            f"Username: @{username if username else '-'}\n"
            f"Name: {full_name or '-'}"
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, admin_text, admin_request_markup(request_id))
        return

    if parts[0] == "changeplan" and len(parts) == 2:
        plan_id = parts[1]
        if plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Неизвестный тариф.")
            return
        existing = store.get_active_request_by_chat_id(user_chat_id)
        if not existing or existing.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        if str(existing.get("plan_id") or "1") == plan_id:
            bot.answer_callback_query(callback_id, "Этот тариф уже подключен.")
            bot.send_message(user_chat_id, f"У тебя уже подключен тариф: {plan_label(plan_id)}.")
            return
        bot.answer_callback_query(callback_id, "Тариф выбран.")
        bot.send_message(
            user_chat_id,
            f"Новый тариф: {plan_label(plan_id)}.\nВыбери, через какой банк тебе удобнее оплатить смену тарифа:",
            plan_change_payment_method_markup(int(existing["id"]), plan_id),
        )
        return

    if parts[0] == "changeplanpay" and len(parts) == 4 and parts[2].isdigit():
        payment_method = parts[1]
        request_id = int(parts[2])
        plan_id = parts[3]
        if payment_method not in payment_methods(config):
            bot.answer_callback_query(callback_id, "Этот способ оплаты не настроен.")
            return
        if plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Неизвестный тариф.")
            return
        row = store.get_request(request_id)
        if not row or int(row.get("chat_id") or 0) != user_chat_id or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        if str(row.get("plan_id") or "1") == plan_id:
            bot.answer_callback_query(callback_id, "Этот тариф уже подключен.")
            return

        updated = store.request_plan_change(request_id, plan_id, payment_method)
        if not updated:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        payment_info = payment_method_info(config, payment_method)
        payment_method_name = payment_info["label"]
        old_plan = plan_label(row.get("plan_id"))
        new_plan = plan_label(plan_id)
        bot.answer_callback_query(callback_id, "Заявка на смену тарифа создана.")
        send_plan_change_payment_instructions(
            bot,
            config,
            user_chat_id,
            request_id,
            plan_id,
            payment_method,
            plan_change_payment_markup(request_id, payment_info.get("link", "") or payment_info.get("qr_url", "")),
        )
        username, full_name = user_display(from_user)
        admin_text = (
            f"Заявка на смену тарифа VPN #{request_id}\n"
            f"Старый тариф: {old_plan}\n"
            f"Новый тариф: {new_plan}\n"
            f"Оплата через: {payment_method_name}\n"
            f"Сумма: {plan_info(plan_id)['price']} руб\n"
            f"Комментарий оплаты: {payment_comment(request_id)} тариф\n"
            "Перед одобрением вручную проверь оплату.\n"
            f"Chat ID: {user_chat_id}\n"
            f"Username: @{username if username else '-'}\n"
            f"Name: {full_name or '-'}"
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, admin_text, admin_plan_change_markup(request_id))
        return

    if parts[0] == "planpaid" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or int(row.get("chat_id") or 0) != user_chat_id:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        pending_plan_id = str(row.get("pending_plan_id") or "")
        if pending_plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Нет заявки на смену тарифа.")
            return
        store.update_plan_change_payment_status(request_id, "user_marked_paid")
        bot.answer_callback_query(callback_id, "Сообщил админу.")
        return

    if parts[0] == "paid" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or int(row.get("chat_id") or 0) != user_chat_id:
            bot.answer_callback_query(callback_id, "Заявка не найдена.")
            return
        store.update_payment_status(request_id, "user_marked_paid")
        bot.answer_callback_query(callback_id, "Сообщил админу.")
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

    if parts[0] == "setplan" and len(parts) == 3 and parts[2].isdigit():
        plan_id = parts[1]
        request_id = int(parts[2])
        if plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Неизвестный тариф.")
            return
        row = store.get_request(request_id)
        if not row or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        old_plan = plan_label(row.get("plan_id"))
        new_plan = plan_label(plan_id)
        if str(row.get("plan_id") or "1") == plan_id:
            bot.answer_callback_query(callback_id, "Этот тариф уже подключен.")
            return
        updated = store.set_plan(request_id, plan_id)
        if not updated:
            bot.answer_callback_query(callback_id, "Не смог сменить тариф.")
            return
        bot.answer_callback_query(callback_id, "Тариф изменён.")
        send_admin_result(
            bot,
            user_chat_id,
            int(updated["chat_id"]),
            f"Тариф VPN-профиля #{request_id} изменён админом: {old_plan} -> {new_plan}. Ссылка осталась прежней.",
            f"Готово. Тариф профиля #{request_id} изменён: {old_plan} -> {new_plan}.",
        )
        return

    if parts[0] == "approve_plan" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        pending_plan_id = str(row.get("pending_plan_id") or "")
        if pending_plan_id not in SUBSCRIPTION_PLANS:
            bot.answer_callback_query(callback_id, "Нет заявки на смену тарифа.")
            return
        old_plan = plan_label(row.get("plan_id"))
        new_plan = plan_label(pending_plan_id)
        updated = store.approve_plan_change(request_id)
        if not updated:
            bot.answer_callback_query(callback_id, "Не смог сменить тариф.")
            return
        bot.answer_callback_query(callback_id, "Тариф изменён.")
        send_admin_result(
            bot,
            user_chat_id,
            int(updated["chat_id"]),
            f"Тариф VPN-профиля #{request_id} изменён: {old_plan} -> {new_plan}. Ссылка осталась прежней.",
            f"Готово. Тариф профиля #{request_id} изменён: {old_plan} -> {new_plan}.",
        )
        return

    if parts[0] == "reject_plan" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        updated = store.reject_plan_change(request_id)
        if not updated:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Смена тарифа отклонена.")
        send_admin_result(
            bot,
            user_chat_id,
            int(updated["chat_id"]),
            f"Админ отклонил смену тарифа профиля #{request_id}. Текущий тариф не изменился.",
            f"Смена тарифа профиля #{request_id} отклонена.",
        )
        return

    if parts[0] == "free" and len(parts) == 3 and parts[2].isdigit():
        is_free = parts[1] == "on"
        request_id = int(parts[2])
        row = store.get_request(request_id)
        if not row or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        updated = store.set_free_access(request_id, is_free)
        if not updated:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        if is_free:
            bot.answer_callback_query(callback_id, "Добавлен в бесплатные.")
            bot.send_message(user_chat_id, f"Профиль #{request_id} добавлен в бесплатные клиенты. Автоотключение по подписке не сработает.")
        else:
            bot.answer_callback_query(callback_id, "Убран из бесплатных.")
            bot.send_message(user_chat_id, f"Профиль #{request_id} убран из бесплатных клиентов.")
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
        selected_plan_label = plan_label(row.get("plan_id"))

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
        send_admin_result(
            bot,
            user_chat_id,
            int(row["chat_id"]),
            f"Заявка одобрена. Тариф: {selected_plan_label}. Подписка: {subscription_text}.\n\nТвоя VPN-ссылка:\n\n" + link,
            f"Готово. Заявка #{request_id} одобрена как {profile_label(profile_type)}. Тариф: {selected_plan_label}. Подписка: {subscription_text}.",
        )
        send_routing_instructions(bot, config, int(row["chat_id"]))


def check_sharing_alerts(bot: TelegramBot, store: Store, config: Config, manager: XrayManager) -> None:
    rows = store.list_approved_requests()
    email_to_row = {str(row.get("client_email") or ""): row for row in rows if row.get("client_email")}
    if not email_to_row:
        return

    recent_ips = manager.get_recent_ips_by_email(list(email_to_row), SHARING_LOOKBACK_MINUTES)
    for email, ips in recent_ips.items():
        row = email_to_row[email]
        allowed_devices = int(row.get("plan_devices") or plan_info(row.get("plan_id"))["devices"])
        alert_ip_limit = allowed_devices + SHARING_IP_GRACE
        if len(ips) <= alert_ip_limit or not store.should_send_sharing_alert(email):
            continue
        request_id = int(row["id"])
        username = str(row.get("username") or "-")
        profile_type = str(row.get("profile_type") or "default")
        plan = plan_label(row.get("plan_id"))
        ip_list = ", ".join(sorted(ips))
        text = (
            f"Подозрение на шаринг VPN #{request_id}\n"
            f"Пользователь: @{username}\n"
            f"Тип: {profile_label(profile_type)}\n"
            f"Тариф: {plan}\n"
            f"Лимит устройств: {allowed_devices}\n"
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
    start_routing_web_server(config)
    store = Store(config.db_path)
    bot = TelegramBot(config.telegram_token)
    manager = XrayManager(config)
    remote_state_checked = False
    try:
        remote_state = manager.load_state_backup()
        remote_state_checked = True
        if remote_state:
            merged = store.merge_remote_data(remote_state)
            if merged:
                logging.info("Merged %s records from remote state backup", merged)
    except Exception:
        logging.exception("Could not restore remote state backup")
    store.set_remote_backup(manager.save_state_backup)
    try:
        current_state = store.export_data()
        if remote_state_checked or current_state.get("requests"):
            manager.save_state_backup(current_state)
    except Exception:
        logging.exception("Could not initialize remote state backup")
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
