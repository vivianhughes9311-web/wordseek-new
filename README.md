# WordSeek — Solver + Telegram Autoplay

A Flask app in two parts:

1. **Solver** (`/`) — the panda-themed WordSeek solver. Paste a 4/5-letter board and it
   ranks the best guesses instantly. Unchanged and always public.
2. **Autoplay dashboard** (`/dashboard`) — each visitor **logs in with their own Telegram
   account** (phone number + the code Telegram sends), picks one of their groups, and lets
   the app watch that group, solve every WordSeek board, and play entire rounds
   automatically — event-driven, one guess per confirmed board update.

The Telegram clients run in **one background thread with its own asyncio loop**, so they
never block Flask and never take the web server down.

> ⚠️ **Read before deploying.** Logging a real user account in and auto-playing a game bot
> is exactly the kind of automation Telegram and game bots flag — accounts can be limited or
> banned. Each connected account's session is stored **encrypted at rest**, but if your
> server is compromised those sessions are exposed. Set a strong, stable `SECRET_KEY`
> (it is the encryption key), treat the data volume as sensitive, and only run this in groups
> where automation is allowed. You are responsible for how it is used.

## How login works

The app uses **one app-level** `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` (you set these once,
from <https://my.telegram.org>). Users never need their own API credentials — they only enter
their **phone number**, then the **code** Telegram sends, and a **2FA password** if their
account has one. The resulting session string is AES-encrypted (`crypto_store`, key derived
from `SECRET_KEY`) and saved under `DATA_DIR`. It is never shown in the browser or logged.

Each browser gets a signed `uid` cookie = one account. Different people get different uids and
their own isolated client, group, and autoplay engine.

## Autoplay (event-driven)

For every new message in the selected group the engine:

1. classifies it (board / win / loss / new game),
2. on a **new, confirmed** board state, re-solves the **full** board with the existing solver
   (so every green/yellow/red result is re-applied — it is **not** a pre-generated word list),
3. sends the best guess (respecting delay, cooldown, min-confidence),
4. waits for the bot's next board, and repeats until the word is solved, the game ends, the
   guess limit is hit, it is paused, or an error occurs.

Safety: at most one guess per distinct board state, message-id + normalized-board de-dupe,
strict cooldown, FloodWait handling with bounded retries, stop-on-repeat, per-game state
machine (`IDLE → WAITING_FOR_BOARD → BOARD_DETECTED → SOLVING → WAITING_TO_SEND → GUESS_SENT
→ WAITING_FOR_UPDATE → WON/LOST/PAUSED/ERROR`), and sends only ever go to the selected group.

## Folder structure

```
wordseek/
├── app.py                # Flask: solver + optional gate + dashboard + /api/* controls
├── solver.py             # WordSeek solving algorithm (unchanged)
├── board.py              # board detection / de-dupe key (reuses solver.parse_board)
├── autoplay.py           # event-driven autoplay state machine (one per user)
├── telegram_worker.py    # multi-account Telegram service (login, clients, dispatch)
├── users.py              # per-user JSON store (settings + autoplay config + activity)
├── crypto_store.py       # AES-256-CBC + HMAC session encryption (pure-python, pyaes)
├── security.py           # uid identity, optional access gate, CSRF, rate limiting
├── words4.txt / words5.txt
├── requirements.txt
├── Procfile              # gunicorn, 1 worker (single process for all clients)
├── railway.json          # start command + /health check
├── vercel.json           # solver UI/API only (no persistent Telegram)
├── templates/            # index.html, dashboard.html, login.html
├── static/               # style/script (solver), dashboard.css/.js, favicon.svg
└── data/                 # runtime (users/<uid>.json) — mount a volume here on Railway
```

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_API_ID` | **yes** (for login) | App API id from my.telegram.org. |
| `TELEGRAM_API_HASH` | **yes** (for login) | App API hash from my.telegram.org. |
| `SECRET_KEY` | **strongly** | Signs cookies **and** derives the session-encryption key. Use a long random value and keep it stable — changing it forces everyone to re-login. |
| `DATA_DIR` | recommended | Where per-user data lives. Set to your volume path, e.g. `/data`. |
| `DASHBOARD_PASSWORD` | optional | If set, the whole dashboard sits behind this shared password (private instance). If **unset**, the dashboard is open and Telegram login is the only auth. |
| `COOKIE_SECURE` | optional | `1` to mark cookies Secure (recommended behind HTTPS). |

Nothing sensitive is hardcoded; all secrets come from the environment.

## Deploy on Railway (recommended)

1. Push to GitHub, create a Railway project from the repo.
2. Railway installs `requirements.txt` and runs the `startCommand` in `railway.json`
   — gunicorn with **exactly 1 worker** (required: one process holds every user's client and
   the in-progress login state).
3. **Add a Volume mounted at `/data`** and set `DATA_DIR=/data` so sessions/settings survive
   restarts. On restart, each account reconnects automatically from its stored session.
4. Set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and a strong `SECRET_KEY` (optionally
   `DASHBOARD_PASSWORD`, `COOKIE_SECURE=1`).
5. Health check path is `/health` (returns `200` even if Telegram is idle).
6. Open **Dashboard**, log in with your phone + code, load groups, pick a target, then turn on
   **Autoplay** and press **Start**.

## Deploy on Vercel (solver only)

`vercel.json` serves the solver UI and `/solve`. Vercel is serverless and **cannot hold a live
Telegram connection**, so autoplay/monitoring will not run there — use Railway for that.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")
export TELEGRAM_API_ID=...  TELEGRAM_API_HASH=...
export DATA_DIR=./data
python app.py                      # http://127.0.0.1:51332
```

The solver works with no configuration. The dashboard needs the two Telegram variables to log
in; set `DASHBOARD_PASSWORD` too if you want a shared access gate.

## Security summary

- Secrets are env-only; never returned to the browser or logged.
- Session strings are encrypted at rest (AES-256-CBC + HMAC).
- All control routes require the uid session, a CSRF token, and are rate-limited.
- Autoplay only sends valid guesses, once per board, to the selected group, with cooldowns.
- Auto-reconnect + FloodWait/expired-session handling keep the server up through failures.
```
