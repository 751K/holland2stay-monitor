# Holland2Stay Listing Monitor

> For the Chinese (简体中文) version, see: [README_cn.md](README_cn.md)

A personal project that monitors Holland2Stay (https://www.holland2stay.com) for new listings and status changes, pushes notifications to multiple users, and can automatically add qualifying listings to the booking cart (stops before payment).

Note: Personal project — not for commercial use. Contributions, issues and PRs are welcome.

---

## Project status

| Component | Status | Notes |
|---|---:|---|
| Data scraping | ✅ Done | Uses GraphQL + curl_cffi to bypass Cloudflare WAF |
| Multi-city monitoring | ✅ Done | 26 Dutch cities; select cities in the web UI |
| Multi-channel notifications | ✅ Done | iMessage / Telegram / Email / WhatsApp (Twilio) |
| Web panel notifications | ✅ Done | Real-time bell + toasts via SSE, works on any platform |
| Notification filters | ✅ Done | Per-user filters: rent, area, floor, layout, district |
| Auto-booking | ✅ Done | Full flow: add to cart → place order → direct payment URL |
| Fast-path booking | ✅ Done | Reserved → Available booking submitted before notifications send |
| Web admin panel | ✅ Done | Dashboard, listings, users, global settings |
| Hot config reload | ✅ Done | Cross-platform reload, no restart required |
| Smart polling | ✅ Done | Peak hours auto-accelerate; adaptive interval probes rate limit |
| Rate limit protection | ✅ Done | 429 exponential backoff + 5-minute cooldown + proxy support |
| Multi-user support | ✅ Done | Each user has independent channels / filters / booker settings |
| VPS / Docker ready | ✅ Done | iMessage gracefully skipped on non-macOS; web panel takes over |
| Day/night theme | ✅ Done | Light/dark, follows OS preference without flicker |
| Visualization | ✅ Done | 30-day trends, city/status distribution, price histogram |
| Move-in calendar | ✅ Done | Calendar view filtered by city |
| Map view | ✅ Done | Leaflet.js + OpenStreetMap with auto-geocoding |
| i18n (中/EN) | ✅ Done | One-click language switch, cookie-persisted |
| Notification testing | ✅ Done | Per-channel test with result details |
| Optional auth for web | ✅ Done | Session login enabled when password set |

---

## Core features

### Data scraping

- Polls the Holland2Stay GraphQL API every N seconds (default: 5 minutes)
- Supports multi-city monitoring; cities can be selected in the web UI
- Detects both new listings and status changes, such as lottery → available to book
- Stores all listings in local SQLite so history remains queryable and duplicate notifications are avoided

### Smart adaptive polling

Normal intervals apply outside peak hours. During the Dutch morning release window (default 08:30–10:00 CET on weekdays), adaptive polling kicks in:

- Starts each peak session at `PEAK_INTERVAL` (default 60 s)
- After every successful scrape round, shrinks the interval by 5%, automatically probing how fast the API will tolerate
- Floors at `MIN_INTERVAL` (default 15 s, configurable) — never pushes below this
- On a 429 rate-limit response, doubles the current interval and holds a 5-minute cooldown before retrying
- Resets to `PEAK_INTERVAL` at the end of each peak window, ready to probe again tomorrow
- Randomised ±`JITTER_RATIO` % jitter on every sleep to avoid mechanical fingerprinting
- All parameters (PEAK_INTERVAL, MIN_INTERVAL, PEAK_START, PEAK_END, JITTER_RATIO, PEAK_WEEKDAYS_ONLY) configurable in the web UI

### Rate limit protection

- `scraper.py` retries a 429 response twice (waits 30 s then 60 s) before giving up
- On persistent rate-limiting, `monitor.py` raises a `RateLimitError`, notifies all users, and sleeps 5 minutes before resuming
- Optional proxy support: set `HTTPS_PROXY` or `HTTP_PROXY` in `.env` to route all scraping and booking traffic through a proxy; picked up at runtime so a hot reload applies the change without restart

### Fast-path booking

When a listing transitions from "Reserved" (or any other status) directly to "Available to book", the window to claim it is measured in seconds. The monitor:

1. Pre-scans the diff result in memory (no network calls)
2. Immediately submits `try_book()` to a thread pool, before any notification is sent
3. Runs the booking HTTP flow concurrently while notification sends are in flight
4. **Prewarmed session**: a pre-authenticated `curl_cffi` session is prepared for each auto-book user during notification dispatch, so `try_book()` can skip session creation + login — saving ~0.7 s per booking attempt
5. Awaits the booking result after notifications finish — in most cases the booking is already done

This reduces the delay between detecting the availability change and reaching the server to approximately 0–1 second instead of the former 2–5 second notification-first approach.

### Multi-user support

- Each user has independent channels, credentials, filters, and auto-book settings
- One scrape run is shared across all users — adding users does not multiply API traffic
- User data is stored in `data/users.json` and can be managed entirely from the web UI
- On first run, legacy notification env vars can be migrated into a default user automatically

### Notifications

**Per-user push channels** (iMessage, Telegram, Email, WhatsApp):

- Each user can enable one or more channels simultaneously
- Notification content includes status, rent, area, floor, energy label, move-in date, and listing link
- Per-user filters restrict which listings trigger a notification
- One-click per-channel test with per-result details in the web UI

**iMessage platform check**: iMessage requires macOS and the Messages.app. On Linux/Windows/Docker the channel is automatically skipped with a warning; the user-form page shows an alert if the server is not running macOS.

**Web panel notifications (platform-independent)**:

- Every event (new listing, status change, booking result, error, heartbeat) is also written to a `web_notifications` SQLite table
- The navbar bell icon shows an unread badge; clicking opens a dropdown of recent notifications
- Slide-in toast popups appear automatically for real-time events
- Powered by Server-Sent Events (SSE) at `GET /api/events` — the browser reconnects automatically on disconnect
- Works on all platforms including VPS and Docker, with no extra dependencies

### Auto-booking

- When a qualifying "Available to book" listing appears, the monitor can complete the booking workflow automatically
- Flow: login → `createEmptyCart` → `addNewBooking` → `placeOrder` (with `store_id=54`) → `idealCheckOut` (with `plateform="h"`)
- Sessions are pre-warmed (pre-logged-in) during the notification phase — each booking attempt skips session creation + login, saving ~0.7 s
- Matches the official H2S frontend booking flow verified via browser DevTools
- If `placeOrder` returns "another unit reserved" and `cancel_enabled` is on, auto-cancels the old order via `cancelOrder` mutation and retries the entire flow
- If `cancel_enabled` is off (default), the "another unit reserved" error is forwarded directly to the user — no cancel attempt is made (H2S disables `cancelOrder` by default)
- Sends a direct payment URL so payment can be completed without logging in again
- Supports stricter booking filters than notification filters, plus a dry-run mode for validation
- Booking runs concurrently with notifications (see Fast-path booking above)

### Web admin panel

- **Dashboard** — totals, today's new listings, recent changes, latest scrape info, auto-refresh
- **Listings** — filter by status, keyword search, sortable table view
- **Map** — Leaflet.js interactive map with auto-geocoding (Nominatim → cached coordinates), color-coded markers (green=direct book, orange=lottery, grey=other), popup details, dark/light tile filters
- **Calendar** — month grid with city filter, click-to-expand date detail panel
- **Stats** — Chart.js trends (new listings, status changes), doughnut distributions (city, status), price histogram, 7/30/90-day range selector
- **Users** — CRUD, enable/disable, per-user notification channels & filters & auto-booking config, one-click per-channel test
- **Global Settings** — polling intervals, adaptive smart-polling params, monitored cities, save-and-reload workflow
- **i18n** — one-click Chinese / English switch in sidebar, cookie-persisted across sessions
- **Minimal design** — borderless cards, shadow-based depth, dark/light theme (OS-aware, smooth CSS transition) with Inter typeface

---

## Technical architecture

### Data flow

```text
Holland2Stay website (Next.js + Magento)
        |
        |  Page data is loaded through Apollo GraphQL requests
        v
api.holland2stay.com/graphql/   <- Magento GraphQL backend
        |
        |  curl_cffi impersonate="chrome110" bypasses Cloudflare WAF
        v
   scraper.py  ->  models.py (Listing dataclass)
        |
        v
   storage.py (SQLite diff: compare old vs new snapshots)
        |
        +-- New listing / status change
        |        |
        |        +-- WebNotifier -> web_notifications table
        |        |     -> /api/events SSE -> browser bell + toast
        |        |
        |        +-- Loop through enabled users in users.json
        |                 |
        |                 +-- ListingFilter.passes() -> notifier.py
        |                 |     -> iMessage (macOS only) / Telegram / Email / WhatsApp
        |                 |
        |                 +-- AutoBookConfig.passes() -> booker.py  [concurrent]
        |                       -> prewarmed session (login done in parallel with notifs)
        |                          → createEmptyCart → addNewBooking
        |                          → placeOrder (store_id=54) → idealCheckOut → payment URL
        |
        +-- Read-only web queries -> web.py (Flask + custom design system)
                 -> /api/charts
                 -> /api/map    (auto-geocoding)
                 -> /api/events  (SSE stream)
                 -> /api/notifications
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `monitor.py` | Main scheduler, adaptive smart polling, hot reload, PID management, prewarmed sessions, concurrent booking |
| `scraper.py` | GraphQL scraping, `curl_cffi`, pagination, multi-city, 429 retry, proxy support |
| `storage.py` | SQLite persistence, diff detection, chart aggregation, meta storage, web_notifications table |
| `models.py` | `Listing` dataclass and formatting helpers |
| `notifier.py` | `BaseNotifier` ABC; iMessage (macOS gate), Telegram, Email, WhatsApp, `WebNotifier`, multi-dispatch |
| `booker.py` | `PrewarmedSession`, `createEmptyCart`, `addNewBooking`, `placeOrder` (store_id), `idealCheckOut` (plateform "h"); optional `cancel_enabled` auto-cancel, proxy support |
| `config.py` | Global config loading, known cities, `ListingFilter`, `AutoBookConfig` |
| `users.py` | `UserConfig`, `users.json` read/write, legacy env migration |
| `web.py` | Flask admin panel, user CRUD, session auth, charts, SSE stream, notifications API, reload endpoint, map auto-geocoding |
| `translations.py` | 120+ UI translation keys (zh/en), template `_()` helper |
| `geocode_all.py` | One-shot Nominatim geocoding to pre-warm the coordinate cache |
| `static/` | `design.css` (borderless design system), `app.js` (theme / nav / SSE / i18n-aware) |
| `templates/` | Jinja2 templates with `_()` i18n, Leaflet.js map, Chart.js stats, sidebar layout |

### Key technical decisions

| Problem | Solution | Why |
|---|---|---|
| Cloudflare 403 | `curl_cffi` + `impersonate="chrome110"` | Emulates a Chrome TLS fingerprint without launching a browser |
| No useful listing HTML | Call the GraphQL API directly | Holland2Stay uses Next.js + Apollo client-side data loading |
| Sync scraping + async notifications | `run_in_executor` bridge | Keeps `curl_cffi` scraping simple while async notifiers still work |
| Booking race condition | Submit `try_book()` to thread pool before notifications send | Booking and notification network calls run concurrently; booking reaches the server ~2–4 s sooner |
| Repeated login overhead | `PrewarmedSession`: log in once per round, reuse for all candidates | Prewarm runs in parallel with notifications; each booking saves ~0.7 s (session creation + login round-trip) |
| API rate limits | 429 backoff (30 s / 60 s retry) + 5-min cooldown + adaptive decrease | Three-layer defence: scraper retries, monitor cools down, adaptive polling stays below the threshold |
| Peak-hour probing | Adaptive interval: ×0.95 on success, ×2.0 on 429, floor at MIN_INTERVAL | Automatically discovers the maximum safe frequency without manual tuning |
| Multi-channel notifications | `BaseNotifier` + `MultiNotifier` | Shared formatting logic, per-channel send implementations |
| Platform-independent notifications | `WebNotifier` writes to SQLite; SSE pushes to browser | Works on VPS/Docker without any OS dependency |
| iMessage on non-macOS | `is_macos()` gate in `create_user_notifier()` | Logs a clear warning, skips gracefully, web notifications take over |
| Concurrent SQLite access | WAL journal mode | Monitor writes `web_notifications`; web.py reads from a separate connection safely |
| Hot reload across platforms | Signals on Unix, reload request file fallback on Windows | Settings apply without restarting the process |
| Multi-user storage | `data/users.json` | No extra dependency, simple structure, easy web-based CRUD |
| Theme switching without flicker | Inline `<head>` script + CSS custom properties | Ensures the correct theme is applied before CSS paint |
| Optional panel auth | Skip auth when `WEB_PASSWORD` is empty | Keeps local use frictionless while allowing protection when exposed |

### GraphQL API parameters

| Parameter | Value |
|---|---|
| Endpoint | `POST https://api.holland2stay.com/graphql/` |
| Category UID | `category_uid: "Nw=="` (Residences) |
| Available to book | `available_to_book: { in: ["179"] }` |
| Available in lottery | `available_to_book: { in: ["336"] }` |
| Custom fields | `custom_attributesV2` → `basic_rent`, `living_area`, `floor`, `available_startdate`, and more |

---

## Quick start

### Install

Requirements: Python 3.11+

```bash
pip install -r requirements.txt
cp .env.example .env
```

### Run locally

```bash
# 1) Test scraping only (no DB writes, no notifications)
python monitor.py --test

# 2) Start the web admin panel and add your first user
python web.py  # open http://127.0.0.1:5000

# 3) Run once to test full notification flow
python monitor.py --once

# 4) Run continuous monitoring (background example)
nohup python monitor.py > logs/monitor.log 2>&1 &
```

Tip: On first run, if `data/users.json` does not exist and old `.env` notification env vars are present, the tool auto-migrates them into a default user.

### Run with Docker (VPS / server)

Requirements: Docker + Docker Compose v2

```bash
# 1. Create .env from the template (Docker mounts this file at runtime)
cp .env.example .env
#    Edit .env to set WEB_PASSWORD and any other settings

# 2. Create the runtime directories so Docker mounts them as directories, not files
mkdir -p data logs

# 3. Build the image
docker compose build

# 4. Start in the background
docker compose up -d

# 5. Tail live logs
docker compose logs -f

# 6. Stop
docker compose down
```

The container runs `monitor.py` and `web.py` together under supervisord. Both processes log to `./logs/` on the host.

**First-time setup on a VPS:**
1. After `docker compose up -d`, open `http://<server-ip>:5000` in your browser
2. Go to **Users** and add your first user with a Telegram or Email channel (iMessage is macOS-only and is skipped automatically)
3. Go to **Settings** and choose which cities to monitor
4. Click **立即生效 / Apply now** to hot-reload the config without restarting

**Updating to a new version:**
```bash
git pull
docker compose build --no-cache
docker compose up -d
```

**Exposing via HTTPS (optional but recommended):**
Put an nginx reverse proxy in front on the VPS and terminate TLS there. A minimal nginx snippet:
```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Required for SSE (disable buffering so notifications stream in real time)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

---

## Configuration

User-level settings (notifications, filters, auto-booking) are managed in the web UI and stored in `data/users.json`.

Global settings can be changed either in the web UI or by editing `.env` directly.

```env
# Web admin
WEB_USERNAME=admin
WEB_PASSWORD=          # leave empty to disable login; set to enable session auth
FLASK_SECRET=          # auto-generated and written to .env on first run

# Scraper
CHECK_INTERVAL=300     # normal polling interval (seconds)
CITIES=Eindhoven,29    # monitored cities (use | to separate multiple)
LOG_LEVEL=INFO
TIMEZONE=Europe/Amsterdam  # IANA timezone for chart day boundaries & peak-hour clock

# Adaptive smart polling (peak hours)
PEAK_INTERVAL=60       # peak starting interval / backoff target (seconds)
MIN_INTERVAL=15        # adaptive floor — never go below this (seconds)
PEAK_START=08:30       # peak start, Amsterdam time
PEAK_END=10:00         # peak end, Amsterdam time
PEAK_WEEKDAYS_ONLY=true
JITTER_RATIO=0.20      # ±% randomisation applied to every sleep

# Proxy (optional)
HTTPS_PROXY=           # e.g. http://user:pass@host:port
HTTP_PROXY=

# DB
DB_PATH=data/listings.db
```

### Telegram Bot setup

1. Create a bot with @BotFather and keep the token
2. Send any message to your bot
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Copy the `chat.id` from the response and paste it into the user config

---

## Notification examples

New listing:

```
✅ New listing

🏠 Kastanjelaan 1-529
📌 Status: Available to book
💰 Rent: €1,680/mo
📅 Move-in: 2026-04-01

🛏 Type: 2
📐 Area: 149 m²
👤 Occupancy: Two (only couples)
🏢 Floor: 5
⚡ Energy label: A

🔗 https://www.holland2stay.com/residences/kastanjelaan-1-529.html
```

Status change (lottery → available to book):

```
🚀 Status change

🏠 Beukenlaan 89-11
📌 Available in lottery → Available to book
💰 Rent: €707/mo
📅 Move-in: 2026-04-08

🔗 https://www.holland2stay.com/residences/beukenlaan-89-11.html
```

Auto-booking success:

```
🛒 Auto-booking success!

🏠 Kastanjelaan 1-529
💰 Rent: €1,680/mo
📅 Move-in: 2026-04-01

⚡ Tap to pay now (time-limited):

https://account.holland2stay.com/idealcheckout/setup.php?order_id=...

⚠️ Direct payment link — no login required.
```

---


## File structure

```text
monitor.py          Main scheduler, adaptive smart polling, hot reload, concurrent booking
scraper.py          GraphQL scraping, curl_cffi, pagination, 429 retry, proxy support
storage.py          SQLite: listings / status_changes / web_notifications / meta / geocode_cache, chart queries
models.py           Listing dataclass and formatting helpers
notifier.py         BaseNotifier, iMessage (macOS gate), Telegram, Email, WhatsApp, WebNotifier
booker.py           Login, createEmptyCart, addNewBooking, placeOrder (store_id=54), idealCheckOut (plateform "h"), proxy support
config.py           Global config loading, known cities, ListingFilter, AutoBookConfig
users.py            UserConfig, users.json management, legacy env migration
translations.py     UI translations (zh/en) — 120+ keys covering all pages
geocode_all.py      One-shot script: pre-geocode all listing addresses via Nominatim
web.py              Flask admin panel, session auth, SSE stream, notifications API, reload endpoint, map API
static/
  design.css        Complete design system (minimal, borderless, dark/light theme)
  app.js            Frontend JS: theme toggle, mobile nav, SSE notifications, language-aware
templates/
  base.html         Sidebar layout, bell notifications (SSE + toasts), language switch
  login.html        Login page (standalone, no sidebar)
  index.html        Dashboard (KPI cards, recent listings, status changes)
  listings.html     Listing list (status filter + keyword search)
  map.html          Map view (Leaflet.js + OpenStreetMap, auto-geocoding, color-coded markers)
  calendar.html     Move-in calendar (month grid, city filter, detail panel)
  stats.html        Charts (Chart.js: trends / distribution / price buckets)
  users.html        User management list (cards with channels / filters / actions)
  user_form.html    User add/edit form (4-step: basic info, channels, filters, auto-booking)
  settings.html     Global settings (scrape config, smart polling, cities, danger zone)
Dockerfile          Single-container image (python:3.11-slim + supervisord)
supervisord.conf    Runs monitor.py + web.py together, with log rotation and auto-restart
docker-compose.yml  Volume mounts (data/, logs/, .env), port mapping, healthcheck
.dockerignore       Excludes .env, data/, logs/, __pycache__ from build context
requirements.txt    Python dependencies
.env.example        Configuration template
data/               Runtime data (auto-created)
  listings.db       SQLite database
  users.json        Per-user config (channels / filters / booking credentials)
  monitor.pid       Monitor process PID for hot reload
logs/               Log files (auto-created; supervisord writes monitor.log + web.log)
```

---

If you prefer to read the original Chinese README, it is available at [README_cn.md](README_cn.md).
