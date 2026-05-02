# Holland2Stay Listing Monitor

> For the Chinese (简体中文) version, see: [README_cn.md](README_cn.md)

A personal project that monitors Holland2Stay (https://www.holland2stay.com) for new listings and status changes, pushes notifications to multiple users, and can automatically add qualifying listings to the booking cart (stops before payment).

Note: Personal project — not for commercial use. Contributions, issues and PRs are welcome.

---

## Project status

| Component | Status | Notes |
|---|---:|---|
| Data scraping | ✅ Done | Uses GraphQL + curl_cffi to bypass Cloudflare WAF |
| Multi-city monitoring | ✅ Done | Supports 26 Dutch cities; select cities in the web UI |
| Multi-channel notifications | ✅ Done | iMessage / Telegram / Email / WhatsApp (Twilio) |
| Notification filters | ✅ Done | Per-user filters: rent, area, floor, layout, district |
| Auto-booking | ✅ Done | Full flow: add to cart → place order → generate direct payment URL, push via notification |
| Web admin panel | ✅ Done | Dashboard, listings, users, global settings |
| Hot config reload | ✅ Done | Cross-platform reload, no restart required |
| Smart polling | ✅ Done | Peak hours (08:30–10:00 CET) accelerate polling to 60s |
| Multi-user support | ✅ Done | Each user has independent channels/filters/booker settings |
| Day/night theme | ✅ Done | Light/dark, follows OS preference without flicker |
| Visualization | ✅ Done | 30-day trends, city/status distribution, price histogram |
| Move-in calendar | ✅ Done | Calendar view filtered by city |
| Notification testing | ✅ Done | Per-channel test with result details |
| Optional auth for web | ✅ Done | Session login enabled when password set |

---

## Core features

### Data scraping

- Polls the Holland2Stay GraphQL API every N seconds (default: 5 minutes)
- Supports multi-city monitoring; cities can be selected in the web UI
- Detects both new listings and status changes, such as lottery → available to book
- Stores all listings in local SQLite so history remains queryable and duplicate notifications are avoided

### Smart polling

- Speeds up polling during the Dutch morning release window (default: 08:30-10:00 CET)
- Falls back to the normal interval outside the peak window to balance freshness and resource usage
- Peak interval, start/end time, and weekday-only behavior can all be configured in the web UI

### Multi-user support

- Each user has independent channels, credentials, filters, and auto-book settings
- One scrape run is shared across all users, so adding users does not multiply API traffic
- User data is stored in `data/users.json` and can be managed entirely from the web UI
- On first run, legacy notification env vars can be migrated into a default user automatically

### Notifications

- Supports iMessage, Telegram Bot, SMTP email, and WhatsApp via Twilio
- Each user can enable one or more channels at the same time
- Notification content includes status, rent, area, floor, energy label, move-in date, and listing link
- Per-user filters allow users to receive only listings that match their own criteria
- The web UI provides one-click per-channel notification testing with success or failure details

### Auto-booking

- When a qualifying "Available to book" listing appears, the monitor can complete the booking workflow automatically
- Flow: login → cancel pending orders → `addNewBooking` → `placeOrder` → `idealCheckOut`
- Sends a direct payment URL to the user so payment can be completed without logging in again
- Supports stricter booking filters than notification filters, plus a dry-run mode for validation

### Web admin panel

- Dashboard with totals, today's new listings, recent changes, and latest scrape info
- Listings page with status filters and keyword search
- Calendar and chart views for move-in dates, city distribution, status distribution, and price ranges
- User management with CRUD, enable/disable, per-user config, and test notifications
- Global settings for polling, smart polling, and monitored cities without editing `.env`
- Save-and-reload workflow so monitor settings can be applied without restarting the process

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
   storage.py (SQLite diffing between old and new snapshots)
        |
        +-- New listing / status change
        |        |
        |        +-- Loop through enabled users in users.json
        |                 |
        |                 +-- ListingFilter.passes() -> notifier.py
        |                 |     -> iMessage / Telegram / Email / WhatsApp
        |                 |
        |                 +-- AutoBookConfig.passes() -> booker.py
        |                       -> login -> cancel pending orders -> addNewBooking
        |                          -> placeOrder -> idealCheckOut -> payment URL
        |
        +-- Read-only web queries -> web.py (Flask + Bootstrap)
                 -> /api/charts
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `monitor.py` | Main scheduler, smart polling, hot reload, PID management |
| `scraper.py` | GraphQL scraping, `curl_cffi`, pagination, multi-city fetching |
| `storage.py` | SQLite persistence, diff detection, chart aggregation, meta storage |
| `models.py` | `Listing` dataclass and formatting helpers |
| `notifier.py` | Base notifier abstractions plus iMessage, Telegram, Email, WhatsApp, and multi-channel dispatch |
| `booker.py` | Login, pending-order cleanup, `addNewBooking`, `placeOrder`, `idealCheckOut`, payment URL generation |
| `config.py` | Global config loading, known cities, `ListingFilter`, `AutoBookConfig` |
| `users.py` | `UserConfig`, `users.json` read/write, legacy env migration |
| `web.py` | Flask admin panel, user CRUD, session auth, charts, reload endpoints |
| `templates/` | Bootstrap UI, theme switching, Chart.js views, calendar view |

### Key technical decisions

| Problem | Solution | Why |
|---|---|---|
| Cloudflare 403 | `curl_cffi` + `impersonate="chrome110"` | Emulates a Chrome TLS fingerprint without launching a browser |
| No useful listing HTML in page source | Call the GraphQL API directly | Holland2Stay uses Next.js + Apollo client-side data loading |
| Sync scraping with async notifications | `run_in_executor` bridge | Keeps `curl_cffi` scraping simple while async notifiers still work |
| Multi-channel notifications | `BaseNotifier` + `MultiNotifier` | Shared formatting logic, per-channel send implementations |
| Hot reload across platforms | Signals on Unix, reload request file fallback on Windows | Lets monitor settings be applied without restarting the process |
| Multi-user storage | `data/users.json` | No extra dependency, simple structure, easy web-based CRUD |
| Theme switching without flicker | Inline `<head>` script + CSS custom properties | Ensures the correct theme is applied before CSS paint |
| Optional panel auth | Skip auth when `WEB_PASSWORD` is empty | Keeps local use frictionless while still allowing protection when exposed |

### GraphQL API parameters

| Parameter | Value |
|---|---|
| Endpoint | `POST https://api.holland2stay.com/graphql/` |
| Category UID | `category_uid: "Nw=="` (Residences) |
| Available to book | `available_to_book: { in: ["179"] }` |
| Available in lottery | `available_to_book: { in: ["336"] }` |
| Custom fields | `custom_attributesV2` -> `basic_rent`, `living_area`, `floor`, `available_startdate`, and more |

---

## Quick start

### Install

Requirements: Python 3.11+

```bash
pip install -r requirements.txt
cp .env.example .env
```

### Run

```bash
# 1) Test scraping only (no DB writes, no notifications)
python monitor.py --test

# 2) Start the web admin panel and add your first user
python web.py  # open http://127.0.0.1:5000

# 3) Run once to test full notification flow
python monitor.py --once

# 4) Run continuous monitoring (background example)
python monitor.py
nohup python monitor.py > logs/monitor.log 2>&1 &
```

Tip: On first run, if `data/users.json` does not exist and old `.env` notification env vars are present, the tool can auto-migrate them into a default user.

---

## Configuration

User-level settings for notifications, filters, and auto-booking are managed in the web UI and stored in `data/users.json`.

Global settings can be changed either in the web UI or by editing `.env`.

Important envs (see `.env.example`):

```env
# Web admin
WEB_USERNAME=admin
WEB_PASSWORD=          # leave empty to disable login; set to enable session auth
FLASK_SECRET=

# Scraper
CHECK_INTERVAL=300     # normal polling interval (seconds)
CITIES=Eindhoven,29    # monitored cities
LOG_LEVEL=INFO

# Smart polling (peak hours)
PEAK_INTERVAL=60
PEAK_START=08:30
PEAK_END=10:00
PEAK_WEEKDAYS_ONLY=true

# DB
DB_PATH=data/listings.db
```

### Telegram Bot setup

1. Create a bot with @BotFather and keep the token
2. Send any message to your bot
3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates
4. Copy the chat id from the response and paste it to the user config

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

## Roadmap

### High priority

- Dockerize the app and remove the macOS-only iMessage dependency so the service can run 24/7 on a small VPS

### Medium priority

- Automate lottery registration through GraphQL mutations, with extra care around auth and rate limits
- Add daily digest notifications instead of only real-time pushes
- Add a Discord webhook notification channel
- Track price history for the same listing and alert on drops

## File structure

```text
monitor.py          Main scheduler, smart polling, hot reload, PID management
scraper.py          GraphQL scraping with curl_cffi, pagination, multi-city fetching
storage.py          SQLite listings, status changes, chart aggregation, meta storage
models.py           Listing dataclass and formatting helpers
notifier.py         BaseNotifier plus iMessage, Telegram, Email, WhatsApp, and multi-dispatch
booker.py           Login, cart flow, addNewBooking, placeOrder, idealCheckOut
config.py           Global config loading, known cities, filters
users.py            UserConfig, users.json management, legacy env migration
web.py              Flask admin panel, session auth, user CRUD, charts, reload endpoints
templates/          Jinja2 templates for the web UI
requirements.txt    Python dependencies
data/               Runtime data such as listings.db and users.json
```

---

If you prefer to read the original Chinese README, it is available at [README_cn.md](README_cn.md).
