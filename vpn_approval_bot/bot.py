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
from hmac import compare_digest
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

import paramiko


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
POLL_TIMEOUT = 30
SHARING_CHECK_INTERVAL_SECONDS = 60
SUBSCRIPTION_CHECK_INTERVAL_SECONDS = 600
INACTIVE_CHECK_INTERVAL_SECONDS = 86400
SHARING_LOOKBACK_MINUTES = 10
SHARING_IP_GRACE = 1
SHARING_ALERT_COOLDOWN_MINUTES = 30
DEFAULT_SUBSCRIPTION_DAYS = 30
SUBSCRIPTION_NOTICE_DAYS = (3, 1)
INACTIVE_CLEANUP_DAYS = 60
LAST_SEEN_JOURNAL_LINES = 1500
USAGE_STATS_JOURNAL_LINES = 5000
XRAY_USAGE_RE = re.compile(
    r"from (?:tcp:)?(?P<ip>\d+\.\d+\.\d+\.\d+):\d+ accepted .* email: (?P<email>\S+)"
)
TG_CLIENT_EMAIL_RE = re.compile(r"^tg-(?P<chat_id>\d+)-(?P<request_id>\d+)(?:-d(?P<device_id>\d+))?$")


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
    admin_web_token: str
    reserve_vpn_host: str


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
    "domain:xn--p1ai",
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
    "domain:icq.com",
    "domain:yandex.ru",
    "domain:yandex.com",
    "domain:yandex.net",
    "domain:yandex.st",
    "domain:ya.ru",
    "domain:ya.cc",
    "domain:yastatic.net",
    "domain:yastat.net",
    "domain:yandexcloud.net",
    "domain:yandexcloud.ru",
    "domain:yandexbank.ru",
    "domain:yndx.net",
    "domain:dzen.ru",
    "domain:kinopoisk.ru",
    "domain:rutube.ru",
    "domain:pladform.ru",
    "domain:premier.one",
    "domain:ivi.ru",
    "domain:okko.tv",
    "domain:more.tv",
    "domain:smotrim.ru",
    "domain:start.ru",
    "domain:wink.ru",
    "domain:kion.ru",
    "domain:avito.ru",
    "domain:cdnvideo.ru",
    "domain:cdnvideo.net",
    "domain:ozon.ru",
    "domain:ozon.by",
    "domain:ozon.com",
    "domain:ozon.travel",
    "domain:ozonbank.ru",
    "domain:ozonusercontent.com",
    "domain:cdn-ozon.ru",
    "domain:ozone.ru",
    "domain:wildberries.ru",
    "domain:wb.ru",
    "domain:wbstatic.ru",
    "domain:wbbasket.ru",
    "domain:wbstatic.net",
    "domain:wildberries.by",
    "domain:wildberries.kz",
    "domain:wildberries.am",
    "domain:wildberries.uz",
    "domain:wildberries.tj",
    "domain:wildberries.kg",
    "domain:wildberries.az",
    "domain:wildberries.ge",
    "domain:wildberries.com",
    "domain:lamoda.ru",
    "domain:megamarket.ru",
    "domain:sbermegamarket.ru",
    "domain:yandex.market",
    "domain:market.yandex.ru",
    "domain:beru.ru",
    "domain:goods.ru",
    "domain:kazanexpress.ru",
    "domain:magnitmarket.ru",
    "domain:kuper.ru",
    "domain:samokat.ru",
    "domain:delivery-club.ru",
    "domain:lavka.yandex.ru",
    "domain:eda.yandex.ru",
    "domain:sbermarket.ru",
    "domain:vprok.ru",
    "domain:utkonos.ru",
    "domain:auchan.ru",
    "domain:lenta.com",
    "domain:lenta.ru",
    "domain:metro-cc.ru",
    "domain:okeydostavka.ru",
    "domain:perekrestok.ru",
    "domain:pyaterochka.ru",
    "domain:magnit.ru",
    "domain:dixy.ru",
    "domain:dns-shop.ru",
    "domain:mvideo.ru",
    "domain:eldorado.ru",
    "domain:citilink.ru",
    "domain:holodilnik.ru",
    "domain:technopark.ru",
    "domain:restore.ru",
    "domain:re-store.ru",
    "domain:goldapple.ru",
    "domain:letu.ru",
    "domain:rivegauche.ru",
    "domain:detmir.ru",
    "domain:sportmaster.ru",
    "domain:apteka.ru",
    "domain:eapteka.ru",
    "domain:uteka.ru",
    "domain:asna.ru",
    "domain:2gis.ru",
    "domain:2gis.com",
    "domain:doublegis.com",
    "domain:hh.ru",
    "domain:rabota.ru",
    "domain:superjob.ru",
    "domain:profi.ru",
    "domain:kp.ru",
    "domain:rbc.ru",
    "domain:lenta.ru",
    "domain:ria.ru",
    "domain:tass.ru",
    "domain:fontanka.ru",
    "domain:kommersant.ru",
    "domain:vedomosti.ru",
    "domain:mos.ru",
    "domain:mosreg.ru",
    "domain:spb.ru",
    "domain:lk.gosuslugi.ru",
    "domain:pfr.gov.ru",
    "domain:sfr.gov.ru",
    "domain:esia.gosuslugi.ru",
    "domain:pos.gosuslugi.ru",
    "domain:gu.spb.ru",
    "domain:zakupki.gov.ru",
    "domain:rosreestr.gov.ru",
    "domain:customs.gov.ru",
    "domain:cbr.ru",
    "domain:mironline.ru",
    "domain:nspk.ru",
    "domain:sberbank.ru",
    "domain:sber.ru",
    "domain:online.sberbank.ru",
    "domain:sberbankonline.ru",
    "domain:sberbank.com",
    "domain:sberpay.ru",
    "domain:sberdevices.ru",
    "domain:sbermarket.ru",
    "domain:tbank.ru",
    "domain:tinkoff.ru",
    "domain:tinkoffbank.ru",
    "domain:tcsbank.ru",
    "domain:alfabank.ru",
    "domain:alfastrah.ru",
    "domain:vtb.ru",
    "domain:vtb24.ru",
    "domain:gazprombank.ru",
    "domain:gpbl.ru",
    "domain:raiffeisen.ru",
    "domain:raiffeisenbank.ru",
    "domain:pochtabank.ru",
    "domain:mkb.ru",
    "domain:open.ru",
    "domain:psbank.ru",
    "domain:rsb.ru",
    "domain:rncb.ru",
    "domain:uralsib.ru",
    "domain:domrfbank.ru",
    "domain:banki.ru",
    "domain:sravni.ru",
    "domain:sovcombank.ru",
    "domain:halvacard.ru",
    "domain:akbars.ru",
    "domain:rosbank.ru",
    "domain:otpbank.ru",
    "domain:rencredit.ru",
    "domain:homecredit.ru",
    "domain:modulbank.ru",
    "domain:tochka.com",
    "domain:qiwi.com",
    "domain:yoomoney.ru",
    "domain:gosuslugi.ru",
    "domain:nalog.gov.ru",
    "domain:fns.ru",
    "domain:lkfl2.nalog.ru",
    "domain:beeline.ru",
    "domain:mts.ru",
    "domain:mgts.ru",
    "domain:megafon.ru",
    "domain:tele2.ru",
    "domain:yota.ru",
    "domain:rt.ru",
    "domain:rostelecom.ru",
    "domain:dom.ru",
    "domain:cloudflare-dns.com",
    "domain:dns.google",
    "domain:msftconnecttest.com",
    "domain:msftncsi.com",
    "domain:connectivitycheck.gstatic.com",
    "domain:connectivitycheck.android.com",
    "domain:captive.apple.com",
    "domain:apple.com",
    "domain:icloud.com",
    "domain:cdn-apple.com",
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


def device_label(device: dict[str, Any]) -> str:
    name = str(device.get("name") or "").strip()
    if name:
        return name
    device_id = int(device.get("device_id") or 1)
    return f"Устройство {device_id}"


