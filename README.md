# WordSeek — Solver + Telegram Monitor

A Flask app with two parts:

1. **Solver** (`/`) — the original panda-themed WordSeek solver. Paste a 4/5-letter
   board and it ranks the best guesses instantly. Unchanged.
2. **Telegram monitor** (`/dashboard`) — a password-protected control panel that runs
   a **Telethon** client on the server to watch a group, auto-solve new WordSeek
   boards, and send the best guess back (manually or automatically).

The Telegram client runs in a **background thread with its own asyncio loop**, so it
never blocks Flask and never takes the web server down if Telegram has a problem.

> **Railway is the primary target** — it keeps the process alive so monitoring persists.
> Vercel is only suitable for the solver UI/API; its serverless functions cannot hold a
> live Telegram connection, so do not rely on it for monitoring.

## Folder structure

```
wordseek/
├── app.py                # Flask app: solver + auth + dashboard + /api control routes
├── solver.py             # WordSeek solving algorithm (unchanged)
├── board.py              # Board detection / de-dupe (reuses solver.parse_board)
├── telegram_worker.py    # Background Telethon manager (thread + asyncio loop)
├── storage.py            # JSON persistence for non-secret settings + activity
├── security.py           # Password login, CSRF, rate limiting
├── words4.txt / words5.txt
├── requirements.txt
├── Procfile              # gunicorn, 1 worker (single Telegram client)
├── railway.json          # Railway start command + /health check
├── vercel.json           # Solver UI/API only (no persistent monitoring)
├── templates/
│   ├── index.html        # Solver UI
│   ├── dashboard.html    # Telegram dashboard
│   └── login.html        # Dashboard login
├── static/
│   ├── style.css         # Solver styles (shared design tokens)
│   ├── script.js         # Solver logic
│   ├── dashboard.css     # Dashboard styles
│   ├── dashboard.js      # Dashboard logic
│   └── favicon.svg
└── data/                 # created at runtime (settings.json, activity.json)
```

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DASHBOARD_PASSWORD` | **yes** (for dashboard) | Password to access `/dashboard` and all controls. |
| `SECRET_KEY` | recommended | Signs session cookies. Set a long random value so logins survive restarts. |
| `TELEGRAM_API_ID` | for Telegram | From <https://my.telegram.org> → API development tools. |
| `TELEGRAM_API_HASH` | for Telegram | From the same page. |
| `TELEGRAM_STRING_SESSION` | user mode | A Telethon `StringSession` for your account (see below). |
| `TELEGRAM_BOT_TOKEN` | bot mode | A bot token from @BotFather (alternative to a string session). |
| `TELEGRAM_MODE` | optional | `user` or `bot`. Auto-detected if omitted. |
| `TELEGRAM_AUTOSTART` | optional | `1` (default) connects on boot; `0` waits for the **Connect** button. |
| `DATA_DIR` | optional | Where settings/activity are stored. Set to your volume path, e.g. `/data`. |
| `COOKIE_SECURE` | optional | `1` to mark session cookies Secure (recommended behind HTTPS). |

**Secrets are only ever read from the environment.** They are never written to disk,
never logged, and never sent to the browser.

> **Bot mode caveat:** a bot can only read messages in groups where it has been added
> and (usually) granted privacy-off/admin so it can see all messages. A user string
> session sees everything your account sees. Pick whichever fits your group.

### Generating a StringSession (user mode)

Run this **once locally** (never commit the output):

```bash
pip install telethon
python - <<'PY'
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id = int(input("api_id: "))
api_hash = input("api_hash: ")
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nTELEGRAM_STRING_SESSION=", client.session.save(), sep="")
PY
```

Paste the printed value into `TELEGRAM_STRING_SESSION` in Railway.

## Deploy on Railway (recommended)

1. Push this repo to GitHub and create a Railway project from it.
2. Railway installs `requirements.txt` and runs the `startCommand` in `railway.json`
   (gunicorn, **1 worker** so there is exactly one Telegram client).
3. **Add a Volume** and mount it at `/data`. Then set `DATA_DIR=/data` so your selected
   group and settings survive restarts.
4. Set the environment variables from the table above.
5. Health check path is `/health` (returns `200` even if Telegram is offline).
6. Open the app, go to **Dashboard**, log in, press **Connect**, load groups, pick your
   target group, then toggle **Auto-send** (or send manually).

## Deploy on Vercel (solver only)

`vercel.json` routes the app so the **solver UI and `/solve` API** work. Persistent
Telegram monitoring will **not** run on Vercel — use Railway for that.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export DASHBOARD_PASSWORD=changeme
export SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")
# optional Telegram:
export TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_STRING_SESSION=...

python app.py                      # http://127.0.0.1:51332
```

The solver works with no configuration. The dashboard needs `DASHBOARD_PASSWORD`;
the monitor additionally needs the Telegram variables.

## How monitoring works

1. A `NewMessage` handler fires for the selected group only.
2. `board.detect_board` checks the text for tiles and a valid 4/5-letter layout.
3. Each board is fingerprinted so the same board is never processed twice.
4. The existing `solve_board` ranks candidates; the best guess + top 5 are shown.
5. If **Auto-send** is on and the board is valid, the best guess is sent after the
   configured delay — **once per board**, guarded by a cooldown and FloodWait handling.

## Safety

- Dashboard + every control route require the password and a CSRF token; control
  endpoints are rate-limited.
- The status API returns only sanitized data (names/ids/flags) — never credentials.
- Auto-send only fires for valid boards with a real answer, once per board, respecting
  the cooldown; manual sends are restricted to the current result's guesses.
- Auto-reconnect handles dropped connections and Railway restarts; FloodWait and
  invalid/expired sessions are caught and surfaced in the dashboard instead of crashing.
```
