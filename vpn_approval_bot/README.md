# VPN Approval Bot

Separate Telegram bot for issuing personal Xray/VLESS REALITY profiles after admin approval.

## Flow

1. User sends `/vpn` or presses `Получить VPN`.
2. User chooses an operator profile.
3. Admin receives an approval request.
4. Bot creates a new UUID in every VLESS inbound on the Xray server.
5. Bot backs up the previous Xray config before changing it.
6. If Xray fails to restart or become active, the bot restores the backup.
7. User receives a personal `vless://` link.

## Operator Profiles

The bot supports these profile types:

- `Обычный оператор` -> `VPN_DEFAULT_PORT`, usually `443`.
- `МТС` -> `VPN_MTS_PORT`, usually `8443`.
- `МегаФон` -> `VPN_MTS_PORT`, usually `8443`.
- `Билайн` -> `VPN_DEFAULT_PORT`, usually `443`.
- `Tele2` -> `VPN_MTS_PORT`, usually `8443`.
- `Yota` -> `VPN_MTS_PORT`, usually `8443`.
- `Ростелеком` -> `VPN_DEFAULT_PORT`, usually `443`.
- `Т-Мобайл` -> `VPN_MTS_PORT`, usually `8443`.
- `T-Mobile` -> `VPN_ALT_PORT`, usually `2053`.

Old profiles with `default` and `mts` remain valid.

## Happ Routing

The bot can send a Happ routing QR/link from `Моя подписка` or `/routing`.

Routing mode:

- TikTok and its CDN domains go through VPN.
- Russian services go directly, bypassing VPN.
- The direct whitelist includes `.ru`, `.рф`, banks, Yandex, VK, Gosuslugi, government sites, operators, marketplaces, maps, delivery services, Russian media, Apple/Google connectivity checks, and common captcha domains.

This reduces bank login errors, captchas, and slow loading for local services while keeping TikTok routed through VPN.

## Device Profiles

The bot counts devices by separate Xray/VLESS profiles, not by Wi-Fi or IP address.

- One VPN link is intended for one device.
- The first approved link becomes `Устройство 1`.
- Users can press `Моя подписка` -> `Добавить устройство` to create more device links up to their tariff limit.
- Each added device gets its own UUID and Xray email like `tg-CHAT-REQUEST-d2`.
- If a tariff is downgraded below the current device count, extra device links are removed from Xray and stop working.
- Sharing detection now checks each device link separately. One device link appearing from several IPs can trigger a sharing warning.

## Admin Features

Admins get a persistent `Список клиентов` button. It shows paid approved profiles, selected operator profile, creation time, and how long ago each client was last seen in Xray logs. Free users are intentionally excluded from this list and shown only in `Бесплатные клиенты`.

When an admin requests `Список клиентов` or `Бесплатные клиенты` again, the bot deletes the previous client-list messages it sent in that chat before sending the fresh list.

Admins can also reissue a client link into any operator profile. Reissue replaces the old UUID and resets the server-side first-IP binding for that profile.

Admins can disable a client from the client card. Disable removes that client's UUID from Xray, so the old VPN link stops working completely and not just from a specific IP.

Admins can mark approved users as free clients from the client card. Free clients are shown in the `Бесплатные клиенты` admin list and are skipped by automatic subscription expiration checks.

Free client cards do not show subscription extension buttons, because they are not controlled by paid subscription dates. Admins can still change the device plan for both paid and free clients directly from the client card; this updates the saved device limit without changing the VPN link.

Client cards use compact action menus. The main card shows only high-level sections: subscription extension, tariff, operator/reissue, statistics, reserve link, and access controls. Each section opens its own inline submenu so the chat is not filled with every possible action at once.