def request_devices(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_devices = row.get("devices")
    devices: list[dict[str, Any]] = []
    if isinstance(raw_devices, list):
        for item in raw_devices:
            if not isinstance(item, dict):
                continue
            email = str(item.get("client_email") or "")
            client_uuid = str(item.get("uuid") or "")
            if not email or not client_uuid:
                continue
            device_id = int(item.get("device_id") or len(devices) + 1)
            devices.append(
                {
                    "device_id": device_id,
                    "name": str(item.get("name") or f"Устройство {device_id}"),
                    "client_email": email,
                    "uuid": client_uuid,
                    "profile_type": str(item.get("profile_type") or row.get("profile_type") or "default"),
                    "created_at": str(item.get("created_at") or row.get("decided_at") or row.get("created_at") or ""),
                }
            )
    if devices:
        return sorted(devices, key=lambda item: int(item.get("device_id") or 0))
    email = str(row.get("client_email") or "")
    client_uuid = str(row.get("uuid") or "")
    if not email or not client_uuid:
        return []
    return [
        {
            "device_id": 1,
            "name": "Устройство 1",
            "client_email": email,
            "uuid": client_uuid,
            "profile_type": str(row.get("profile_type") or "default"),
            "created_at": str(row.get("decided_at") or row.get("created_at") or ""),
        }
    ]


def request_device_emails(row: dict[str, Any]) -> list[str]:
    return [str(device.get("client_email") or "") for device in request_devices(row) if device.get("client_email")]


def request_device_limit(row: dict[str, Any]) -> int:
    return int(row.get("plan_devices") or plan_info(row.get("plan_id"))["devices"])


def next_device_id(row: dict[str, Any]) -> int:
    used = {int(device.get("device_id") or 0) for device in request_devices(row)}
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


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
        "Name": "VPN: TikTok через VPN, белый список напрямую",
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


def happ_routing_qr_url(config: Config) -> str:
    if not config.public_base_url:
        return ""
    return f"{config.public_base_url}/happ-routing-qr.png"


def build_qr_png(data: str) -> bytes:
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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


def admin_extend_button_rows(request_id: int) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "+7 дней", "callback_data": f"extend:7:{request_id}"},
            {"text": "+30 дней", "callback_data": f"extend:30:{request_id}"},
            {"text": "+90 дней", "callback_data": f"extend:90:{request_id}"},
        ],
        [{"text": "Продлить до даты", "callback_data": f"extend_until_help:{request_id}"}],
        [{"text": "Назад к клиенту", "callback_data": f"client:{request_id}"}],
    ]


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
        admin_web_token=_get(raw, "ADMIN_WEB_TOKEN"),
        reserve_vpn_host=_get(raw, "RESERVE_VPN_HOST"),
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
                "devices": [],
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

    def list_paid_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.list_approved_requests() if not request.get("is_free")]

    def list_free_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.list_approved_requests() if request.get("is_free")]

    def import_approved_clients(self, clients: list[dict[str, str]]) -> int:
        data = self._read()
        existing_emails = {
            email
            for item in data["requests"]
            for email in request_device_emails(item) + [str(item.get("client_email") or "")]
            if email
        }
        requests_by_id = {
            int(item["id"]): item
            for item in data["requests"]
            if str(item.get("id") or "").isdigit()
        }
        imported = 0
        now = utc_now_iso()

        for client in clients:
            email = str(client.get("email") or "")
            client_uuid = str(client.get("uuid") or "")
            match = TG_CLIENT_EMAIL_RE.match(email)
            if not match or not client_uuid or email in existing_emails:
                continue

            request_id = int(match.group("request_id"))
            device_id = int(match.group("device_id") or 1)
            existing_request = requests_by_id.get(request_id)
            if existing_request:
                devices = request_devices(existing_request)
                if not any(str(device.get("client_email") or "") == email for device in devices):
                    devices.append(
                        {
                            "device_id": device_id,
                            "name": f"Устройство {device_id}",
                            "client_email": email,
                            "uuid": client_uuid,
                            "profile_type": str(existing_request.get("profile_type") or "default"),
                            "created_at": now,
                        }
                    )
                    existing_request["devices"] = sorted(devices, key=lambda item: int(item.get("device_id") or 0))
                    if device_id == 1:
                        existing_request["client_email"] = email
                        existing_request["uuid"] = client_uuid
                    existing_emails.add(email)
                    imported += 1
                continue

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
                    "devices": [
                        {
                            "device_id": int(match.group("device_id") or 1),
                            "name": f"Устройство {int(match.group('device_id') or 1)}",
                            "client_email": email,
                            "uuid": client_uuid,
                            "profile_type": "default",
                            "created_at": now,
                        }
                    ],
                    "created_at": now,
                    "decided_at": now,
                    "subscription_status": "restored_no_deadline",
                    "subscription_until": "",
                    "restored_from_xray": True,
                }
            )
            existing_emails.add(email)
            requests_by_id[request_id] = data["requests"][-1]
            data["last_id"] = max(int(data.get("last_id", 0)), request_id)
            imported += 1

        if imported:
            self._write(data)
        return imported

    def repair_orphan_device_requests(self) -> int:
        data = self._read()
        requests = data.get("requests") or []
        by_id = {
            int(item["id"]): item
            for item in requests
            if isinstance(item, dict) and str(item.get("id") or "").isdigit()
        }
        remove_indexes: set[int] = set()
        repaired = 0
        for index, request in enumerate(requests):
            email = str(request.get("client_email") or "")
            match = TG_CLIENT_EMAIL_RE.match(email)
            if not match:
                continue
            real_request_id = int(match.group("request_id"))
            current_id = int(request.get("id") or 0)
            device_id = int(match.group("device_id") or 1)
            if real_request_id == current_id or device_id <= 1:
                continue
            parent = by_id.get(real_request_id)
            if not parent:
                continue
            devices = request_devices(parent)
            if not any(str(device.get("client_email") or "") == email for device in devices):
                devices.append(
                    {
                        "device_id": device_id,
                        "name": f"Устройство {device_id}",
                        "client_email": email,
                        "uuid": str(request.get("uuid") or ""),
                        "profile_type": str(request.get("profile_type") or parent.get("profile_type") or "default"),
                        "created_at": str(request.get("decided_at") or request.get("created_at") or utc_now_iso()),
                    }
                )
                parent["devices"] = sorted(devices, key=lambda item: int(item.get("device_id") or 0))
            remove_indexes.add(index)
            repaired += 1
        if repaired:
            data["requests"] = [item for index, item in enumerate(requests) if index not in remove_indexes]
            self._write(data)
        return repaired

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
                    request["devices"] = [
                        {
                            "device_id": 1,
                            "name": "Устройство 1",
                            "client_email": client_email,
                            "uuid": client_uuid,
                            "profile_type": profile_type,
                            "created_at": now,
                        }
                    ]
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
                devices = request_devices(request)
                if devices:
                    devices[0].update(
                        {
                            "client_email": client_email,
                            "uuid": client_uuid,
                            "profile_type": profile_type,
                            "created_at": now,
                        }
                    )
                    request["devices"] = devices
                else:
                    request["devices"] = [
                        {
                            "device_id": 1,
                            "name": "Устройство 1",
                            "client_email": client_email,
                            "uuid": client_uuid,
                            "profile_type": profile_type,
                            "created_at": now,
                        }
                    ]
                self._write(data)
                return

    def add_device(self, request_id: int, device_id: int, device_name: str, profile_type: str, client_email: str, client_uuid: str) -> dict[str, Any] | None:
        data = self._read()
        now = utc_now_iso()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            devices = request_devices(request)
            devices.append(
                {
                    "device_id": int(device_id),
                    "name": device_name,
                    "client_email": client_email,
                    "uuid": client_uuid,
                    "profile_type": profile_type,
                    "created_at": now,
                }
            )
            request["devices"] = sorted(devices, key=lambda item: int(item.get("device_id") or 0))
            request["updated_at"] = now
            self._write(data)
            return request
        return None

    def update_device(self, request_id: int, device_id: int, client_uuid: str, profile_type: str | None = None) -> dict[str, Any] | None:
        data = self._read()
        now = utc_now_iso()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            devices = request_devices(request)
            for device in devices:
                if int(device.get("device_id") or 0) != int(device_id):
                    continue
                if profile_type:
                    device["profile_type"] = profile_type
                device["uuid"] = client_uuid
                device["updated_at"] = now
                if int(device_id) == 1:
                    request["uuid"] = client_uuid
                    request["client_email"] = str(device["client_email"])
                    request["profile_type"] = str(device.get("profile_type") or request.get("profile_type") or "default")
                request["devices"] = sorted(devices, key=lambda item: int(item.get("device_id") or 0))
                request["updated_at"] = now
                self._write(data)
                return request
        return None

    def prune_devices(self, request_id: int, keep_count: int) -> list[dict[str, Any]]:
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            devices = request_devices(request)
            keep = devices[: max(1, int(keep_count))]
            removed = devices[len(keep) :]
            request["devices"] = keep
            if keep:
                request["client_email"] = str(keep[0]["client_email"])
                request["uuid"] = str(keep[0]["uuid"])
                request["profile_type"] = str(keep[0].get("profile_type") or request.get("profile_type") or "default")
            request["updated_at"] = utc_now_iso()
            self._write(data)
            return removed
        return []

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

    def set_subscription_until(self, request_id: int, until: datetime) -> dict[str, Any] | None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        data = self._read()
        for request in data["requests"]:
            if int(request["id"]) != request_id:
                continue
            request["subscription_until"] = until.isoformat(timespec="seconds")
            request["subscription_status"] = "active"
            request["subscription_set_at"] = utc_now_iso()
            if request.get("status") == "expired":
                request["status"] = "approved"
            notices = data.setdefault("subscription_notices", {})
            if isinstance(notices, dict):
                notices.pop(str(request_id), None)
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

    def find_subscription_notice_requests(self, days_before: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        notices = self._read().setdefault("subscription_notices", {})
        sent_for_day = notices if isinstance(notices, dict) else {}
        result: list[dict[str, Any]] = []
        for request in self.list_approved_requests():
            if request.get("is_free"):
                continue
            request_id = str(request.get("id") or "")
            already_sent = sent_for_day.get(request_id, []) if isinstance(sent_for_day, dict) else []
            if str(days_before) in [str(item) for item in already_sent]:
                continue
            until = parse_iso_time(str(request.get("subscription_until") or ""))
            if not until:
                continue
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            seconds_left = (until - now).total_seconds()
            if 0 < seconds_left <= days_before * 86400:
                result.append(request)
        return result

    def mark_subscription_notice(self, request_id: int, days_before: int) -> None:
        data = self._read()
        notices = data.setdefault("subscription_notices", {})
        if not isinstance(notices, dict):
            notices = {}
            data["subscription_notices"] = notices
        values = notices.setdefault(str(request_id), [])
        if str(days_before) not in [str(item) for item in values]:
            values.append(str(days_before))
        self._write(data)

    def find_inactive_requests(self, last_seen: dict[str, str], inactive_days: int = INACTIVE_CLEANUP_DAYS) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for request in self.list_approved_requests():
            if request.get("is_free"):
                continue
            seen_values = [last_seen.get(email, "") for email in request_device_emails(request)]
            seen_at = max((parse_iso_time(value) for value in seen_values if parse_iso_time(value)), default=None)
            if not seen_at:
                continue
            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=timezone.utc)
            if now - seen_at >= timedelta(days=inactive_days):
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

def admin_row_to_json(row: dict[str, Any], last_seen: dict[str, str] | None = None) -> dict[str, Any]:
    last_seen = last_seen or {}
    emails = request_device_emails(row)
    latest_seen = max((last_seen.get(email, "") for email in emails), default="")
    devices = request_devices(row)
    return {
        "id": int(row.get("id") or 0),
        "status": request_status_ru(row.get("status")),
        "raw_status": str(row.get("status") or ""),
        "chat_id": int(row.get("chat_id") or 0),
        "username": format_username(str(row.get("username") or "")),
        "full_name": str(row.get("full_name") or "-"),
        "profile_type": str(row.get("profile_type") or "default"),
        "profile_label": profile_label(str(row.get("profile_type") or "default")),
        "subscription": subscription_display(row),
        "plan_id": str(row.get("plan_id") or "1"),
        "plan": plan_label(row.get("plan_id")),
        "devices_count": len(devices),
        "devices_limit": request_device_limit(row),
        "is_free": bool(row.get("is_free")),
        "last_seen": format_age(latest_seen),
        "client_email": str(row.get("client_email") or ""),
        "uuid": str(row.get("uuid") or ""),
        "pending_plan": plan_label(row.get("pending_plan_id")) if row.get("pending_plan_id") else "",
        "devices": [
            {
                "id": int(device.get("device_id") or 1),
                "name": device_label(device),
                "email": str(device.get("client_email") or ""),
                "uuid": str(device.get("uuid") or ""),
                "profile_label": profile_label(str(device.get("profile_type") or row.get("profile_type") or "default")),
                "last_seen": format_age(last_seen.get(str(device.get("client_email") or ""), "")),
            }
            for device in devices
        ],
    }


