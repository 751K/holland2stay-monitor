# Holland2Stay Listing Monitor

> For the Chinese (简体中文) version, see: [README_cn.md](README_cn.md)

A personal project that monitors Holland2Stay (https://www.holland2stay.com) for new listings and status changes, pushes notifications to multiple users, and can automatically add qualifying listings to the booking cart (stops before payment).

> **Disclaimer:** This project is for personal, non-commercial use only. It is not affiliated with, endorsed by, or associated with Holland2Stay. Users are solely responsible for complying with Holland2Stay's Terms of Service and applicable laws. The author assumes no liability for any misuse or consequences arising from the use of this software.

**Live demo:** [flatradar.app](https://flatradar.app) — click "Guest mode" on the login page for read-only access.

---

## Quick start

The project supports three ways to get started: Docker (recommended for VPS/server deployment), pre-built .dmg/.exe for local use, and running from source.
Docker images bundle Caddy reverse proxy with automatic Let's Encrypt HTTPS; local runs use Flask's built-in server for personal computers.

**Docker (recommended):**
```bash
cp .env.example .env && mkdir -p data logs logs/caddy
# Edit Caddyfile: replace "your.domain.com" with your actual domain
docker compose up -d
# open https://your.domain.com → Dashboard → "Start monitor"
```

**macOS:**
Download the latest `.dmg` from [Releases](../../releases), drag to Applications, and double-click to start. The browser opens automatically. Persistent data is stored in `~/.h2s-monitor/`.

**Windows:**
Download the latest `.zip` from [Releases](../../releases), extract and double-click `h2s-monitor.exe`. A CMD window opens and the browser launches automatically. Persistent data is stored in `%USERPROFILE%\.h2s-monitor\`.

**Run from source:**
```bash
pip install -r requirements.txt
cp .env.example .env
python web.py
```

**Run tests:**
```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

[Full installation guide →](#run-locally)

---

## Project status

| Component | Status | Notes |
|---|---:|---|
| Data scraping | ✅ Done | Uses GraphQL + curl_cffi to bypass Cloudflare WAF |
| Multi-city monitoring | ✅ Done | 26 Dutch cities; select cities in the web UI |
| Multi-channel notifications | ✅ Done | iMessage / Telegram / Email / WhatsApp (Twilio) |
| Web panel notifications | ✅ Done | Real-time bell + toasts via SSE, works on any platform |
| Notification filters | ✅ Done | Per-user: rent, area, floor, type, occupancy, city, neighborhood, contract, tenant, promo, finishing, energy |
| Multi-select filter UI | ✅ Done | Dropdown checkboxes with i18n labels; listing filters for city, tenant, contract |
| Short-stay detection | ✅ Done | Contract / Tenant / Offer tags extracted from GraphQL; per-user filters |
| Cross-platform builds | ✅ Done | GitHub Actions builds macOS .dmg + Windows .exe on tag push |
| Geocoding (Photon) | ✅ Done | Fast map geocoding via Komoot Photon API; manual trigger button |
| Auto-booking | ✅ Done | Full flow: add to cart → place order → direct payment URL |
| Fast-path booking | ✅ Done | Reserved → Available booking submitted before notifications send |
| Web admin panel | ✅ Done | Dashboard, listings, users, global settings |
| Hot config reload | ✅ Done | Cross-platform reload, no restart required |
| Smart polling | ✅ Done | Dual peak windows (AM + PM), auto-accelerate; adaptive interval probes rate limit |
| Rate limit protection | ✅ Done | 429 exponential backoff + 5-minute cooldown + proxy support |
| Cloudflare block detection | ✅ Done | 403 WAF detection, throttled alert, 15-min cooldown, actionable recovery steps |
| Multi-user support | ✅ Done | Each user has independent channels / filters / booker settings |
| VPS / Docker ready | ✅ Done | iMessage gracefully skipped on non-macOS; web panel takes over |
| Day/night theme | ✅ Done | Light/dark, follows OS preference without flicker |
| Mobile web optimization | ✅ Done | Adaptive views: card layouts, 44px touch targets, safe-area insets, dvh units, list/calendar toggle, responsive grids |
| Visualization | ✅ Done | 10 charts: trends, city/status/price/area/floor/type/energy/tenant/contract distributions, 24h drop time |
| Move-in calendar | ✅ Done | Calendar view filtered by city |
| Map view | ✅ Done | Leaflet.js + OpenStreetMap with auto-geocoding |
| i18n (中/EN) | ✅ Done | One-click language switch, cookie-persisted |
| Notification testing | ✅ Done | Per-channel test with result details |
| Guest mode (RBAC) | ✅ Done | Password-free read-only access; admin role required for settings/users/logs |
| Optional auth for web | ✅ Done | Session login enabled when password set; `WEB_GUEST_MODE` controls guest entry |
| Login rate limiting | ✅ Done | IP-based exponential backoff after 5 failures |
| HTTPS / Caddy | ✅ Done | Bundled Caddyfile + docker-compose Caddy service; auto Let's Encrypt |
| Security hardening | ✅ Done | RBAC decorators, notifications/SSE/geocode blocked for guests, CSRF, open-redirect fix, DOM XSS prevention (map geocode errors, settings numeric validation) |
| Startup preflight | ✅ Done | Blocks container start if `WEB_PASSWORD` unset or Caddyfile domain is still a placeholder |
| Production WSGI | ✅ Done | Gunicorn (1 worker × 8 threads, timeout=0) replaces Flask dev server in Docker |
| Dependency pinning | ✅ Done | `requirements.lock` with exact `==` versions; Dockerfile installs from lock file |
| Code modularization | ✅ Done | web.py split into `app/` (10 route + 8 shared modules); `mcore/` (interval, prewarm, booking); `mstorage/` (6 mixin modules); `monitor.py` 1,235→971, `storage.py` 1,177→17 re-export |
| Prewarm session cache | ✅ Done | `mcore/prewarm.py` PrewarmCache class; process-level cache with TTL refresh; invalidated on user/config change |
| Error log (errors.log) | ✅ Done | Separate WARNING+ log with `funcName:lineno` format; web.log for Flask app; log viewer with file tabs, line numbers, level coloring, keyword search, auto-scroll |
| Pytest test suite | ✅ Done | 30 test modules (561 tests) covering full stack: models, mcore, mstorage, storage, scraper, booker, notifier, auth, CSRF, routes, i18n |
| Code quality | ✅ Done | Literal types, shared constants, dedup parse logic, mixin composition for Storage |

---

## Core features

### Data scraping

- Polls the Holland2Stay GraphQL API every N seconds (default: 5 minutes)
- Supports multi-city monitoring; cities can be selected in the web UI
- Detects both new listings and status changes, such as lottery → available to book
- Stores all listings in local SQLite so history remains queryable and duplicate notifications are avoided

### Smart adaptive polling

Normal intervals apply outside peak hours. During the Dutch morning release window (default 08:30–10:00 CET, weekdays) and afternoon window (default 13:30–15:00 CET, weekdays), adaptive polling kicks in:

- Starts each peak session at `PEAK_INTERVAL` (default 60 s)
- After every successful scrape round, shrinks the interval by 5%, automatically probing how fast the API will tolerate
- Floors at `MIN_INTERVAL` (default 15 s, configurable) — never pushes below this
- On a 429 rate-limit response, doubles the current interval and holds a 5-minute cooldown before retrying
- Resets to `PEAK_INTERVAL` at the end of each peak window, ready to probe again tomorrow
- Randomised ±`JITTER_RATIO` % jitter on every sleep to avoid mechanical fingerprinting
- All parameters (PEAK_INTERVAL, MIN_INTERVAL, PEAK_START, PEAK_END, PEAK_START_2, PEAK_END_2, JITTER_RATIO, PEAK_WEEKDAYS_ONLY) configurable in the web UI

### Rate limit & block protection

**429 (rate limit)** — temporary, auto-recovers:

- `scraper.py` retries a 429 response twice (waits 30 s then 60 s) before giving up
- On persistent rate-limiting, `monitor.py` raises a `RateLimitError`, notifies all users, and sleeps 5 minutes before resuming

**403 (Cloudflare WAF block)** — permanent until you act:

- `scraper.py` detects Cloudflare challenge pages (HTML signatures like `no-js ie6 oldie`) and immediately raises `BlockedError` — no retry, unlike 429
- `monitor.py` catches `BlockedError`, notifies users (throttled to 1 alert per 30 min), and sleeps 15 minutes
- Error message includes actionable recovery steps: switch proxy IP, restart monitor (new TLS fingerprint), or pause for a few hours

**Proxy support:** set `HTTPS_PROXY` or `HTTP_PROXY` in `.env` to route all scraping and booking traffic through a proxy; picked up at runtime so a hot reload applies the change without restart

### Fast-path booking

For any qualifying "Available to book" listing — whether it just appeared for the first time or transitioned from another status — the window to claim it is measured in seconds. The monitor:

1. Pre-scans the diff result in memory (no network calls)
2. Immediately submits `try_book()` to a thread pool for **all** auto-book candidates, before any notification is sent
3. Runs the booking HTTP flow concurrently while notification sends are in flight
4. Awaits the booking result after notifications finish — in most cases the booking is already done

This reduces the delay between detecting availability and reaching the server to approximately 0–1 second instead of the former 2–5 second notification-first approach.

### Multi-user support

- Each user has independent channels, credentials, filters, and auto-book settings
- One scrape run is shared across all users — adding users does not multiply API traffic
- User data is stored in `data/users.json` and can be managed entirely from the web UI
- On first run, open the web panel and click "Add User" to create your first user

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
- **Stats** — Chart.js trends (new listings, status changes), doughnut distributions (city, status), price histogram (9 buckets up to >€1600), 24h listing drop time chart, 7/30/90-day range selector
- **Users** — CRUD, enable/disable, per-user notification channels & filters & auto-booking config, one-click per-channel test
- **Global Settings** — polling intervals, adaptive smart-polling params (dual windows), heartbeat interval, monitored cities, save-and-reload workflow
- **Guest mode** — login page "Guest mode" button lets anyone view the panel read-only without a password; set `WEB_GUEST_MODE=false` to disable; admin routes (Users / Settings / System / Logs) remain fully restricted
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
| `monitor.py` | Main scheduler, adaptive smart polling (dual peak windows), hot reload, PID management, prewarmed session cache (Phase B cross-round reuse), concurrent booking, time-based heartbeat, dual logging (monitor.log + errors.log) |
| `scraper.py` | GraphQL scraping, `curl_cffi`, pagination, multi-city, 429 retry with cumulative wait, proxy support, enhanced error context logging |
| `storage.py` | SQLite persistence, diff detection, chart aggregation, meta storage, web_notifications table, `get_distinct_cities()` |
| `models.py` | `Listing` dataclass and formatting helpers |
| `notifier.py` | `BaseNotifier` ABC; iMessage (macOS gate, AppleScript escape hardened), Telegram, Email, WhatsApp, `WebNotifier`, multi-dispatch |
| `booker.py` | `PrewarmedSession`, `createEmptyCart`, `addNewBooking`, `placeOrder` (store_id), `idealCheckOut` (plateform "h"); enhanced error context (sku/contract_id/start_date); optional `cancel_enabled` auto-cancel, proxy support |
| `config.py` | Global config loading, known cities, `ListingFilter`, `AutoBookConfig` |
| `users.py` | `UserConfig`, `users.json` read/write |
| `web.py` | Flask app bootstrap: instantiation, security headers, CSRF, Jinja filters, context processors, route registration, web process file logging |
| `app/auth.py` | Session authentication, role decorators (`login_required`, `admin_required`, `admin_api_required`), guest mode, login rate limiting |
| `app/csrf.py` | CSRF token generation and validation (Unicode-safe via `.encode("utf-8")`) |
| `app/db.py` | Database helper: `get_db()` connection factory |
| `app/env_writer.py` | `.env` file in-place key writer (avoids `dotenv.set_key()` atomic-rename issues on Docker bind mounts) |
| `app/forms/user_form.py` | User form data extraction and `UserConfig` construction |
| `app/i18n.py` | Language detection, cookie persistence, option localisation |
| `app/jinja_filters.py` | Jinja2 custom filters registered on the Flask app |
| `app/process_ctrl.py` | Monitor process lifecycle: start / stop / reload / PID management |
| `app/safety.py` | Security response helpers |
| `app/routes/dashboard.py` | Dashboard: index, charts API, listing search; `get_distinct_cities()` for correct city list |
| `app/routes/calendar_routes.py` | Move-in calendar view and data API |
| `app/routes/map_routes.py` | Map view, geocode cache API, neighbourhood API |
| `app/routes/notifications.py` | Notification list, mark-read, SSE event stream |
| `app/routes/control.py` | Monitor control: start / stop / shutdown / reload endpoints |
| `app/routes/sessions.py` | Login / logout / guest entry |
| `app/routes/settings.py` | Global settings: view, save, filter options API |
| `app/routes/stats.py` | Statistics dashboard with Chart.js data API |
| `app/routes/system.py` | System info, log viewer (file tabs: monitor/errors/web, line numbers, level coloring, keyword search), log clear, health endpoint, log files list API |
| `app/routes/users.py` | User CRUD, toggle enable/disable, notification test |
| `translations.py` | 120+ UI translation keys (zh/en), template `_()` helper |
| `tools/geocode_all.py` | One-shot Nominatim geocoding to pre-warm the coordinate cache |
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
| Cloudflare 403 WAF block | Immediate `BlockedError` raise (no retry) + Cloudflare challenge detection + 15-min cooldown + throttled alert (max 1/30 min) | 403 is permanent — waiting won't help; actionable recovery steps included in error + notification |
| Peak-hour probing | Adaptive interval: ×0.95 on success, ×2.0 on 429, floor at MIN_INTERVAL | Automatically discovers the maximum safe frequency without manual tuning |
| Multi-channel notifications | `BaseNotifier` + `MultiNotifier` | Shared formatting logic, per-channel send implementations |
| Platform-independent notifications | `WebNotifier` writes to SQLite; SSE pushes to browser | Works on VPS/Docker without any OS dependency |
| iMessage on non-macOS | `is_macos()` gate in `create_user_notifier()` | Logs a clear warning, skips gracefully, web notifications take over |
| Concurrent SQLite access | WAL journal mode | Monitor writes `web_notifications`; web.py reads from a separate connection safely |
| Hot reload across platforms | Signals on Unix, reload request file fallback on Windows | Settings apply without restarting the process |
| Multi-user storage | `data/users.json` | No extra dependency, simple structure, easy web-based CRUD |
| Theme switching without flicker | Inline `<head>` script + CSS custom properties | Ensures the correct theme is applied before CSS paint |
| Optional panel auth | Skip auth when `WEB_PASSWORD` is empty | Keeps local use frictionless while allowing protection when exposed |
| Monolithic web.py (1,200+ lines) | Split into `app/routes/` (10 route modules) + `app/` (auth, csrf, db, i18n, etc.) | Each module ~15–240 lines with single responsibility; `web.py` is now a 154-line bootstrap; routes use `add_url_rule` to keep flat endpoint names so templates need zero changes |
| Prewarm session per-round waste | Process-level cache with TTL-aware refresh; cross-round reuse | Hit: zero network IO; TTL < 300 s: background refresh parallel to scrape; only invalidated on email change / unknown_error |
| INFO noise drowning warnings | Separate `errors.log` (WARNING+) with `funcName:lineno` formatter, backupCount=5 | `monitor.log` stays INFO+ for operational view; `errors.log` captures sparse but actionable anomalies with precise source location |
| No automated tests | 10 pytest modules with shared fixtures (`temp_db`, `client`, `admin_client`, etc.) | Pure-function tests for models/crypto/safety/storage; HTTP integration tests for auth/user/log routes; zero external network dependency |

### GraphQL API parameters

| Parameter | Value |
|---|---|
| Endpoint | `POST https://api.holland2stay.com/graphql/` |
| Category UID | `category_uid: "Nw=="` (Residences) |
| Available to book | `available_to_book: { in: ["179"] }` |
| Available in lottery | `available_to_book: { in: ["336"] }` |
| Custom fields | `custom_attributesV2` → `price` (total rent incl. service costs), `living_area`, `floor`, `available_startdate`, and more |

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

# 2) Start the web panel — the only command you need
python web.py  # open http://127.0.0.1:8088
#    Go to Dashboard and click "Start monitor" to begin monitoring.
#    You can also start/stop the monitor from the web UI at any time.

# 3) Or run monitor standalone (one-shot, CLI)
python monitor.py --once
```

The web panel dashboard includes **Start / Stop / Shutdown** buttons — no need to SSH in or manage background processes manually.

Tip: On first run, open the web panel and click "Add User" in the Users page to create your first user with notification channels and filters.

### Run with Docker (VPS / server)

Requirements: Docker + Docker Compose v2

The included `docker-compose.yml` runs **Caddy + h2s** together. Caddy handles HTTPS (Let's Encrypt) and is the only external entry point — port 8088 is internal to the Docker network and is **not** mapped to the host.

**Before starting — two required steps:**

1. **Edit `Caddyfile`**: replace `your.domain.com` with your actual domain:
   ```
   your.domain.com {
       reverse_proxy h2s:8088
       ...
   }
   ```

2. **Edit `.env`**: set a password and enable secure cookies:
   ```env
   WEB_PASSWORD=yourpassword
   SESSION_COOKIE_SECURE=true
   ```

Also point your domain's DNS A record to the VPS IP, and make sure ports 80 and 443 are open (needed for the ACME challenge).

**Start:**
```bash
cp .env.example .env   # then edit as above
mkdir -p data logs
docker compose up -d

# Tail logs
docker compose logs -f

# Stop
docker compose down
```

**Using a proxy with Docker:**

If you need to route scraping and booking traffic through a proxy (e.g. residential proxy to avoid Cloudflare 403 blocks), you must pass the proxy variables into the container in **two places**:

1. **`.env`** — set the proxy URL so the app can read it at runtime:
   ```env
   HTTPS_PROXY=http://user:pass@proxy-host:port
   # or HTTP_PROXY if your proxy uses HTTP
   ```

2. **`docker-compose.yml`** — forward the variable from the host into the container by adding under `services.h2s.environment`:
   ```yaml
   environment:
     - TZ=Europe/Amsterdam
     - PYTHONUNBUFFERED=1
     - HTTP_PROXY=${HTTP_PROXY}
     - HTTPS_PROXY=${HTTPS_PROXY}
     - ALL_PROXY=${ALL_PROXY}
   ```

   The `${VAR}` syntax pulls from your host shell or a `.env` file in the same directory (docker compose reads `.env` automatically). After editing, run `docker compose up -d` to recreate the container with the new variables.

The container runs `monitor.py` and `web.py` together under supervisord. Logs go to `./logs/` on the host. The container runs as non-root user `appuser`. `mem_limit: 512M` and `cpus: 1.0` cap resource usage.

**First-time setup:**
1. Open `https://your.domain.com` and log in
2. Go to **Users** → add your first user with a Telegram or Email channel (iMessage is macOS-only and skipped automatically on Linux/Docker)
3. Go to **Settings** → choose which cities to monitor
4. Click **立即生效 / Apply now** to hot-reload without restarting

**Updating to a new version:**
```bash
git pull
docker compose up -d --build
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
PEAK_START=08:30       # peak window ① start, Amsterdam time
PEAK_END=10:00         # peak window ① end, Amsterdam time
PEAK_START_2=13:30     # peak window ② start, Amsterdam time
PEAK_END_2=15:00       # peak window ② end, Amsterdam time
PEAK_WEEKDAYS_ONLY=true
JITTER_RATIO=0.20      # ±% randomisation applied to every sleep

# Monitoring heartbeat (periodic summary confirming the monitor is alive)
HEARTBEAT_INTERVAL_MINUTES=60   # minutes between heartbeats; set 0 to disable

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
monitor.py          Main scheduler, adaptive smart polling (dual windows), hot reload, prewarm cache (Phase B), time-based heartbeat, dual logging
scraper.py          GraphQL scraping, curl_cffi, pagination, 429 retry with cumulative wait, proxy support
storage.py          SQLite: listings / status_changes / web_notifications / meta / geocode_cache, chart queries, get_distinct_cities()
models.py           Listing dataclass and formatting helpers
notifier.py         BaseNotifier, iMessage (AppleScript escape hardened), Telegram, Email, WhatsApp, WebNotifier
booker.py           Login, createEmptyCart, addNewBooking, placeOrder (store_id=54), idealCheckOut (plateform "h"), proxy support; enhanced error context
config.py           Global config loading, known cities, ListingFilter, AutoBookConfig, heartbeat interval
users.py            UserConfig, users.json read/write
translations.py     UI translations (zh/en) — 120+ keys covering all pages
tools/
  geocode_all.py      One-shot script: pre-geocode all listing addresses via Nominatim
  reset_db.py         One-shot script: reset the database for testing
web.py              Flask app bootstrap — security headers, CSRF, i18n, route registration
app/
  __init__.py       Package init
  auth.py           Session auth, RBAC decorators, guest mode, login rate limiting
  csrf.py           CSRF token generation and validation
  db.py             Database connection factory
  env_writer.py     .env file in-place key writer
  i18n.py           Language detection and cookie persistence
  jinja_filters.py  Custom Jinja2 filters
  process_ctrl.py   Monitor process lifecycle (start/stop/reload)
  safety.py         Security response helpers
  forms/
    user_form.py    User form data extraction
  routes/
    __init__.py     Route registration coordinator
    dashboard.py    Dashboard, charts API, listing search
    calendar_routes.py  Calendar view and data
    map_routes.py   Map, geocode cache, neighbourhoods
    notifications.py    Notification list, mark-read, SSE stream
    control.py      Monitor start/stop/shutdown/reload
    sessions.py     Login, logout, guest entry
    settings.py     Global settings view/save, filter options
    stats.py        Chart.js data API
    system.py       System info, log viewer (tabs, line numbers, level coloring, search), health
    users.py        User CRUD, enable/disable, notification test
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
pytest.ini          Pytest configuration (strict markers, deprecation filters)
requirements-dev.txt Pytest dev dependency (not needed in Docker)
tests/
  conftest.py       Shared fixtures: temp_db, app_ctx, fresh_crypto, test_app, client, admin_client, guest_client
  test_applescript_escape.py   AppleScript escape hardening
  test_auth_routes.py          Authentication routes (login/logout/guest/session)
  test_crypto.py               Encryption / decryption round-trip
  test_log_routes.py           Log viewer API (file whitelist, clear, path traversal)
  test_models_filter.py        ListingFilter logic (pass/reject edge cases)
  test_prewarm_cache.py        Prewarm session cache lifecycle
  test_safety.py               safe_next_url / security redirect helpers
  test_storage_diff.py         SQLite diff detection (new/change/stale)
  test_user_form.py            User form data extraction
  test_user_routes.py          User CRUD routes (RBAC enforcement)
Dockerfile          Single-container image (python:3.11-slim + supervisord)
docker-compose.yml  Volume mounts (data/, logs/, .env), port mapping, healthcheck
.dockerignore       Excludes .env, data/, logs/, __pycache__ from build context
docker/
  supervisord.conf    Runs monitor.py + web.py together, with log rotation and auto-restart
  entrypoint.sh       Docker entrypoint (creates .env and directories on first run)
requirements.txt    Python dependencies
.env.example        Configuration template
packaging/
  asset/              App icon source (1024x1024 PNG)
  build_dmg.sh        macOS .dmg build script (PyInstaller + .app bundle + icon)
  build.bat           Windows build script (PyInstaller + ZIP)
  h2s_monitor.spec    PyInstaller spec file
launcher.py         macOS .app entry point (imports web.app, handles --run-monitor)
.github/workflows/  GitHub Actions CI/CD (builds .dmg + .exe on tag/manual trigger)
data/               Runtime data (auto-created)
  listings.db       SQLite database
  users.json        Per-user config (channels / filters / booking credentials)
  monitor.pid       Monitor process PID for hot reload
logs/               Log files (auto-created; supervisord writes monitor.log + web.log)
```

---

## License

Holland2Stay Monitor is licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).

**Permitted:**
- Personal use
- Educational use
- Research use
- Non-commercial modification and redistribution

**Not permitted without prior written permission:**
- Commercial use
- Use by a company or for-profit organization
- Selling, sublicensing, hosting as a paid service, or integrating this project into a commercial product or workflow

See the [LICENSE](../LICENSE) file for the full terms.

---

If you prefer to read the original Chinese README, it is available at [README_cn.md](README_cn.md).