Admins can run `/check_vpn`, press `Проверить VPN`, or press `Ресурсы сервера` to see a Russian grouped server summary: VPN services, uptime, CPU, load average, RAM, disk usage, active VPN TCP connections, open VPN ports, and whether `RESERVE_VPN_HOST` is configured. If `RESERVE_VPN_HOST` is set, the client card can generate a reserve VLESS link for the same UUID using that host.

Client cards have a `Статистика` section. It shows grouped per-profile activity in Telegram: recent connection count, share of total VPN activity, active profiles in the log sample, unique IP count, first seen time, and last seen time. Xray does not expose exact CPU/RAM per user in this setup, so per-user resource usage is represented by real profile activity from Xray logs.

Xray config writes are serialized with a remote lock before the bot adds, reissues, or disables a profile. This prevents overlapping approvals from corrupting the config or stacking multiple Xray restarts. The current server does not expose the Xray API, so profile add/remove operations can still cause a short reconnect while Xray restarts; fully zero-drop provisioning requires enabling the Xray API in a separate migration.

The bot also watches recent Xray logs for soft sharing detection. If one approved profile appears from two or more different IPs within the recent activity window, admins receive a warning with `Отключить`, `Перевыпустить`, and `Игнорировать` actions. The bot does not auto-block users from this check.

The subscription checker also sends reminders before expiration, currently at 3 days and 1 day before the deadline. Admins can set an exact subscription date with `/extend_until ID YYYY-MM-DD`. The bot monitors inactive paid clients and notifies admins if a profile has not appeared in recent activity for the configured inactivity window; it does not auto-delete those profiles without admin confirmation.

Performance notes: the bot keeps heavy VPS log reads limited. Client-list activity uses a bounded journal read, sharing checks read only the recent window, detailed statistics are loaded only when an admin opens a client's `Статистика`, and inactive-client checks run once per day instead of every subscription cycle.

## Setup

Copy `.env.example` values into environment variables or create `config.json` with lowercase keys matching the env names.

Required values:

```text
TELEGRAM_BOT_TOKEN
ADMIN_CHAT_IDS
VPN_SSH_PASSWORD
VPN_PUBLIC_KEY
VPN_SHORT_ID
```

Install dependencies:

```powershell
python -m pip install -r vpn_approval_bot\requirements.txt
```

Run:

```powershell
python vpn_approval_bot\bot.py
```

## Railway

Deploy the repository from GitHub. Railway uses the root `railway.json` and starts the bot with:

```text
python -X utf8 vpn_approval_bot/bot.py
```

Add these variables in Railway:

```text
TELEGRAM_BOT_TOKEN
ADMIN_CHAT_IDS
DB_PATH
REMOTE_STATE_PATH
VPN_SSH_HOST
VPN_SSH_PORT
VPN_SSH_USER
VPN_SSH_PASSWORD
XRAY_CONFIG_PATH
VPN_BACKUP_DIR
VPN_HOST
VPN_SNI
VPN_PUBLIC_KEY
VPN_SHORT_ID
VPN_DEFAULT_PORT
VPN_MTS_PORT
VPN_ALT_PORT
VPN_DEFAULT_SUBSCRIPTION_DAYS
RESERVE_VPN_HOST
PAYMENT_QR_URL
PAYMENT_LINK
PAYMENT_TBANK_LINK
PAYMENT_RECIPIENT
PAYMENT_BANKS
PUBLIC_BASE_URL
```

Recommended Railway values:

```text
ADMIN_CHAT_IDS=8611021280
DB_PATH=/data/vpn_approval.json
REMOTE_STATE_PATH=/usr/local/etc/xray/vpn_approval_state.json
VPN_SSH_PORT=22
VPN_SSH_USER=root
XRAY_CONFIG_PATH=/usr/local/etc/xray/config.json
VPN_BACKUP_DIR=/usr/local/etc/xray
VPN_DEFAULT_PORT=443
VPN_MTS_PORT=8443
VPN_ALT_PORT=2053
VPN_DEFAULT_SUBSCRIPTION_DAYS=30
RESERVE_VPN_HOST=
PAYMENT_LINK=https://www.sberbank.com/sms/pbpn?requisiteNumber=79050122709
PAYMENT_TBANK_LINK=https://www.tinkoff.ru/rm/r_fpDGcUCZRx.uCrfsYPegf/WuswZ21804
PAYMENT_QR_URL=
PAYMENT_RECIPIENT=Вадим
PAYMENT_BANKS=Сбер / Т-Банк
PUBLIC_BASE_URL=https://your-railway-domain.up.railway.app
```

