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
| Multi-channel notifications | ✅ Done | iMessage / Telegram / WhatsApp (Twilio) |
| Notification filters | ✅ Done | Per-user filters: rent, area, floor, layout, district |
| Auto-booking | ✅ Done | Adds to cart, stops before payment for manual confirmation |
| Web admin panel | ✅ Done | Dashboard, listings, users, global settings |
| Hot config reload | ✅ Done | SIGHUP-based reload, no restart required |
| Smart polling | ✅ Done | Peak hours (08:30–10:00 CET) accelerate polling to 60s |
| Multi-user support | ✅ Done | Each user has independent channels/filters/booker settings |
| Day/night theme | ✅ Done | Light/dark, follows OS preference without flicker |
| Visualization | ✅ Done | 30-day trends, city/status distribution, price histogram |
| Move-in calendar | ✅ Done | Calendar view filtered by city |
| Notification testing | ✅ Done | Per-channel test with result details |
| Optional auth for web | ✅ Done | Session login enabled when password set |

---

## Key features

- Polls Holland2Stay GraphQL API every N seconds (default 5 minutes)
- Detects new listings and status changes (e.g. lottery → available to book)
- Persists all listings to local SQLite; deduplicates notifications for the same listing
- Per-user notification channels and filters (stored in data/users.json)
- Auto-booking: logs in, checks cart, calls addNewBooking (stops before placeOrder)
- Web UI (Flask + Bootstrap) for dashboard, users, settings, charts
- Smart polling: configurable peak window and shorter interval during high-traffic times

---

## Architecture overview

Data flow:

Holland2Stay frontend (Next.js + Magento)
  └─> GraphQL endpoint: POST https://api.holland2stay.com/graphql/
       (scraper uses curl_cffi with impersonate="chrome110" to bypass Cloudflare)
  └─> scraper.py → models.py (Listing dataclass)
  └─> storage.py (SQLite diffing)
       ├─> New listing / status change → loop enabled users in data/users.json
       │     ├─> ListingFilter.passes() → notifier.py (iMessage / Telegram / WhatsApp)
       │     └─> AutoBookConfig.passes() → booker.py (login → cart → addNewBooking)
       └─> web.py (read-only queries for UI) → /api/charts

Modules (responsibilities):
- monitor.py: main scheduler, smart polling, SIGHUP hot reload, PID management
- scraper.py: GraphQL scraping using curl_cffi, pagination, multi-city
- storage.py: SQLite persistence, diff detection, chart aggregation
- models.py: Listing dataclass and helpers
- notifier.py: BaseNotifier + implementations (iMessage, Telegram, WhatsApp)
- booker.py: login / cart / addNewBooking flow (no payment)
- config.py: global config, KNOWN_CITIES, ListingFilter, AutoBookConfig
- users.py: user config dataclass and data/users.json management
- web.py: Flask admin UI and API endpoints

Key design notes:
- Cloudflare bypass: curl_cffi impersonate="chrome110" to emulate Chrome TLS fingerprint without a browser
- Single scrape feed, per-user filtering: one scrape powers notifications and bookings for all users (no Nx API calls)
- Hot reload: SIGHUP triggers an asyncio.Event to wake the scheduler and apply new settings
- Per-user channels: supports multiple channels simultaneously; MultiNotifier aggregates results

---

## Quick start

Requirements: Python 3.11+

Install:

```bash
pip install -r requirements.txt
cp .env.example .env
```

Run:

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

Tip: On first run, if `data/users.json` does not exist and old `.env` notification env vars are present, the tool will auto-migrate them into a default user.

---

## Configuration

User-level settings (notification channels, filters, auto-booking) are managed in the Web UI and stored in `data/users.json`.
Global settings can be updated in the Web UI or via `.env`.

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

## Telegram Bot setup

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

Auto-booking (added to cart):

```
🛒 Auto-booking (added to cart)

🏠 Kastanjelaan 1-529
💰 Rent: €1,680/mo
📅 Move-in: 2026-04-01

✅ Added to cart, please complete payment manually
```

---

## Roadmap (high level)

Priority items:
- Dockerize the app and remove macOS-only iMessage dependency so the service can run 24/7 on a small VPS
- Automate lottery registration via GraphQL mutations (exploratory, needs auth/rate-limit caution)
- Daily digest push, Discord webhook channel, price history tracking

---

## File structure

(See codebase root)

- monitor.py — scheduler and hot-reload
- scraper.py — GraphQL scraping (curl_cffi)
- storage.py — SQLite persistence and diffs
- models.py — Listing dataclass and helpers
- notifier.py — iMessage / Telegram / WhatsApp implementations
- booker.py — login / cart / addNewBooking flow
- config.py — known cities and filters
- users.py — data/users.json management
- web.py — Flask admin UI
- templates/ — Jinja2 templates for the web UI
- requirements.txt — Python dependencies
- data/ — runtime data (listings.db, users.json)

---

If you prefer to read the original Chinese README, it is available at [README_cn.md](README_cn.md).
