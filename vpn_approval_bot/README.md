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

## Admin Features

Admins get a persistent `Список клиентов` button. It shows approved profiles, selected operator profile, creation time, and how long ago each client was last seen in Xray logs.

Admins can also reissue a client link into any operator profile. Reissue replaces the old UUID and resets the server-side first-IP binding for that profile.

Admins can disable a client from the client card. Disable removes that client's UUID from Xray, so the old VPN link stops working completely and not just from a specific IP.

The bot also watches recent Xray logs for soft sharing detection. If one approved profile appears from two or more different IPs within the recent activity window, admins receive a warning with `Отключить`, `Перевыпустить`, and `Игнорировать` actions. The bot does not auto-block users from this check.

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
PAYMENT_RECIPIENT
PAYMENT_BANKS
PAYMENT_QR_TEMPLATE
```

Recommended Railway values:

```text
ADMIN_CHAT_IDS=8611021280
DB_PATH=/data/vpn_approval.json
VPN_SSH_PORT=22
VPN_SSH_USER=root
XRAY_CONFIG_PATH=/usr/local/etc/xray/config.json
VPN_BACKUP_DIR=/usr/local/etc/xray
VPN_DEFAULT_PORT=443
VPN_MTS_PORT=8443
VPN_ALT_PORT=2053
VPN_DEFAULT_SUBSCRIPTION_DAYS=30
PAYMENT_RECIPIENT=Вадим
PAYMENT_BANKS=Сбер / Т-Банк
PAYMENT_QR_TEMPLATE=Оплата VPN #{request_id}\nСумма: {amount} руб\nКомментарий: {comment}\nПолучатель: {recipient}\nБанк: {banks}
```

If you use `DB_PATH=/data/vpn_approval.json`, attach a Railway volume mounted at `/data` so requests and clients survive restarts.

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

After the user selects a tariff and an operator, the bot generates a QR code for that exact request and sends it with the payment amount and comment like `VPN #15`. By default this QR contains payment instructions. If your bank gives you a real payment link template, put that link into `PAYMENT_QR_TEMPLATE` with placeholders such as `{amount}`, `{comment}`, and `{request_id}`.

Admin commands:

```text
/clients
```

User commands:

```text
/vpn
/reissue
/vpn_status ID
```

`/reissue` lets an approved user request a new link. The old UUID is replaced
only after admin approval.

## Notes

- Keep `VPN_SSH_PASSWORD` out of Git.
- `ADMIN_CHAT_IDS` can contain multiple comma-separated Telegram IDs.
- Keep ports `443`, `8443`, and `2053` open on the server if all operator profiles are used.
