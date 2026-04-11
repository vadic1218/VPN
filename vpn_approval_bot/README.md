# VPN Approval Bot

Separate Telegram bot for issuing personal Xray/VLESS REALITY profiles after admin approval.

## Flow

1. User sends `/vpn`.
2. Admin receives an approval request.
3. Admin chooses `Обычный 443`, `МТС 8443`, or `Отклонить`.
4. Bot creates a new UUID in every VLESS inbound on the Xray server.
5. Bot backs up the previous Xray config before changing it.
6. If Xray fails to restart or become active, the bot restores the backup.
7. User receives a personal `vless://` link.

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

## Notes

- Keep `VPN_SSH_PASSWORD` out of Git.
- `ADMIN_CHAT_IDS` can contain multiple comma-separated Telegram IDs.
- MTS users receive `VPN_MTS_PORT`, currently `8443`.
- Regular users receive `VPN_DEFAULT_PORT`, currently `443`.
