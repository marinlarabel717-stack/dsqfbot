# dsqfbot

`dsqfbot` is a button-driven Telegram admin bot for managing multiple user `session` accounts, importing group links in batches, joining with smart spacing, checking speaking restrictions, and creating Telegram scheduled messages.

## What It Does

- Button-only bot UI, no slash-command workflow required
- Add and verify multiple Telegram user sessions through the bot
- Import one or more existing `.session` files from a zip archive
- Sync group/channel lists for each session
- Batch import `t.me` links and enqueue join jobs
- Smart join intervals to reduce aggressive join bursts
- Detect readable group status such as:
  - can speak
  - muted
  - not joined
  - awaiting approval
  - no speaking permission
  - real send-probe result using a temporary random emoji
- Create scheduled Telegram messages
- Daily repeat support for premium-capable accounts via rolling future scheduling
- View queued join jobs
- View saved scheduled tasks
- View scheduled messages already stored in Telegram for a group
- Cancel in-progress bot flows with a button

## Project Layout

```text
dsqfbot/
  dsqfbot/
    __init__.py
    app.py
    config.py
    db.py
    telegram_mtproto.py
    utils.py
  .env.example
  .gitignore
  main.py
  requirements.txt
  README.md
```

## Setup

```bash
git clone https://github.com/marinlarabel717-stack/dsqfbot.git
cd dsqfbot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Environment

Copy `.env.example` to `.env` and fill at least:

```env
BOT_TOKEN=
ADMIN_IDS=
API_ID=
API_HASH=
DATABASE_PATH=storage/dsqfbot.sqlite3
SESSION_DIR=storage/sessions
CLIENT_DEVICE_MODEL=DSQFBot
CLIENT_SYSTEM_VERSION=Linux
CLIENT_APP_VERSION=1.0
CLIENT_LANG_CODE=zh-hans
CLIENT_SYSTEM_LANG_CODE=zh-hans
DEFAULT_JOIN_INTERVAL_SECONDS=60
REPEAT_LOOKAHEAD_MINUTES=5
DEFAULT_TIMEZONE=Asia/Shanghai
```

Notes:

- `BOT_TOKEN`: Telegram bot token for the admin bot
- `ADMIN_IDS`: allowed Telegram user IDs, comma-separated
- `API_ID` and `API_HASH`: Telegram app credentials for Telethon
- `DATABASE_PATH`: SQLite database path
- `SESSION_DIR`: folder for user session files
- `CLIENT_*`: explicit client fingerprint sent during login instead of relying on Telethon defaults

## Run

```bash
python main.py
```

## Button Flow

1. Send any message to the bot
2. Open `账号管理`
3. Add one or more user accounts, or upload a zip containing `.session` files
4. Open `批量加群`
5. Select sessions and distribution mode
6. Paste group links
7. Select the join interval button
8. Sync groups or open a group detail page
9. Create a scheduled message from the group detail page

## Current Behavior

- Public groups and invite links can be processed automatically
- Approval-based invite links stay in `awaiting_approval`
- Join queue retries flood-wait cases automatically
- Session status is marked offline when the account is no longer authorized
- Daily repeat depends on the bot process staying online so it can keep scheduling the next occurrence

## Clone URL

```bash
git clone https://github.com/marinlarabel717-stack/dsqfbot.git
```