def admin_public_config() -> dict[str, Any]:
    return {
        "plans": [{"id": plan_id, "label": plan_label(plan_id)} for plan_id in sorted(SUBSCRIPTION_PLANS, key=int)],
        "profiles": [{"id": key, "label": value["button"]} for key, value in OPERATOR_PROFILES.items()],
    }


def build_admin_html() -> str:
    public_config = json.dumps(admin_public_config(), ensure_ascii=False)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VPN Admin</title>
<style>
:root{{--bg:#07110f;--card:rgba(255,255,255,.82);--ink:#10201d;--muted:#63736e;--brand:#143c31;--blue:#246bfe;--red:#dc3f3f;--amber:#d99221}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;color:var(--ink);font-family:Trebuchet MS,Segoe UI,sans-serif;background:radial-gradient(circle at 10% 12%,rgba(60,210,135,.3),transparent 32rem),radial-gradient(circle at 90% 0,rgba(246,186,92,.25),transparent 28rem),linear-gradient(135deg,#07110f,#10231f 62%,#1c160d)}}
.wrap{{max-width:1420px;margin:0 auto;padding:26px}}.hero{{display:flex;justify-content:space-between;gap:18px;align-items:center;color:#fff;padding:26px;border-radius:30px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.16);box-shadow:0 24px 80px rgba(0,0,0,.28);backdrop-filter:blur(18px)}}
h1{{margin:0;font-size:clamp(34px,5vw,62px);letter-spacing:-2px}}.hero p{{color:rgba(255,255,255,.76)}}.pill{{padding:12px 16px;border-radius:999px;background:rgba(255,255,255,.14);color:#eaf7ef;white-space:nowrap}}
.grid{{display:grid;grid-template-columns:300px 1fr;gap:18px;margin-top:18px}}.panel{{border-radius:28px;background:rgba(240,236,218,.94);border:1px solid rgba(255,255,255,.55);box-shadow:0 18px 60px rgba(0,0,0,.18)}}.side{{padding:16px;align-self:start;position:sticky;top:16px}}.main{{padding:18px}}
button,select,input{{font:inherit}}button,.btn{{border:0;border-radius:17px;padding:12px 14px;cursor:pointer;background:rgba(255,255,255,.68);color:var(--ink)}}button:hover{{filter:brightness(1.04);transform:translateY(-1px)}}.tab{{width:100%;margin-bottom:9px;text-align:left}}.tab.on,.primary{{background:var(--brand);color:#fff}}.blue{{background:var(--blue);color:#fff}}.red{{background:var(--red);color:#fff}}.amber{{background:var(--amber);color:#fff}}
.summary{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:14px}}.metric{{padding:13px;border-radius:17px;background:rgba(255,255,255,.62)}}.metric b{{display:block;font-size:25px}}.toolbar{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}}.search{{flex:1;min-width:220px;border:1px solid rgba(16,32,29,.14);border-radius:17px;padding:12px;background:rgba(255,255,255,.72)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}}.card,.box{{padding:16px;border-radius:22px;background:var(--card);border:1px solid rgba(16,32,29,.12)}}.card h3{{margin:0 0 8px;font-size:22px}}.meta{{color:var(--muted);line-height:1.55}}.badges{{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0}}.badge{{padding:7px 10px;border-radius:999px;background:rgba(20,60,49,.1);font-size:13px}}.free{{background:rgba(22,163,107,.18)}}.warn{{background:rgba(217,146,33,.2)}}.actions{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px}}.detail{{display:grid;grid-template-columns:1.1fr .9fr;gap:14px}}.device{{padding:11px;border-radius:15px;background:rgba(20,60,49,.08);word-break:break-word;margin-bottom:8px}}.login{{min-height:100vh;display:grid;place-items:center;padding:24px}}.login-card{{max-width:460px;width:100%;padding:28px;border-radius:30px;color:#fff;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);backdrop-filter:blur(18px)}}.login-card input{{width:100%;margin:16px 0;border:0;border-radius:18px;padding:15px}}.hide{{display:none!important}}.toast{{position:fixed;right:18px;bottom:18px;padding:14px 16px;border-radius:16px;background:#143c31;color:#fff;box-shadow:0 18px 50px rgba(0,0,0,.22)}}pre{{white-space:pre-wrap;word-break:break-word}}@media(max-width:920px){{.grid,.detail{{grid-template-columns:1fr}}.hero{{display:block}}.side{{position:static}}.actions{{grid-template-columns:1fr}}}}
</style></head>
<body>
<div id="login" class="login"><div class="login-card"><h1>VPN Admin</h1><p>Введи токен из Railway переменной <b>ADMIN_WEB_TOKEN</b>.</p><input id="tokenInput" type="password" placeholder="Админ-токен"><button class="primary" onclick="saveToken()">Открыть</button></div></div>
<main id="app" class="wrap hide"><section class="hero"><div><h1>Панель VPN</h1><p>Клиенты, тарифы, устройства, продления, доступ и сервер в одном месте.</p></div><div id="serverPill" class="pill">Сервер: проверяем...</div></section><section class="grid"><aside class="panel side"><button class="tab on" data-kind="paid" onclick="setKind('paid')">Платные клиенты</button><button class="tab" data-kind="free" onclick="setKind('free')">Бесплатные клиенты</button><button class="tab" data-kind="pending" onclick="setKind('pending')">Заявки</button><button class="tab" data-kind="all" onclick="setKind('all')">Все профили</button><button class="tab" onclick="loadServer(true)">Ресурсы сервера</button><div id="summary" class="summary"></div></aside><section class="panel main"><div class="toolbar"><input id="search" class="search" placeholder="Поиск: username, имя, chat id..." oninput="renderCards()"><button class="primary" onclick="loadClients()">Обновить</button></div><div id="view"></div></section></section></main><div id="toast" class="toast hide"></div>
<script>
const APP_CONFIG={public_config};let token=localStorage.getItem('vpnAdminToken')||new URLSearchParams(location.search).get('token')||'';let kind='paid';let clients=[];let selected=null;
function saveToken(){{token=document.getElementById('tokenInput').value.trim();localStorage.setItem('vpnAdminToken',token);boot()}}
function toast(t){{let n=document.getElementById('toast');n.textContent=t;n.classList.remove('hide');setTimeout(()=>n.classList.add('hide'),2800)}}
async function api(path,opt={{}}){{let h=Object.assign({{'X-Admin-Token':token}},opt.headers||{{}});if(opt.body&&typeof opt.body!=='string'){{h['Content-Type']='application/json';opt.body=JSON.stringify(opt.body)}}let r=await fetch(path,Object.assign(opt,{{headers:h}}));let d=await r.json();if(r.status===401||r.status===403){{localStorage.removeItem('vpnAdminToken');document.getElementById('app').classList.add('hide');document.getElementById('login').classList.remove('hide')}}if(!r.ok||d.ok===false)throw new Error(d.error||'Ошибка');return d}}
function boot(){{if(!token)return;document.getElementById('login').classList.add('hide');document.getElementById('app').classList.remove('hide');loadSummary();loadClients();loadServer(false)}}
async function loadSummary(){{try{{let d=await api('/admin/api/summary'),s=d.summary;document.getElementById('summary').innerHTML=`<div class="metric"><b>${{s.paid}}</b>платных</div><div class="metric"><b>${{s.free}}</b>бесплатных</div><div class="metric"><b>${{s.pending}}</b>заявок</div><div class="metric"><b>${{s.devices}}</b>устройств</div>`}}catch(e){{toast(e.message)}}}}
function setKind(k){{kind=k;selected=null;document.querySelectorAll('.tab[data-kind]').forEach(x=>x.classList.toggle('on',x.dataset.kind===kind));loadClients()}}
async function loadClients(){{try{{let d=await api('/admin/api/clients?kind='+encodeURIComponent(kind));clients=d.clients;renderCards();loadSummary()}}catch(e){{toast(e.message)}}}}
function ok(row){{let q=document.getElementById('search').value.trim().toLowerCase();return !q||[row.id,row.chat_id,row.username,row.full_name,row.profile_label,row.plan].join(' ').toLowerCase().includes(q)}}
function renderCards(){{if(selected)return renderDetail(selected);let rows=clients.filter(ok),title={{paid:'Платные клиенты',free:'Бесплатные клиенты',pending:'Заявки',all:'Все профили'}}[kind]||'Клиенты';document.getElementById('view').innerHTML=`<h2>${{title}} · ${{rows.length}}</h2><div class="cards">${{rows.map(card).join('')||'<div class="box">Пока пусто.</div>'}}</div>`}}
function card(r){{let p=r.raw_status==='pending';return `<article class="card"><h3>#${{r.id}} · ${{r.username}}</h3><div class="meta">${{r.full_name}}<br>Chat ID: ${{r.chat_id}}<br>Активность: ${{r.last_seen}}</div><div class="badges"><span class="badge">${{r.status}}</span><span class="badge ${{r.is_free?'free':''}}">${{r.is_free?'бесплатный':r.plan}}</span><span class="badge">${{r.devices_count}}/${{r.devices_limit}} устр.</span><span class="badge">${{r.profile_label}}</span>${{r.pending_plan?`<span class="badge warn">смена: ${{r.pending_plan}}</span>`:''}}</div><div class="actions"><button class="primary" onclick="openClient(${{r.id}})">Открыть</button>${{p?`<button class="blue" onclick="action('approve',${{r.id}})">Одобрить</button><button class="red" onclick="action('reject',${{r.id}})">Отклонить</button>`:`<button onclick="quickStats(${{r.id}})">Статистика</button>`}}</div></article>`}}
async function openClient(id){{try{{let d=await api('/admin/api/client?id='+id);selected=d.client;renderDetail(selected)}}catch(e){{toast(e.message)}}}}
function renderDetail(r){{let plans=APP_CONFIG.plans.map(p=>`<option value="${{p.id}}" ${{p.id===r.plan_id?'selected':''}}>${{p.label}}</option>`).join(''),profiles=APP_CONFIG.profiles.map(p=>`<option value="${{p.id}}" ${{p.id===r.profile_type?'selected':''}}>${{p.label}}</option>`).join('');document.getElementById('view').innerHTML=`<button onclick="selected=null;renderCards()">← Назад</button><div class="detail" style="margin-top:14px"><section class="box"><h2>Клиент #${{r.id}} · ${{r.username}}</h2><p class="meta">${{r.full_name}}<br>Chat ID: ${{r.chat_id}}<br>Статус: ${{r.status}}<br>Подписка: ${{r.subscription}}<br>Тариф: ${{r.plan}}<br>Оператор: ${{r.profile_label}}<br>Активность: ${{r.last_seen}}</p><h3>Устройства</h3>${{r.devices.map(d=>`<div class="device"><b>${{d.name}}</b><br>${{d.profile_label}}<br>Email: ${{d.email}}<br>Активность: ${{d.last_seen}}</div>`).join('')||'Нет устройств'}}</section><section class="box"><h3>Действия</h3><div class="actions"><button onclick="action('extend',${{r.id}},{{days:7}})">+7 дней</button><button onclick="action('extend',${{r.id}},{{days:30}})">+30 дней</button><button onclick="action('extend',${{r.id}},{{days:90}})">+90 дней</button><button class="red" onclick="confirmAction('disable',${{r.id}},'Отключить все ссылки клиента?')">Отключить</button></div><div class="box" style="margin-top:12px"><label>Тариф</label><select id="planSelect">${{plans}}</select><button class="primary" style="margin-top:8px;width:100%" onclick="action('set_plan',${{r.id}},{{plan_id:document.getElementById('planSelect').value}})">Сменить тариф</button></div><div class="box" style="margin-top:12px"><label>Оператор / перевыпуск основной ссылки</label><select id="profileSelect">${{profiles}}</select><button class="amber" style="margin-top:8px;width:100%" onclick="confirmAction('reissue',${{r.id}},'Перевыпустить основную ссылку?',{{profile_type:document.getElementById('profileSelect').value}})">Перевыпустить</button></div><div class="actions"><button class="blue" onclick="action('set_free',${{r.id}},{{is_free:${{r.is_free?'false':'true'}}}})">${{r.is_free?'Убрать бесплатный':'Сделать бесплатным'}}</button><button onclick="quickStats(${{r.id}})">Статистика</button><button onclick="action('approve_plan',${{r.id}})">Одобрить смену тарифа</button><button onclick="action('reject_plan',${{r.id}})">Отклонить смену тарифа</button></div><pre id="statsBox"></pre></section></div>`}}
function confirmAction(n,id,t,e={{}}){{if(confirm(t))action(n,id,e)}}async function action(n,id,e={{}}){{try{{let d=await api('/admin/api/action',{{method:'POST',body:Object.assign({{action:n,id}},e)}});toast(d.message||'Готово');selected=null;await loadClients()}}catch(x){{toast(x.message)}}}}
async function quickStats(id){{try{{let d=await api('/admin/api/client_stats?id='+id);if(selected)document.getElementById('statsBox').textContent=d.text;else alert(d.text)}}catch(e){{toast(e.message)}}}}
async function loadServer(show){{try{{let d=await api('/admin/api/server');document.getElementById('serverPill').textContent=`Xray: ${{d.status.xray||'-'}} · CPU ${{d.status.cpu||'-'}}% · RAM ${{d.status.memory||'-'}}`;if(show){{selected=null;document.getElementById('view').innerHTML=`<h2>Ресурсы сервера</h2><div class="box"><pre>${{d.text}}</pre></div>`}}}}catch(e){{toast(e.message)}}}}
boot();
</script></body></html>"""


def start_routing_web_server(config: Config, store: Store | None = None, manager: Any = None, bot: TelegramBot | None = None) -> None:
    if config.web_port <= 0:
        return

    class RoutingHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logging.info("HTTP %s", format % args)

        def _send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
            payload = body.encode("utf-8")
            self._send_bytes(status, payload, content_type)

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            self._send_text(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

        def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _admin_token(self) -> str:
            header = self.headers.get("X-Admin-Token", "")
            if header:
                return header.strip()
            query = parse_qs(urlparse(self.path).query)
            return str((query.get("token") or [""])[0]).strip()

        def _require_admin(self) -> bool:
            if not config.admin_web_token:
                self._send_json(403, {"ok": False, "error": "ADMIN_WEB_TOKEN не настроен в Railway."})
                return False
            if not compare_digest(self._admin_token(), config.admin_web_token):
                self._send_json(401, {"ok": False, "error": "Неверный админ-токен."})
                return False
            if not store or not manager:
                self._send_json(503, {"ok": False, "error": "Админка ещё запускается."})
                return False
            return True

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/", "/healthz"}:
                self._send_text(200, "ok\n")
                return
            if path == "/admin":
                self._send_text(200, build_admin_html(), "text/html; charset=utf-8")
                return
            if path.startswith("/admin/api/"):
                self._handle_admin_get(path, parse_qs(parsed.query))
                return
            if path == "/happ-routing-qr.png":
                try:
                    qr_target = happ_routing_redirect_url(config) or build_happ_routing_link()
                    self._send_bytes(200, build_qr_png(qr_target), "image/png")
                except Exception:
                    logging.exception("Could not build Happ routing QR")
                    self._send_text(500, "qr error\n")
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

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/admin/api/"):
                self._handle_admin_post(parsed.path)
                return
            self._send_json(404, {"ok": False, "error": "Не найдено."})

        def _rows_with_activity(self, rows: list[dict[str, Any]]) -> dict[str, str]:
            emails = [email for row in rows for email in request_device_emails(row)]
            try:
                return manager.get_last_seen_by_email(emails) if emails else {}
            except Exception:
                logging.exception("Admin web: could not load activity")
                return {}

        def _handle_admin_get(self, path: str, query: dict[str, list[str]]) -> None:
            if not self._require_admin():
                return
            assert store is not None
            try:
                if path == "/admin/api/summary":
                    approved = store.list_approved_requests()
                    paid = store.list_paid_requests()
                    free = store.list_free_requests()
                    pending = [row for row in store.export_data().get("requests", []) if row.get("status") == "pending"]
                    self._send_json(200, {"ok": True, "summary": {
                        "paid": len(paid),
                        "free": len(free),
                        "pending": len(pending),
                        "all": len(approved),
                        "devices": sum(len(request_devices(row)) for row in approved),
                    }})
                    return
                if path == "/admin/api/clients":
                    kind = str((query.get("kind") or ["paid"])[0])
                    if kind == "free":
                        rows = store.list_free_requests()
                    elif kind == "pending":
                        rows = [row for row in store.export_data().get("requests", []) if row.get("status") == "pending"]
                    elif kind == "all":
                        rows = store.export_data().get("requests", [])
                    else:
                        rows = store.list_paid_requests()
                    rows = sorted(rows, key=lambda item: int(item.get("id") or 0), reverse=True)
                    last_seen = self._rows_with_activity(rows)
                    self._send_json(200, {"ok": True, "clients": [admin_row_to_json(row, last_seen) for row in rows]})
                    return
                if path == "/admin/api/client":
                    request_id = int((query.get("id") or ["0"])[0])
                    row = store.get_request(request_id)
                    if not row:
                        self._send_json(404, {"ok": False, "error": "Клиент не найден."})
                        return
                    last_seen = self._rows_with_activity([row])
                    self._send_json(200, {"ok": True, "client": admin_row_to_json(row, last_seen)})
                    return
                if path == "/admin/api/server":
                    status = manager.check_server_status()
                    self._send_json(200, {"ok": True, "status": status, "text": format_vpn_status(status, config)})
                    return
                if path == "/admin/api/client_stats":
                    request_id = int((query.get("id") or ["0"])[0])
                    row = store.get_request(request_id)
                    if not row or not row.get("client_email"):
                        self._send_json(404, {"ok": False, "error": "Клиент не найден."})
                        return
                    all_emails = [email for item in store.list_approved_requests() for email in request_device_emails(item)]
                    stats = manager.get_usage_stats(str(row["client_email"]), all_emails)
                    self._send_json(200, {"ok": True, "stats": stats, "text": format_usage_stats(row, stats)})
                    return
                self._send_json(404, {"ok": False, "error": "Не найдено."})
            except Exception as exc:
                logging.exception("Admin web GET failed")
                self._send_json(500, {"ok": False, "error": str(exc)})

        def _handle_admin_post(self, path: str) -> None:
            if not self._require_admin():
                return
            assert store is not None
            if path != "/admin/api/action":
                self._send_json(404, {"ok": False, "error": "Не найдено."})
                return
            try:
                payload = self._read_json()
                action = str(payload.get("action") or "")
                request_id = int(payload.get("id") or 0)
                row = store.get_request(request_id)
                if not row:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден."})
                    return
                if action == "approve":
                    if row.get("status") != "pending":
                        self._send_json(400, {"ok": False, "error": "Заявка уже обработана."})
                        return
                    profile_type = str(row.get("profile_type") or "default")
                    if not is_profile_type(profile_type):
                        profile_type = "default"
                    client_uuid = str(uuid.uuid4())
                    client_email = f"tg-{row['chat_id']}-{request_id}"
                    manager.save_client(client_email, client_uuid)
                    store.finish_request(request_id, "approved", profile_type, client_email, client_uuid, config.default_subscription_days)
                    link = build_vless_link(config, client_uuid, profile_type, f"VPN {request_id} {profile_short(profile_type)}")
                    if bot:
                        bot.send_message(int(row["chat_id"]), f"Заявка одобрена.\n\nТвоя VPN-ссылка:\n\n{link}")
                    self._send_json(200, {"ok": True, "message": f"Заявка #{request_id} одобрена."})
                    return
                if action == "reject":
                    if row.get("status") != "pending":
                        self._send_json(400, {"ok": False, "error": "Заявка уже обработана."})
                        return
                    store.finish_request(request_id, "rejected", "", "", "")
                    if bot:
                        bot.send_message(int(row["chat_id"]), f"Заявка #{request_id} отклонена.")
                    self._send_json(200, {"ok": True, "message": f"Заявка #{request_id} отклонена."})
                    return
                if action == "extend":
                    days = int(payload.get("days") or 0)
                    if days not in {7, 30, 90}:
                        self._send_json(400, {"ok": False, "error": "Неверный срок продления."})
                        return
                    if row.get("status") == "expired":
                        for device in request_devices(row):
                            manager.save_client(str(device["client_email"]), str(device["uuid"]))
                    updated = store.extend_subscription(request_id, days)
                    text = f"Подписка профиля #{request_id} продлена на {days} дней: {format_subscription(str(updated.get('subscription_until') if updated else ''))}."
                    if updated and bot:
                        bot.send_message(int(updated["chat_id"]), text)
                    self._send_json(200, {"ok": True, "message": text})
                    return
                if action == "set_plan":
                    plan_id = str(payload.get("plan_id") or "")
                    updated = store.set_plan(request_id, plan_id)
                    if not updated:
                        self._send_json(400, {"ok": False, "error": "Не смог сменить тариф."})
                        return
                    removed = enforce_device_limit(manager, store, updated)
                    text = f"Тариф профиля #{request_id} изменён на {plan_label(plan_id)}."
                    if removed:
                        text += f" Отключено лишних устройств: {len(removed)}."
                    self._send_json(200, {"ok": True, "message": text})
                    return
                if action == "approve_plan":
                    updated = store.approve_plan_change(request_id)
                    if not updated:
                        self._send_json(400, {"ok": False, "error": "Нет заявки на смену тарифа."})
                        return
                    removed = enforce_device_limit(manager, store, updated)
                    text = f"Смена тарифа профиля #{request_id} одобрена."
                    if removed:
                        text += f" Отключено лишних устройств: {len(removed)}."
                    self._send_json(200, {"ok": True, "message": text})
                    return
                if action == "reject_plan":
                    if not store.reject_plan_change(request_id):
                        self._send_json(400, {"ok": False, "error": "Профиль не найден."})
                        return
                    self._send_json(200, {"ok": True, "message": f"Смена тарифа профиля #{request_id} отклонена."})
                    return
                if action == "set_free":
                    updated = store.set_free_access(request_id, bool(payload.get("is_free")))
                    if not updated:
                        self._send_json(404, {"ok": False, "error": "Профиль не найден."})
                        return
                    self._send_json(200, {"ok": True, "message": f"Бесплатный доступ профиля #{request_id}: {'включён' if updated.get('is_free') else 'выключен'}."})
                    return
                if action == "disable":
                    if row.get("status") != "approved":
                        self._send_json(400, {"ok": False, "error": "Отключать можно только активный профиль."})
                        return
                    for client_email in request_device_emails(row):
                        manager.remove_client(client_email)
                    store.disable_request(request_id)
                    if bot:
                        bot.send_message(int(row["chat_id"]), "Твой VPN-профиль отключён админом. Старые ссылки больше не работают.")
                    self._send_json(200, {"ok": True, "message": f"Профиль #{request_id} отключён."})
                    return
                if action == "reissue":
                    if row.get("status") != "approved":
                        self._send_json(400, {"ok": False, "error": "Перевыпуск доступен только активному профилю."})
                        return
                    profile_type = str(payload.get("profile_type") or row.get("profile_type") or "default")
                    if not is_profile_type(profile_type):
                        profile_type = "default"
                    client_email = str(row.get("client_email") or f"tg-{row['chat_id']}-{request_id}")
                    client_uuid = str(uuid.uuid4())
                    link = build_vless_link(config, client_uuid, profile_type, f"VPN {request_id} {profile_short(profile_type)}")
                    manager.save_client(client_email, client_uuid)
                    manager.reset_profile_guard_binding(client_email)
                    store.update_profile(request_id, profile_type, client_email, client_uuid)
                    if bot:
                        bot.send_message(int(row["chat_id"]), f"Новая VPN-ссылка создана. Старая больше не работает.\n\n{link}")
                    self._send_json(200, {"ok": True, "message": f"Профиль #{request_id} перевыпущен как {profile_label(profile_type)}."})
                    return
                self._send_json(400, {"ok": False, "error": "Неизвестное действие."})
            except Exception as exc:
                logging.exception("Admin web action failed")
                self._send_json(500, {"ok": False, "error": str(exc)})

    server = ThreadingHTTPServer(("0.0.0.0", config.web_port), RoutingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Routing/admin web server started on port %s", config.web_port)


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
            command = f"journalctl -u xray -n {LAST_SEEN_JOURNAL_LINES} -o short-iso --no-pager || true"
            rc, out, err = self._run(client, command)
            if rc != 0:
                raise RuntimeError((err or "Could not read Xray journal").strip()[:500])
        finally:
            client.close()

        last_seen: dict[str, str] = {}
        email_set = set(emails)
        for line in out.splitlines():
            match = XRAY_USAGE_RE.search(line)
            if not match:
                continue
            email = match.group("email")
            if email not in email_set:
                continue
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

    def get_usage_stats(
        self,
        client_email: str,
        all_client_emails: list[str] | None = None,
        lines: int = USAGE_STATS_JOURNAL_LINES,
    ) -> dict[str, Any]:
        client = self._connect()
        try:
            command = f"journalctl -u xray -n {int(lines)} -o short-iso --no-pager || true"
            rc, out, err = self._run(client, command)
            if rc != 0:
                raise RuntimeError((err or "Could not read Xray journal").strip()[:500])
        finally:
            client.close()

        tracked_emails = set(all_client_emails or [])
        hits = 0
        total_tracked_hits = 0
        active_tracked_emails: set[str] = set()
        ips: set[str] = set()
        first_seen = ""
        last_seen = ""
        for line in out.splitlines():
            match = XRAY_USAGE_RE.search(line)
            if not match:
                continue
            email = match.group("email")
            if tracked_emails and email in tracked_emails:
                total_tracked_hits += 1
                active_tracked_emails.add(email)
            if email != client_email:
                continue
            hits += 1
            parts = line.split(maxsplit=1)
            if parts:
                first_seen = first_seen or parts[0]
                last_seen = parts[0]
            ips.add(match.group("ip"))
        if not tracked_emails:
            total_tracked_hits = hits
            active_tracked_emails = {client_email} if hits else set()
        share = round((hits / total_tracked_hits) * 100, 1) if total_tracked_hits else 0
        return {
            "hits": hits,
            "total_tracked_hits": total_tracked_hits,
            "active_tracked_profiles": len(active_tracked_emails),
            "share_percent": share,
            "unique_ips": len(ips),
            "ips": sorted(ips),
            "first_seen": first_seen,
            "last_seen": last_seen,
        }

    def ensure_profile_guard_enabled(self) -> dict[str, str]:
        client = self._connect()
        try:
            command = r"""
systemctl unmask xray-profile-guard >/dev/null 2>&1 || true
cat >/etc/systemd/system/xray-profile-guard.service <<'EOF'
[Unit]
Description=Xray profile one-device guard
After=xray.service
Requires=xray.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/xray-profile-guard.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now xray-profile-guard
systemctl is-active xray-profile-guard
"""
            rc, out, err = self._run(client, command)
            if rc != 0:
                raise RuntimeError(f"Profile guard enable failed: {out}{err}")
            return {"guard": out.strip() or "unknown"}
        finally:
            client.close()

    def check_server_status(self) -> dict[str, Any]:
        client = self._connect()
        try:
            commands = {
                "xray": "systemctl is-active xray || true",
                "guard": "systemctl is-active xray-profile-guard 2>/dev/null || true",
                "guard_enabled": "systemctl is-enabled xray-profile-guard 2>/dev/null || true",
                "ports": "ss -lnt '( sport = :443 or sport = :8443 or sport = :2053 )' | tail -n +2 || true",
                "uptime": "uptime -p || true",
                "load": "cat /proc/loadavg || true",
                "cpu": "if command -v vmstat >/dev/null 2>&1; then vmstat 1 2 | tail -1 | awk '{print 100-$15}'; else awk '/^cpu /{u=$2+$3+$4+$7+$8+$9; t=$2+$3+$4+$5+$6+$7+$8+$9; if(t>0) printf \"%.0f\", u*100/t}' /proc/stat; fi || true",
                "memory": "free -m | awk 'NR==2{print $3 \" \" $2 \" \" int($3*100/$2)}' || true",
                "disk": "df -h / | awk 'NR==2{print $3 \" \" $2 \" \" $5}' || true",
                "connections": "ss -Hnt state established '( sport = :443 or sport = :8443 or sport = :2053 )' | wc -l || true",
            }
            result: dict[str, Any] = {}
            for key, command in commands.items():
                _, out, err = self._run(client, command)
                result[key] = (out or err).strip()
            return result
        finally:
            client.close()


def build_vless_link(config: Config, client_uuid: str, profile_type: str, label: str, host: str | None = None) -> str:
    port = profile_port(config, profile_type)
    fragment = quote(label)
    vpn_host = host or config.vpn_host
    return (
        f"vless://{client_uuid}@{vpn_host}:{port}"
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
                [{"text": "Проверить VPN"}, {"text": "Ресурсы сервера"}],
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
                [{"text": "Мои устройства", "callback_data": "show_devices"}],
                [{"text": "Добавить устройство", "callback_data": "add_device"}],
                [{"text": "Перевыпустить основную ссылку", "callback_data": f"show_reissue:{request_id}"}],
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


def user_devices_markup(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    rows = [
        [{"text": f"Перевыпустить ссылку: {device_label(device)}", "callback_data": f"reissue_device:{request_id}:{int(device.get('device_id') or 1)}"}]
        for device in request_devices(row)
    ]
    rows.append([{"text": "Добавить устройство", "callback_data": "add_device"}])
    rows.append([{"text": "Назад к подписке", "callback_data": "show_subscription"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


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
    rows = [
        [
            {"text": "Устройства", "callback_data": f"client_devices:{request_id}"},
        ],
        [
            {"text": "Продление", "callback_data": f"client_extend:{request_id}"},
            {"text": "Тариф", "callback_data": f"client_plan:{request_id}"},
        ],
        [
            {"text": "Оператор", "callback_data": f"client_operator:{request_id}"},
            {"text": "Статистика", "callback_data": f"client_stats:{request_id}"},
        ],
        [
            {"text": "Резервная ссылка", "callback_data": f"reserve_link:{request_id}"},
            {"text": "Доступ", "callback_data": f"client_access:{request_id}"},
        ],
    ]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def admin_client_extend_markup(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    rows = [] if row.get("is_free") else admin_extend_button_rows(request_id)
    if row.get("is_free"):
        rows.append([{"text": "Бесплатный клиент: продление не нужно", "callback_data": f"client:{request_id}"}])
        rows.append([{"text": "Назад к клиенту", "callback_data": f"client:{request_id}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def admin_client_plan_markup(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    rows = admin_plan_button_rows(request_id)
    rows.append([{"text": "Назад к клиенту", "callback_data": f"client:{request_id}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def admin_client_operator_markup(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    rows = profile_button_rows("reissue", request_id)
    rows.append([{"text": "Назад к клиенту", "callback_data": f"client:{request_id}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def admin_client_access_markup(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    rows = []
    if row.get("is_free"):
        rows.append([{"text": "Убрать бесплатный доступ", "callback_data": f"free:off:{request_id}"}])
    else:
        rows.append([{"text": "Сделать бесплатным", "callback_data": f"free:on:{request_id}"}])
    rows.append([{"text": "Отключить пользователя", "callback_data": f"disable:{request_id}"}])
    rows.append([{"text": "Назад к клиенту", "callback_data": f"client:{request_id}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


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


def request_status_ru(value: Any) -> str:
    status = str(value or "").strip().lower()
    mapping = {
        "pending": "ожидает одобрения",
        "approved": "одобрен",
        "rejected": "отклонён",
        "expired": "истёк",
        "active": "активна",
        "restored_no_deadline": "восстановлен без срока",
        "waiting_manual_payment": "ожидает ручной оплаты",
    }
    return mapping.get(status, status or "-")


def format_client_list(rows: list[dict[str, Any]], last_seen: dict[str, str]) -> str:
    if not rows:
        return "Платных клиентов пока нет. Бесплатные клиенты вынесены в отдельный раздел."

    lines = [f"Платные клиенты VPN: {len(rows)}"]
    for number, row in enumerate(rows, start=1):
        profile_type = profile_label(str(row.get("profile_type") or "default"))
        emails = request_device_emails(row)
        latest_seen = max((last_seen.get(email, "") for email in emails), default="")
        username = format_username(str(row.get("username") or ""))
        subscription = subscription_display(row)
        plan = plan_label(row.get("plan_id"))
        devices_count = len(request_devices(row))
        devices_limit = request_device_limit(row)
        lines.append(
            f"{number}. {username} | {profile_type} | устройств: {devices_count}/{devices_limit} | тариф: {plan} | подписка: {subscription} | активность: {format_age(latest_seen)}"
        )
    return "\n".join(lines)


def format_client_card(row: dict[str, Any], last_seen: dict[str, str], number: int) -> str:
    profile_type = profile_label(str(row.get("profile_type") or "default"))
    email = str(row.get("client_email") or "")
    emails = request_device_emails(row)
    latest_seen = max((last_seen.get(item, "") for item in emails), default=last_seen.get(email, ""))
    username = format_username(str(row.get("username") or ""))
    full_name = str(row.get("full_name") or "-")
    chat_id = str(row.get("chat_id") or "-")
    status = request_status_ru(row.get("status"))
    subscription_status = request_status_ru(row.get("subscription_status") or "active")
    subscription_text = subscription_display(row)
    free_text = "да" if row.get("is_free") else "нет"
    plan = plan_label(row.get("plan_id"))
    devices_count = len(request_devices(row))
    devices_limit = request_device_limit(row)
    return (
        f"Клиент #{number} | профиль #{row.get('id')}\n"
        f"{username} | {full_name}\n\n"
        f"Статус: {status}\n"
        f"Подписка: {subscription_status}, {subscription_text}\n"
        f"Тариф: {plan}\n"
        f"Устройства: {devices_count}/{devices_limit}\n"
        f"Оператор: {profile_type}\n"
        f"Бесплатный: {free_text}\n"
        f"Активность: {format_age(latest_seen)}\n"
        f"Chat ID: {chat_id}\n"
        f"Профиль в Xray: {email or '-'}"
    )


def format_client_details(row: dict[str, Any], last_seen: dict[str, str]) -> str:
    email = str(row.get("client_email") or "")
    created_at = str(row.get("created_at") or "-")
    decided_at = str(row.get("decided_at") or "-")
    restored = "да" if row.get("restored_from_xray") else "нет"
    return (
        format_client_card(row, last_seen, int(row.get("id") or 0))
        + "\n\n"
        f"UUID: {row.get('uuid') or '-'}\n"
        f"Создан: {created_at} ({format_age(created_at)})\n"
        f"Одобрен/обновлён: {decided_at} ({format_age(decided_at)})\n"
        f"Восстановлен из Xray: {restored}"
    )


def format_devices_text(row: dict[str, Any], config: Config, include_links: bool = True) -> str:
    devices = request_devices(row)
    limit = request_device_limit(row)
    lines = [
        f"Устройства профиля #{row.get('id')}: {len(devices)}/{limit}",
        "",
        "Каждое устройство получает отдельную VPN-ссылку. Так лимит считается по устройствам, а не по Wi-Fi.",
        "Чтобы заменить одну ссылку, нажми кнопку перевыпуска нужного устройства под этим сообщением.",
    ]
    if not devices:
        lines.append("")
        lines.append("Устройств пока нет.")
        return "\n".join(lines)
    for device in devices:
        device_id = int(device.get("device_id") or 1)
        profile_type = str(device.get("profile_type") or row.get("profile_type") or "default")
        label = f"VPN {row.get('id')} D{device_id} {profile_short(profile_type)}"
        lines.append("")
        lines.append(f"{device_id}. {device_label(device)}")
        lines.append(f"Оператор: {profile_label(profile_type)}")
        lines.append(f"Создано: {format_age(str(device.get('created_at') or ''))}")
        if include_links:
            lines.append(build_vless_link(config, str(device["uuid"]), profile_type, label))
    return "\n".join(lines)


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


def enforce_device_limit(manager: XrayManager, store: Store, row: dict[str, Any]) -> list[dict[str, Any]]:
    limit = request_device_limit(row)
    devices = request_devices(row)
    if len(devices) <= limit:
        return []
    removed = devices[limit:]
    for device in removed:
        manager.remove_client(str(device["client_email"]))
    return store.prune_devices(int(row["id"]), limit)


def send_client_card(bot: TelegramBot, manager: XrayManager, chat_id: int, row: dict[str, Any], markup: str | None = None) -> None:
    emails = request_device_emails(row)
    try:
        last_seen = manager.get_last_seen_by_email(emails) if emails else {}
    except Exception:
        logging.exception("Could not load single client activity")
        last_seen = {}
    bot.send_message(chat_id, format_client_card(row, last_seen, int(row.get("id") or 0)), markup or admin_client_markup(row))


def format_usage_stats(row: dict[str, Any], stats: dict[str, Any]) -> str:
    ips = ", ".join(stats.get("ips") or []) or "-"
    share = stats.get("share_percent", 0)
    hits = int(stats.get("hits") or 0)
    if hits >= 500 or float(share or 0) >= 50:
        load_level = "высокая"
    elif hits >= 100 or float(share or 0) >= 20:
        load_level = "средняя"
    elif hits > 0:
        load_level = "низкая"
    else:
        load_level = "нет активности в последних логах"
    return (
        f"Статистика профиля #{row.get('id')}\n\n"
        "Клиент:\n"
        f"- пользователь: {format_username(str(row.get('username') or ''))}\n"
        f"- имя: {row.get('full_name') or '-'}\n"
        f"- тариф: {plan_label(row.get('plan_id'))}\n\n"
        "Нагрузка профиля:\n"
        f"- уровень: {load_level}\n"
        f"- подключений в последних логах: {hits}\n"
        f"- доля от общей VPN-активности: {share}%\n"
        f"- активных профилей в выборке: {stats.get('active_tracked_profiles', 0)}\n\n"
        "Подозрение на передачу ссылки:\n"
        f"- уникальных IP: {stats.get('unique_ips', 0)}\n"
        f"- IP: {ips}\n\n"
        "Время:\n"
        f"- первый лог: {format_age(str(stats.get('first_seen') or ''))}\n"
        f"- последний лог: {format_age(str(stats.get('last_seen') or ''))}\n\n"
        "Важно: точный CPU/RAM по одному человеку Xray напрямую не отдаёт. Здесь показана реальная активность профиля по логам сервера."
    )


def service_status_ru(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status == "active":
        return "работает"
    if status == "inactive":
        return "выключено"
    if status == "failed":
        return "ошибка"
    if status == "activating":
        return "запускается"
    if status == "deactivating":
        return "останавливается"
    return status or "-"


def uptime_ru(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("up "):
        text = text[3:]
    replacements = {
        "weeks": "нед.",
        "week": "нед.",
        "days": "дн.",
        "day": "дн.",
        "hours": "ч.",
        "hour": "ч.",
        "minutes": "мин.",
        "minute": "мин.",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text or "-"


def memory_ru(value: Any) -> str:
    parts = str(value or "").split()
    if len(parts) >= 3:
        return f"{parts[0]} из {parts[1]} МБ ({parts[2]}%)"
    return str(value or "-")


def disk_ru(value: Any) -> str:
    parts = str(value or "").split()
    if len(parts) >= 3:
        return f"{parts[0]} из {parts[1]} ({parts[2]})"
    return str(value or "-")


def load_ru(value: Any) -> str:
    parts = str(value or "").split()
    if len(parts) >= 3:
        return f"{parts[0]} / {parts[1]} / {parts[2]}"
    return str(value or "-")


def ports_ru(value: Any) -> str:
    ports = []
    for line in str(value or "").splitlines():
        match = re.search(r":(443|8443|2053)\b", line)
        if match:
            port = match.group(1)
            if port not in ports:
                ports.append(port)
    if not ports:
        return "- открытых VPN-портов не найдено"
    return "\n".join(f"- порт {port}: открыт" for port in sorted(ports, key=int))


def format_vpn_status(status: dict[str, Any], config: Config) -> str:
    reserve = config.reserve_vpn_host or "не настроен"
    connections = str(status.get("connections") or "0").strip()
    return (
        "Состояние VPN-сервера\n\n"
        "Службы:\n"
        f"- Xray: {service_status_ru(status.get('xray'))}\n"
        f"- защита профилей: {service_status_ru(status.get('guard'))} ({status.get('guard_enabled') or 'неизвестно'})\n"
        f"- время работы: {uptime_ru(status.get('uptime'))}\n"
        f"- резервный хост: {reserve}\n\n"
        "Ресурсы сервера:\n"
        f"- CPU сейчас: {status.get('cpu') or '-'}%\n"
        f"- нагрузка 1/5/15 мин: {load_ru(status.get('load'))}\n"
        f"- память: {memory_ru(status.get('memory'))}\n"
        f"- диск: {disk_ru(status.get('disk'))}\n"
        f"- активные TCP-соединения VPN: {connections}\n"
        "- это не количество людей: один телефон или ноутбук может держать много соединений одновременно\n\n"
        f"Порты:\n{ports_ru(status.get('ports'))}"
    )


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
        "Белый список будет идти напрямую, мимо VPN: российские сайты, банки, Яндекс, VK, Госуслуги, операторы, магазины, карты, Apple/Google-проверки сети и капчи.\n\n"
        "Это снижает тормоза, капчи и ошибки входа в банки. Если сайт не открывается, обнови маршрутизацию этой кнопкой ещё раз.\n\n"
        "Отсканируй QR через Happ или нажми кнопку ниже, чтобы открыть Happ."
    )
    qr_source = happ_routing_qr_url(config) or qr_url_for_link(routing_link)
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
            "Как подключить:\n"
            "1. Нажми «Получить VPN».\n"
            "2. Выбери тариф и оператора. Если не знаешь оператора, выбирай «Обычный оператор».\n"
            "3. После одобрения бот пришлёт VPN-ссылку.\n"
            "4. Открой ссылку в Happ и нажми подключить.\n\n"
            "Устройства:\n"
            "1. Одна VPN-ссылка = одно устройство.\n"
            "2. Для телефона, ноутбука и планшета нужны отдельные ссылки.\n"
            "3. Добавить ссылку можно в «Моя подписка» -> «Добавить устройство».\n\n"
            "Маршрутизация:\n"
            "1. Нажми «Моя подписка».\n"
            "2. Нажми кнопку маршрутизации Happ.\n"
            "3. Добавь правила в Happ. TikTok пойдёт через VPN, а Яндекс, банки, VK и Госуслуги будут идти напрямую.\n\n"
            "Команды:\n"
            "/vpn - выбрать оператора и отправить заявку на VPN\n"
            "/vpn_status ID - проверить заявку\n"
            "/reissue - перевыпуск ссылки\n"
            "/change_plan - сменить тариф\n"
            "/routing - правила TikTok/Яндекс/банки\n"
            "/subscription - срок подписки\n"
            "/check_vpn - проверить VPN, только для админа",
            reply_markup,
        )
        return

    if text.startswith("/routing") or text.lower() == "маршрутизация":
        send_routing_instructions(bot, config, chat_id)
        return

    if text.startswith("/check_vpn") or text.lower() in {"проверить vpn", "ресурсы сервера", "состояние сервера"}:
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        try:
            bot.send_message(chat_id, format_vpn_status(manager.check_server_status(), config), admin_reply_markup())
        except Exception as exc:
            logging.exception("VPN status check failed")
            bot.send_message(chat_id, f"Не смог проверить VPN: {exc}", admin_reply_markup())
        return

    if text.startswith("/extend_until"):
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit():
            bot.send_message(chat_id, "Формат: /extend_until ID YYYY-MM-DD")
            return
        request_id = int(parts[1])
        try:
            until = datetime.strptime(parts[2], "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            bot.send_message(chat_id, "Дата должна быть в формате YYYY-MM-DD, например 2026-05-30.")
            return
        row = store.get_request(request_id)
        if not row:
            bot.send_message(chat_id, "Профиль не найден.")
            return
        if row.get("status") == "expired" and row.get("client_email") and row.get("uuid"):
            try:
                for device in request_devices(row):
                    manager.save_client(str(device["client_email"]), str(device["uuid"]))
            except Exception as exc:
                logging.exception("Failed to restore expired VPN profile for exact-date extension")
                bot.send_message(chat_id, f"Не смог включить просроченный профиль #{request_id}: {exc}")
                return
        updated = store.set_subscription_until(request_id, until)
        if not updated:
            bot.send_message(chat_id, "Не смог изменить дату подписки.")
            return
        text_out = f"Подписка профиля #{request_id} установлена до {parts[2]}."
        bot.send_message(chat_id, text_out, admin_reply_markup())
        bot.send_message(int(updated["chat_id"]), f"Твоя VPN-подписка продлена до {parts[2]}.")
        return

    if text.startswith("/clients") or text.lower() == "список клиентов":
        if chat_id not in config.admin_chat_ids:
            bot.send_message(chat_id, "Эта команда доступна только админу.")
            return
        clear_previous_admin_list_messages(bot, store, chat_id)
        rows = store.list_paid_requests()
        if not rows:
            try:
                imported = 0
                if not store.list_approved_requests():
                    imported = store.import_approved_clients(manager.list_bot_clients())
                if imported:
                    rows = store.list_paid_requests()
                    bot.send_message(chat_id, f"Восстановил клиентов из Xray: {imported}.")
            except Exception:
                logging.exception("Failed to restore clients from Xray")
        rows = refresh_missing_user_info(bot, store, rows)
        try:
            last_seen = manager.get_last_seen_by_email([email for row in rows for email in request_device_emails(row)])
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
            last_seen = manager.get_last_seen_by_email([email for row in rows for email in request_device_emails(row)])
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
            f"Устройства: {len(request_devices(existing))}/{request_device_limit(existing)}.\n"
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
                f"Устройства: {len(request_devices(existing))}/{request_device_limit(existing)}.\n"
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
                    f"Устройства: {len(request_devices(existing))}/{request_device_limit(existing)}.\n"
                    "Чтобы добавить ещё устройство, открой «Моя подписка» -> «Добавить устройство».\n\n"
                    "Основная ссылка:\n\n" + link,
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

    if parts[0] == "show_devices":
        existing = store.get_active_request_by_chat_id(user_chat_id)
        if not existing or existing.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Показываю устройства.")
        bot.send_message(user_chat_id, format_devices_text(existing, config), user_devices_markup(existing))
        return

    if parts[0] == "show_subscription":
        existing = store.get_active_request_by_chat_id(user_chat_id)
        if not existing or existing.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Моя подписка.")
        bot.send_message(
            user_chat_id,
            f"Твоя VPN-подписка: {subscription_display(existing)}.\n"
            f"Профиль: #{existing['id']}, {profile_label(str(existing.get('profile_type') or 'default'))}.\n"
            f"Тариф: {plan_label(existing.get('plan_id'))}.\n"
            f"Устройства: {len(request_devices(existing))}/{request_device_limit(existing)}.",
            subscription_actions_markup(int(existing["id"])),
        )
        return

    if parts[0] == "add_device":
        existing = store.get_active_request_by_chat_id(user_chat_id)
        if not existing or existing.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        devices = request_devices(existing)
        limit = request_device_limit(existing)
        if len(devices) >= limit:
            bot.answer_callback_query(callback_id, "Лимит устройств уже достигнут.")
            bot.send_message(
                user_chat_id,
                f"По твоему тарифу доступно устройств: {limit}. Сейчас уже создано: {len(devices)}.\n\n"
                "Чтобы добавить ещё устройство, нажми «Сменить тариф».",
                subscription_actions_markup(int(existing["id"])),
            )
            return
        request_id = int(existing["id"])
        device_id = next_device_id(existing)
        profile_type = str(existing.get("profile_type") or "default")
        client_email = f"tg-{existing['chat_id']}-{request_id}-d{device_id}"
        client_uuid = str(uuid.uuid4())
        device_name = f"Устройство {device_id}"
        label = f"VPN {request_id} D{device_id} {profile_short(profile_type)}"
        link = build_vless_link(config, client_uuid, profile_type, label)
        bot.safe_answer_callback_query(callback_id, "Создаю устройство...")
        try:
            manager.save_client(client_email, client_uuid)
        except Exception as exc:
            logging.exception("Failed to add VPN device")
            bot.send_message(user_chat_id, f"Не смог добавить устройство: {exc}", subscription_actions_markup(request_id))
            return
        updated = store.add_device(request_id, device_id, device_name, profile_type, client_email, client_uuid)
        if not updated:
            bot.send_message(user_chat_id, "Устройство создано на сервере, но не смог сохранить его в базе. Напиши админу.", subscription_actions_markup(request_id))
            return
        bot.send_message(
            user_chat_id,
            f"Готово. Добавлено {device_name}.\n\n"
            "Эту ссылку ставь только на одно устройство:\n\n"
            + link,
            user_devices_markup(updated),
        )
        return

    if parts[0] == "reissue_device" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        request_id = int(parts[1])
        device_id = int(parts[2])
        row = store.get_request(request_id)
        if not row or int(row.get("chat_id") or 0) != user_chat_id or row.get("status") != "approved":
            bot.answer_callback_query(callback_id, "Активный профиль не найден.")
            return
        device = next((item for item in request_devices(row) if int(item.get("device_id") or 0) == device_id), None)
        if not device:
            bot.answer_callback_query(callback_id, "Устройство не найдено.")
            return
        profile_type = str(device.get("profile_type") or row.get("profile_type") or "default")
        client_email = str(device["client_email"])
        client_uuid = str(uuid.uuid4())
        label = f"VPN {request_id} D{device_id} {profile_short(profile_type)}"
        link = build_vless_link(config, client_uuid, profile_type, label)
        bot.safe_answer_callback_query(callback_id, "Перевыпускаю устройство...")
        try:
            manager.save_client(client_email, client_uuid)
            manager.reset_profile_guard_binding(client_email)
        except Exception as exc:
            logging.exception("Failed to reissue VPN device")
            bot.send_message(user_chat_id, f"Не смог перевыпустить {device_label(device)}: {exc}", user_devices_markup(row))
            return
        updated = store.update_device(request_id, device_id, client_uuid, profile_type)
        if not updated:
            bot.send_message(user_chat_id, "Ссылка обновлена на сервере, но не смог сохранить её в базе. Напиши админу.", user_devices_markup(row))
            return
        bot.send_message(
            user_chat_id,
            f"Готово. Ссылка для {device_label(device)} перевыпущена.\n"
            "Старая ссылка этого устройства больше не работает.\n\n"
            "Новая VPN-ссылка:\n\n"
            + link,
            user_devices_markup(updated),
        )
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

    if parts[0] == "client" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Карточка клиента.")
        send_client_card(bot, manager, user_chat_id, row)
        return

    if parts[0] in {"client_extend", "client_plan", "client_operator", "client_access"} and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        title = {
            "client_extend": "Продление подписки",
            "client_plan": "Смена тарифа",
            "client_operator": "Перевыпуск под оператора",
            "client_access": "Доступ клиента",
        }[parts[0]]
        markup = {
            "client_extend": admin_client_extend_markup,
            "client_plan": admin_client_plan_markup,
            "client_operator": admin_client_operator_markup,
            "client_access": admin_client_access_markup,
        }[parts[0]](row)
        bot.answer_callback_query(callback_id, title)
        bot.send_message(user_chat_id, f"{title} для профиля #{request_id}", markup)
        return

    if parts[0] == "client_devices" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row:
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        bot.answer_callback_query(callback_id, "Устройства клиента.")
        bot.send_message(user_chat_id, format_devices_text(row, config, include_links=False), admin_client_markup(row))
        return

    if parts[0] == "client_stats" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or not row.get("client_email"):
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        bot.safe_answer_callback_query(callback_id, "Собираю статистику...")
        try:
            all_emails = [
                email
                for item in store.list_approved_requests()
                for email in request_device_emails(item)
            ]
            stats = manager.get_usage_stats(str(row["client_email"]), all_emails)
            bot.send_message(user_chat_id, format_usage_stats(row, stats), admin_client_markup(row))
        except Exception as exc:
            logging.exception("Could not load client stats")
            bot.send_message(user_chat_id, f"Не смог получить статистику профиля #{request_id}: {exc}", admin_client_markup(row))
        return

    if parts[0] == "extend_until_help" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        bot.answer_callback_query(callback_id, "Отправь дату сообщением.")
        bot.send_message(
            user_chat_id,
            f"Чтобы продлить профиль #{request_id} до конкретной даты, отправь:\n/extend_until {request_id} YYYY-MM-DD\n\nНапример:\n/extend_until {request_id} 2026-05-30",
            admin_client_markup({"id": request_id}),
        )
        return

    if parts[0] == "reserve_link" and len(parts) == 2 and parts[1].isdigit():
        request_id = int(parts[1])
        row = store.get_request(request_id)
        if not row or not row.get("uuid"):
            bot.answer_callback_query(callback_id, "Профиль не найден.")
            return
        if not config.reserve_vpn_host:
            bot.answer_callback_query(callback_id, "Резервный сервер не настроен.")
            bot.send_message(user_chat_id, "Резервная ссылка появится после настройки переменной RESERVE_VPN_HOST.", admin_client_markup(row))
            return
        profile_type = str(row.get("profile_type") or "default")
        label = f"VPN {request_id} reserve"
        link = build_vless_link(config, str(row["uuid"]), profile_type, label, host=config.reserve_vpn_host)
        bot.answer_callback_query(callback_id, "Резервная ссылка создана.")
        bot.send_message(user_chat_id, f"Резервная ссылка профиля #{request_id}:\n\n{link}", admin_client_markup(row))
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
        removed_devices: list[dict[str, Any]] = []
        try:
            removed_devices = enforce_device_limit(manager, store, updated)
        except Exception as exc:
            logging.exception("Could not enforce device limit after admin plan change")
            bot.send_message(user_chat_id, f"Тариф изменён, но не смог отключить лишние устройства: {exc}")
        bot.answer_callback_query(callback_id, "Тариф изменён.")
        extra_text = f" Отключено лишних устройств: {len(removed_devices)}." if removed_devices else ""
        send_admin_result(
            bot,
            user_chat_id,
            int(updated["chat_id"]),
            f"Тариф VPN-профиля #{request_id} изменён админом: {old_plan} -> {new_plan}.{extra_text}",
            f"Готово. Тариф профиля #{request_id} изменён: {old_plan} -> {new_plan}.{extra_text}",
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
        removed_devices: list[dict[str, Any]] = []
        try:
            removed_devices = enforce_device_limit(manager, store, updated)
        except Exception as exc:
            logging.exception("Could not enforce device limit after plan approval")
            bot.send_message(user_chat_id, f"Тариф изменён, но не смог отключить лишние устройства: {exc}")
        bot.answer_callback_query(callback_id, "Тариф изменён.")
        extra_text = f" Отключено лишних устройств: {len(removed_devices)}." if removed_devices else ""
        send_admin_result(
            bot,
            user_chat_id,
            int(updated["chat_id"]),
            f"Тариф VPN-профиля #{request_id} изменён: {old_plan} -> {new_plan}.{extra_text}",
            f"Готово. Тариф профиля #{request_id} изменён: {old_plan} -> {new_plan}.{extra_text}",
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

        client_emails = request_device_emails(row)
        bot.safe_answer_callback_query(callback_id, "Отключаю пользователя...")
        try:
            for client_email in client_emails:
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
                for device in request_devices(row):
                    manager.save_client(str(device["client_email"]), str(device["uuid"]))
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
            manager.reset_profile_guard_binding(client_email)
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
            f"Заявка одобрена. Тариф: {selected_plan_label}. Подписка: {subscription_text}.\n"
            "Создано Устройство 1. Если по тарифу доступно больше устройств, открой «Моя подписка» -> «Добавить устройство».\n\n"
            "Твоя VPN-ссылка:\n\n" + link,
            f"Готово. Заявка #{request_id} одобрена как {profile_label(profile_type)}. Тариф: {selected_plan_label}. Подписка: {subscription_text}.",
        )
        send_routing_instructions(bot, config, int(row["chat_id"]))


def check_sharing_alerts(bot: TelegramBot, store: Store, config: Config, manager: XrayManager) -> None:
    rows = store.list_approved_requests()
    email_to_row: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in rows:
        for device in request_devices(row):
            email = str(device.get("client_email") or "")
            if email:
                email_to_row[email] = (row, device)
    if not email_to_row:
        return

    recent_ips = manager.get_recent_ips_by_email(list(email_to_row), SHARING_LOOKBACK_MINUTES)
    for email, ips in recent_ips.items():
        row, device = email_to_row[email]
        alert_ip_limit = 1 + SHARING_IP_GRACE
        if len(ips) <= alert_ip_limit or not store.should_send_sharing_alert(email):
            continue
        request_id = int(row["id"])
        username = str(row.get("username") or "-")
        profile_type = str(device.get("profile_type") or row.get("profile_type") or "default")
        plan = plan_label(row.get("plan_id"))
        ip_list = ", ".join(sorted(ips))
        text = (
            f"Подозрение на шаринг VPN #{request_id}\n"
            f"Пользователь: @{username}\n"
            f"Устройство: {device_label(device)}\n"
            f"Тип: {profile_label(profile_type)}\n"
            f"Тариф: {plan}\n"
            f"Лимит: одна ссылка = одно устройство\n"
            f"За последние {SHARING_LOOKBACK_MINUTES} мин одна ссылка была с разных IP:\n"
            f"{ip_list}\n\n"
            "Это может быть пересланная ссылка. Проверь и выбери действие."
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, text, sharing_alert_markup(request_id, profile_type))
        store.mark_sharing_alert(email)


def check_expired_subscriptions(bot: TelegramBot, store: Store, config: Config, manager: XrayManager) -> None:
    for days_before in SUBSCRIPTION_NOTICE_DAYS:
        for row in store.find_subscription_notice_requests(days_before):
            request_id = int(row["id"])
            until_text = subscription_display(row)
            bot.send_message(
                int(row["chat_id"]),
                f"Напоминание: подписка VPN-профиля #{request_id} скоро закончится: {until_text}.",
            )
            for admin_chat_id in config.admin_chat_ids:
                bot.send_message(
                    admin_chat_id,
                    f"Подписка скоро закончится: профиль #{request_id}, пользователь {format_username(str(row.get('username') or ''))}, {until_text}.",
                )
            store.mark_subscription_notice(request_id, days_before)

    for row in store.find_expired_requests():
        request_id = int(row["id"])
        client_emails = request_device_emails(row)
        if not client_emails:
            continue
        try:
            for client_email in client_emails:
                manager.remove_client(client_email)
        except Exception:
            logging.exception("Failed to disable expired VPN profile #%s", request_id)
            continue
        store.expire_request(request_id)
        username = format_username(str(row.get("username") or ""))
        text = f"Подписка профиля #{request_id} истекла. VPN-ссылки устройств отключены."
        bot.send_message(int(row["chat_id"]), text)
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, f"{text}\nПользователь: {username}\nChat ID: {row['chat_id']}")


def check_inactive_clients(bot: TelegramBot, store: Store, config: Config, manager: XrayManager) -> None:
    rows = store.list_approved_requests()
    emails = [email for row in rows for email in request_device_emails(row)]
    if not emails:
        return
    last_seen = manager.get_last_seen_by_email(emails)
    for row in store.find_inactive_requests(last_seen):
        request_id = int(row["id"])
        marker = f"inactive_notice_{INACTIVE_CLEANUP_DAYS}d"
        if row.get(marker):
            continue
        username = format_username(str(row.get("username") or ""))
        text = (
            f"Профиль #{request_id} неактивен больше {INACTIVE_CLEANUP_DAYS} дней.\n"
            f"Пользователь: {username}\n"
            f"Последняя активность: {format_age(last_seen.get(str(row.get('client_email') or '')))}\n\n"
            "Автоотключение не выполнено, чтобы случайно не удалить живого клиента. Можно отключить вручную из карточки."
        )
        for admin_chat_id in config.admin_chat_ids:
            bot.send_message(admin_chat_id, text, admin_client_markup(row))
        data = store._read()
        for request in data["requests"]:
            if int(request.get("id") or 0) == request_id:
                request[marker] = utc_now_iso()
        store._write(data)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    store = Store(config.db_path)
    bot = TelegramBot(config.telegram_token)
    manager = XrayManager(config)
    start_routing_web_server(config, store, manager, bot)
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
    try:
        repaired = store.repair_orphan_device_requests()
        if repaired:
            logging.info("Repaired %s orphan device records", repaired)
    except Exception:
        logging.exception("Could not repair orphan device records")
    logging.info("VPN approval bot started; db_path=%s", config.db_path)
    offset = None
    last_sharing_check = 0.0
    last_subscription_check = 0.0
    last_inactive_check = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_subscription_check >= SUBSCRIPTION_CHECK_INTERVAL_SECONDS:
                try:
                    check_expired_subscriptions(bot, store, config, manager)
                except Exception:
                    logging.exception("Subscription expiration check failed")
                last_subscription_check = now

            if now - last_inactive_check >= INACTIVE_CHECK_INTERVAL_SECONDS:
                try:
                    check_inactive_clients(bot, store, config, manager)
                except Exception:
                    logging.exception("Inactive client check failed")
                last_inactive_check = now

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
