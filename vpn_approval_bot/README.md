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