If you use `DB_PATH=/data/vpn_approval.json`, attach a Railway volume mounted at `/data` so requests and clients survive restarts.

`REMOTE_STATE_PATH` is an extra safety copy stored on the VPS. After every local database change, the bot mirrors the full approval database there. On startup, it restores from this remote copy before importing missing UUIDs from Xray, so subscription extensions and selected device limits do not fall back to default values after Railway restarts or volume resets.

On startup, the bot also tries to restore missing approved clients from the Xray config. This helps after migration to Railway if the database starts empty but the VPN profiles already exist on the server.

## Subscriptions

New approved users get `VPN_DEFAULT_SUBSCRIPTION_DAYS` days by default. Admin client cards include `+7 дней`, `+30 дней`, and `+90 дней` buttons. The bot checks active subscriptions every 10 minutes and removes expired VPN UUIDs from Xray.

User-facing tariff plans:

```text
1 device  - 200 руб/мес
2 devices - 300 руб/мес
3 devices - 400 руб/мес
4 devices - 500 руб/мес
5 devices - 600 руб/мес
```

The price list is shown after `/start` and through the `Прайс лист` button. During VPN request creation, the user selects a tariff first and then selects an operator profile.

Approved users can press `Сменить тариф` or use `/change_plan` to move the existing VPN link to another device plan. The UUID/link stays the same; after manual payment verification, the admin approves the change and the bot updates only the saved device limit and plan price.

Users can open `Моя подписка` to see their current status, price list, and action buttons for reissue, plan changes, and Happ routing. The routing action sends one QR-code message with an `Открыть в Happ` button under it. The QR image is generated by the bot at `/happ-routing-qr.png` and contains the short `/happ-routing` HTTPS URL, not the huge raw Happ payload. The button points to the same endpoint and that endpoint immediately redirects to the generated `happ://routing/onadd/...` link, because Telegram inline URL buttons do not reliably accept custom app schemes directly. TikTok domains go through VPN, while Russian IPs, VK, Yandex, Gosuslugi, Russian media, marketplaces, job sites, common Russian banks, and browser captcha services go direct outside the VPN. This is client-side split tunneling, so it must be applied in the user's VPN app.

If the bot has to restore approved VPN profiles directly from the Xray config after a redeploy or database loss, it no longer grants a new 30-day subscription automatically. Restored profiles are marked with an unknown deadline until the saved bot database/remote state is available or an admin sets the subscription manually.

After the user selects a tariff and an operator, the bot asks whether they want to pay through Sber or T-Bank. `PAYMENT_LINK` is used for Sber, `PAYMENT_TBANK_LINK` is used for T-Bank, and `PAYMENT_QR_URL` can override the Sber QR image. The message includes the exact amount and comment like `VPN #15`. The user can press `Я оплатил`; the admin manually checks the incoming payment and approves the VPN request.

Admin commands:

```text
/clients
```

User commands:

```text
/vpn
/reissue
/change_plan
/routing
/vpn_status ID
```

`/reissue` lets an approved user request a new link. The old UUID is replaced
only after admin approval.

## Notes

- Keep `VPN_SSH_PASSWORD` out of Git.
- `ADMIN_CHAT_IDS` can contain multiple comma-separated Telegram IDs.
- Keep ports `443`, `8443`, and `2053` open on the server if all operator profiles are used.
