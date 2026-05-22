# FlatRadar

> For the Chinese (简体中文) version, see: [README_cn.md](README_cn.md)

FlatRadar is a personal rental listing monitor. It tracks new listings and status changes across multiple platforms, pushes notifications to multiple users, and can automatically add qualifying listings to the booking cart (stops before payment). Currently supports **Holland2Stay** (GraphQL API), **OurDomain** (RENTCafe HTML), and **Xior** (WordPress AJAX JSON).

> **Disclaimer:** This project is for personal, non-commercial use only. It is not affiliated with, endorsed by, or associated with Holland2Stay. Users are solely responsible for complying with Holland2Stay's Terms of Service and applicable laws. The author assumes no liability for any misuse or consequences arising from the use of this software.

**Current version:** v1.7.0

**Live demo:** [flatradar.app](https://flatradar.app) — register an account or click "Guest mode" for read-only access.  
**User guide:** [flatradar.app/guide](https://flatradar.app/guide) — full walkthrough with screenshots.

If you find this project useful, please consider **starring the repo** ⭐ — it helps others discover FlatRadar.

---

## Sponsor / Support

FlatRadar is a solo-driven open source project. Server costs, API proxies, and push notification infrastructure are paid out of pocket. Every sponsorship directly keeps the server running and the iOS app on the App Store.

- **[GitHub Sponsors](https://github.com/sponsors/751K)** — monthly or one-time
- **[Buy Me a Coffee](https://flatradar.app/donate)** — Alipay / WeChat QR

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
Download the latest `.dmg` from [Releases](../../releases), drag to Applications, and double-click to start. The browser opens automatically. Persistent data is stored in `~/.flatradar/`.

**Windows:**
Download the latest `.zip` from [Releases](../../releases), extract and double-click `flatradar.exe`. A CMD window opens and the browser launches automatically. Persistent data is stored in `%USERPROFILE%\.flatradar\`.

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

## iOS App (FlatRadar)

A native iOS companion app built with SwiftUI + StoreKit 2. Available as an Xcode project under `ios/FlatRadar/`.

[![Download on the App Store](https://developer.apple.com/assets/elements/badges/download-on-the-app-store.svg)](https://apps.apple.com/us/app/flarradar/id6769857080)

### Quick start (iOS)

```bash
cd ios/FlatRadar
open FlatRadar.xcodeproj
# Select "FlatRadar" scheme → Run on simulator or device
```

The app connects to `flatradar.app` by default. To use a local server, change the URL in Settings (developer builds only).

### Architecture

```
FlatRadar/
├── Models/          # Decodable models (Listing, AuthModels, ChartData, etc.)
├── Networking/      # APIClient, SSE Client, KeychainManager, APIError
├── Stores/          # @Observable state objects (Auth, Listings, Dashboard, etc.)
├── Views/
│   ├── Auth/        # LoginView, LoginModePicker
│   ├── Dashboard/   # DashboardView, ChartDetailView
│   ├── Listings/    # ListingsView, ListingRow, ListingDetailView
│   ├── Map/         # MapView, MapClustering
│   ├── Calendar/    # CalendarView
│   ├── Notifications/ # NotificationsView, NotificationRow
│   ├── Settings/    # SettingsView, FilterEditView
│   ├── Admin/       # AdminUsersView, AdminMonitorView
│   └── Browse/      # BrowseView
├── Navigation/      # NavigationCoordinator + deep link routing
├── Push/            # PushDelegate (APNs bridge)
└── FlatRadarApp.swift  # @main entry, environment injection
```

### Key features

- **3 role modes**: Admin (monitor control), User (saved filters + push), Guest (read-only browsing)
- **Dashboard**: Live stats card with sparkline, 2×2 Explore grid with inline mini-charts, matched listing previews
- **Listings**: Paginated browsing, multi-filter (city/status/type/contract/energy), 6 sort modes, detail view
- **Map**: Native MapKit with custom grid clustering, color-coded pins, tap-to-zoom
- **Calendar**: Monthly move-in calendar with per-day availability counts
- **Notifications**: Card inbox with day-grouping, SSE real-time stream, APNs push
- **Self-service**: Registration, filter editing, account deletion
- **Donation**: "Buy me a coffee" StoreKit 2 IAP (3 tiers, no feature gating)
- **Legal**: Mandatory first-launch Terms agreement, in-app Terms & Privacy sheets
- **i18n**: English / 简体中文 (174 localized strings)
- **Dark mode**: Adaptive colors throughout
- **Design system**: Primary #0A84FF, semantic colors (green/orange/red), tabular-nums on all numbers

### API endpoints used

Full mobile API contract: [Backend API Reference](API.md). Machine-readable OpenAPI 3.1 contract for iOS/Android clients: [openapi.json](openapi.json).

All endpoints under `/api/v1/*` with JWT Bearer auth (or bearer_optional for public stats):

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/auth/login` | POST | None | Login (admin/user via bcrypt or H2S credentials) |
| `/auth/register` | POST | None | Self-registration with auto-login |
| `/auth/logout` | POST | Required | Revoke current token |
| `/auth/me` | GET | Required | Current identity + user info |
| `/stats/public/summary` | GET | Optional | Live stats (total, new24h, new7d, changes24h) |
| `/stats/public/charts/<key>` | GET | Optional | Chart data for visualizations |
| `/listings` | GET | Optional | Paginated listings with filters |
| `/map` | GET | Optional | All listings with coordinates for map |
| `/calendar` | GET | Optional | Move-in date listings |
| `/notifications` | GET | Required | User's notification list |
| `/notifications/stream` | GET (SSE) | Required | Real-time notification stream |
| `/notifications/read` | POST | Required | Mark notifications read |
| `/me/summary` | GET | Required | Filtered match counts |
| `/me/filter` | GET/PUT | Required | Read/update notification filter |
| `/me` | DELETE | Required | Delete own account |
| `/filter/options` | GET | Optional | Available filter choices |
| `/devices/register` | POST | Required | Register APNs device token |
| `/admin/users` | GET | Admin | List all users |
| `/admin/users/<id>/toggle` | POST | Admin | Enable/disable user |
| `/admin/users/<id>` | DELETE | Admin | Delete user |
| `/admin/monitor/status` | GET | Admin | Scraper process status |
| `/admin/monitor/start\|stop\|reload` | POST | Admin | Control scraper |

---

## Core features

### Multi-platform scraping

FlatRadar monitors three housing platforms through a unified `AbstractScraper` interface:

| Platform | Source | Method | Granularity |
|---|---|---|---|
| **Holland2Stay** | GraphQL API | `curl_cffi` Chrome impersonation | Unit-level (specific apartment #) |
| **OurDomain** | RENTCafe HTML | `curl_cffi` Safari impersonation | Unit-level (#6045, sqm, floor, view) |
| **Xior** | WordPress AJAX JSON | `curl_cffi` with 1.5s pacing | Unit-level (M1.30.53, sqm, rent, deposit) |

- Each platform implements `AbstractScraper.scrape(task)` → produces `Listing` objects with `source` tags
- `dispatch_scrape_tasks()` routes by source, isolates per-platform failures, merges results
- All scrapers share `RATE_LIMIT_BACKOFF`, `is_cloudflare_body`, and 429 retry logic from `scrapers/base.py`

### Smart adaptive polling

- Dual peak windows (default 08:30–10:00 and 13:30–15:00 CET, weekdays only)
- Adaptive interval: ×0.95 on success (probing downward), ×2.0 on 429 (backing off), floor at `MIN_INTERVAL` (15s)
- Random ±`JITTER_RATIO` jitter prevents mechanical fingerprinting
- All parameters configurable from the web UI

### Rate limit & block protection

- **429**: Retry twice (30s / 60s) → `RateLimitError` → 5-minute cooldown
- **403 Cloudflare WAF**: Immediate `BlockedError` (no retry) → throttled alert (1/30 min) → 15-minute cooldown + recovery steps
- **Proxy**: `HTTPS_PROXY` / `HTTP_PROXY` in `.env`, hot-reloadable

### Notifications

- **Channels**: iMessage, Telegram, Email, WhatsApp — per-user, multi-channel
- **iOS push**: APNs with bilingual support (en/zh `_T` translation map, per-device language grouping)
- **Web panel**: SSE real-time bell + toast, works on any platform
- **Filters**: 10 dimensions per user (rent, area, floor, type, occupancy, city, source, contract, energy, etc.)

### Auto-booking (Holland2Stay only)

- Full GraphQL flow: login → `createEmptyCart` → `addNewBooking` → `placeOrder` → payment URL
- **Fast-path**: booking submitted to thread pool *before* notifications send — reaches server 1–2s earlier
- **Prewarm cache**: cross-round session reuse; invalidated on config change
- Dry-run mode for validation; retry queue for race-lost candidates

### Web admin panel

- **Dashboard**: KPI cards, filter chips, listings table, 48h status-change timeline
- **Listings**: multi-filter (status / city / source / type / rent / energy), sortable table
- **Map**: Leaflet.js + OpenStreetMap, color-coded markers, auto-geocoding
- **Calendar**: monthly move-in view with city filter
- **Stats**: 10 Chart.js visualizations with 7/30/90-day range selector
- **Users**: CRUD, enable/disable, per-user channels / filters / auto-book config
- **Settings**: global polling, adaptive params, platform cities, save-and-reload
- **i18n**: one-click Chinese / English switch, cookie-persisted
- **RBAC**: Admin / User / Guest roles; self-registration with Terms/Privacy confirmation
- **Theme**: light/dark, OS-aware, no flicker

---

## Project status

| Component | Status | Notes |
|---|---:|---|
| Multi-source scraper | ✅ Done | AbstractScraper + ScrapeTask dispatch; CURl_cffi + impersonation pool (Chrome/Safari/Edge) |
| H2S data scraping | ✅ Done | GraphQL API + `curl_cffi` bypasses Cloudflare WAF; per-city complete-scan status |
| OurDomain scraping | ✅ Done | RENTCafe HTML table parsing; unit-level (apartment #, sqft, floor, view, deposit); 2 buildings (Diemen + South-East) |
| Xior scraping | ✅ Done | WordPress AJAX JSON; unit-level (room #, sqm, rent, deposit, move-in date, booking URL); NL 30 buildings |
| Stale listing convergence | ✅ Done | Only runs for complete cities; `Available to book` / `Unknown` use 7 days, `Available in lottery` uses 2 days |
| Multi-city monitoring | ✅ Done | 26 Dutch cities (H2S) + Amsterdam (OurDomain) + 15 cities (Xior); selectable in web UI |
| Multi-channel notifications | ✅ Done | iMessage / Telegram HTML / Email HTML / WhatsApp (Twilio) |
| APNs bilingual push | ✅ Done | iOS push with `_T` translation map; per-device language group dispatch (en/zh) |
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
| Visualization | ✅ Done | 10 charts with 7/30/90-day range-aware KPI cards and distributions |
| Move-in calendar | ✅ Done | Calendar view filtered by city |
| Map view | ✅ Done | Leaflet.js + OpenStreetMap with auto-geocoding |
| i18n (中/EN) | ✅ Done | One-click language switch, cookie-persisted |
| Notification testing | ✅ Done | Per-channel test with result details |
| Guest mode (RBAC) | ✅ Done | Password-free read-only access; admin role required for settings/users/logs |
| Optional auth for web | ✅ Done | Session login enabled when password set; `WEB_GUEST_MODE` controls guest entry |
| Web self-registration | ✅ Done | Login page registration with Terms/Privacy confirmation; SQLite-backed user config |
| Login rate limiting | ✅ Done | IP-based exponential backoff after 5 failures |
| HTTPS / Caddy | ✅ Done | Bundled Caddyfile + docker-compose Caddy service; auto Let's Encrypt |
| Security hardening | ✅ Done | RBAC decorators, notifications/SSE/geocode blocked for guests, CSRF, open-redirect fix, DOM XSS prevention, email verification `PUBLIC_BASE_URL` fail-closed |
| Startup preflight | ✅ Done | Blocks container start if `WEB_PASSWORD` unset or Caddyfile domain is still a placeholder |
| Production WSGI | ✅ Done | Gunicorn (1 worker × 8 threads, timeout=0) replaces Flask dev server in Docker |
| Dependency pinning | ✅ Done | `requirements.lock` with exact `==` versions; Dockerfile installs from lock file |
| Code modularization | ✅ Done | web.py split into `app/` (10 route + 8 shared modules); `mcore/` (interval, prewarm, booking); `mstorage/` (6 mixin modules); `monitor.py` 1,235→971, `storage.py` 1,177→17 re-export |
| Prewarm session cache | ✅ Done | `mcore/prewarm.py` PrewarmCache class; process-level cache with TTL refresh; invalidated on user/config change |
| Error log (errors.log) | ✅ Done | Separate WARNING+ log with `funcName:lineno` format; web.log for Flask app; log viewer with file tabs, line numbers, level coloring, keyword search, auto-scroll |
| Pytest test suite | ✅ Done | 62 test modules (950 tests) covering full stack: models, mcore, mstorage, scraper (H2S/OD/XR), booker, notifier, auth, CSRF, routes, push, i18n |
| Code quality | ✅ Done | Literal types, shared constants, dedup parse logic, mixin composition for Storage |
| **iOS App (FlatRadar)** |
| iOS auth & RBAC | ✅ Done | Admin / User / Guest login with Keychain token persistence; global 401/403 auto-logout |
| iOS Dashboard | ✅ Done | Real-time stats card + sparkline chart; Greeting banner; 2×2 Explore grid with inline mini-charts (status/price/type/energy); Matched listings preview |
| iOS Listings | ✅ Done | Paginated list with pull-to-refresh + infinite scroll; search; multi-select filter (city, status, type, contract, energy); 6 sort modes; detail view with key details, monitoring history, disclaimer |
| iOS Map | ✅ Done | Native MapKit with custom grid clustering; color-coded pins (green/orange/gray); cluster-tap zoom-to; bottom sheet card → deep link to detail |
| iOS Calendar | ✅ Done | Monthly move-in calendar with availability counts per day; month navigation; selected-day listing list → detail |
| iOS Notifications | ✅ Done | Card-style inbox with TODAY/YESTERDAY/EARLIER sections; SSE real-time stream with exponential backoff reconnect; swipe mark-read; unread badge; APNs push with sandbox/production auto-switch |
| iOS Settings | ✅ Done | Notification filter editor (10 dimensions, multi-select pickers from FilterOptions API); Push permission & device registration; Test push (admin); Theme switcher; Account management (logout / delete account); Legal (Terms + Privacy sheets) |
| iOS Admin | ✅ Done | User management list (toggle enable/disable, delete); Monitor control (start/stop/reload with status: PID, last scrape, last count) |
| iOS Adaptive Layout | ✅ Done | iPhone compact: 4 tabs + Browse segmented picker (List/Map/Calendar); iPad regular: 6 tabs; Keyboard shortcuts ⌘1-6 |
| iOS Deep Links | ✅ Done | `h2smonitor://listing/<id>` URL scheme; APNs notification tap → listing detail |
| iOS Design System | ✅ Done | Primary #0A84FF; Success #34C759; Warning #FF9500; Error #FF3B30; Tabular-nums on all KPIs/prices/timestamps; SF Mono for meta data |
| iOS Dark Mode | ✅ Done | Adaptive colors throughout (Login hero, Dashboard cards, Settings); overscroll color matching |
| iOS i18n | ✅ Done | 174 localized strings (en / zh-Hans); all UI text, error messages, labels covered |
| iOS Registration | ✅ Done | User self-registration with bcrypt password hashing; backend rate-limiting (3 IP/hour) + conflict detection; auto-login on register |
| iOS Account Deletion | ✅ Done | DELETE /me endpoint; double confirmation dialog; revokes all tokens + removes SQLite user config |
| iOS Legal | ✅ Done | First-launch Terms agreement (mandatory, non-dismissible); Terms + Privacy sheets in Settings and Login; full legal text in-app |
| iOS StoreKit | ✅ Done | "Buy me a coffee" IAP (3 consumable tiers: Espresso/Latte/Flat White); StoreKit 2 transaction listener |
| iOS Security | ✅ Done | ATS HTTPS-only; Keychain token storage; bcrypt password hashing; all `print()` guarded with `#if DEBUG`; TTL capped at 90 days; username length cap 64 chars |
| iOS App Store Readiness | ✅ Done | PrivacyInfo.xcprivacy (UserDefaults CA92.1, data collection declarations); App icon asset; Info.plist configured |

---

## Technical architecture

### Data flow

```mermaid
flowchart TD
    A["Start monitor.py"] --> B["Load config .env / SQLite user_configs"]
    B --> B1["Config.scrape_tasks_v2()\nSOURCES / CITIES / OURDOMAIN_CITIES / XIOR_CITIES"]
    B1 --> C["Init Storage / Notifier / WebNotifier"]
    C --> D["Enter main_loop polling"]

    D --> E["run_once()"]
    E --> F["Prewarm H2S auto-booking login sessions"]
    E --> G["dispatch_scrape_tasks(tasks)\nRoute ScrapeTask by source"]

    subgraph S["Multi-platform scraping layer scrapers/"]
        G --> H1["holland2stay:Eindhoven...\nHollandStayScraper"]
        G --> H2["ourdomain:Diemen / South-East\nOurDomainScraper"]
        H2 --> H21["RENTCafe floorplans.aspx\nDiscover floorplan ids"]
        H21 --> H22["rcLoadContent.ashx availableunits\nUnit-level listings"]
        H22 --> H23["Parse Unit / Area / Floor / Date\nsource = ourdomain"]
        H2 --> H24{"403 Cloudflare?"}
        H24 -->|Yes| H25["Switch TLS fingerprint\nOURDOMAIN_IMPERSONATES"]
        H25 --> H21

        G --> H3["xior:Eindhoven Kronehoefstraat...\nXiorScraper"]
        H3 --> H31["POST admin-ajax.php\nyardi_room_availability"]
        H31 --> H32["Parse JSON response\napartmentId / sqm / rent"]
        H32 --> H33["Unit-level listings\nsource = xior"]
        H3 --> H34{"429 rate limit?"}
        H34 -->|Yes| H35["Backoff retry RATE_LIMIT_BACKOFF\n1.5s pacing between requests"]
        H35 --> H31
    end

    H1 --> I{"Scrape result"}
    H23 --> I
    H24 -->|All fingerprints failed| I
    H33 --> I
    H34 -->|Retries exhausted| I

    I -->|At least one task succeeded| J["Merge listings + completeness\nsource:city keys avoid name collisions"]
    I -->|All tasks had network failures| K["ScrapeNetworkError\nFailure streak / cooldown"]
    I -->|All tasks hit 403/429| L["BlockedError / RateLimitError\nmain_loop cooldown"]
    I -->|One platform failed| M["Record completeness=false\nIsolate failed platform"]

    M --> J
    J --> N["storage.diff(fresh)\nsource-aware upsert / status changes"]
    N --> O["mark_stale_listings()\nConverge stale rows by source:city"]
    O --> P["Update last_scrape_at / last_scrape_count"]

    P --> Q["Send notifications\nWeb / Telegram / Email / APNs"]
    Q --> Q1["Title/body include platform badge\nH2S / OD / XR"]
    Q1 --> R["Record notified state"]

    P --> T["Scan auto-booking candidates"]
    T --> U{"listing.source == holland2stay\nand directly bookable?"}
    U -->|Yes| V["Submit try_book() immediately\nvia thread pool (concurrent)"]
    U -->|No| W["Skip auto-booking\nOD / XR are notify-only"]

    V --> X["booker.py\nLogin / reuse prewarmed session"]
    X --> Y["createEmptyCart → addNewBooking\n→ placeOrder → idealCheckOut"]
    Y --> AE{"Booking result"}
    AE -->|Success| AF["Send booking success notification\nwith payment URL"]
    AE -->|Failed| AG["Send failure reason\nMay enter retry queue"]
    AF --> R
    AG --> R

    R --> AH["Round complete"]
    W --> AH
    K --> AI["Wait for next round"]
    L --> AI
    AH --> AI
    AI --> D

    subgraph WEB["Web / API read path"]
        N --> WEB1["listings table includes source"]
        WEB1 --> WEB2["dashboard / listings / map / calendar\nshow H2S / OD / XR badges"]
        WEB1 --> WEB3["/api/v1/listings?source=...\nfilter/options.sources"]
        WEB1 --> WEB4["stats source_dist platform distribution"]
    end
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `monitor.py` | Main scheduler, adaptive smart polling, hot reload, PID management, prewarmed session cache, concurrent booking, multi-source dispatch via `dispatch_scrape_tasks()` |
| `scrapers/__init__.py` | `SCRAPER_REGISTRY`, `dispatch_scrape_tasks()` — routes `ScrapeTask` to `AbstractScraper` by source, merges results, isolates per-source failures |
| `scrapers/base.py` | `AbstractScraper` ABC, `ScrapeTask`/`ScrapeResult` dataclasses, shared exceptions (`RateLimitError`/`BlockedError`/`ScrapeNetworkError`) |
| `scrapers/holland2stay.py` | `HollandStayScraper`: GraphQL API, `curl_cffi` Chrome impersonation, pagination, 429 retry, proxy support |
| `scrapers/ourdomain.py` | `OurDomainScraper`: RENTCafe two-stage scraping (floorplans→availableunits), unit dedup, floor/date parsing, Safari impersonation |
| `scraper.py` | Backward-compatible re-exports from `scrapers/` + legacy `scrape_all()` |
| `storage.py` | SQLite persistence re-export from `mstorage/` |
| `mstorage/_base.py` | SQLite schema, connection management, idempotent migrations |
| `mstorage/_listings.py` | Diff detection, listing queries, status counts, stale convergence |
| `models.py` | `Listing` dataclass (with `source` field for multi-platform) and formatting helpers |
| `notifier.py` | `BaseNotifier` ABC; iMessage, Telegram, Email, WhatsApp, `WebNotifier`, multi-dispatch |
| `booker.py` | H2S auto-booking: `PrewarmedSession`, GraphQL cart/order mutations, payment URL |
| `mcore/booking.py` | Booking orchestration: `book_with_fallback()`, `RetryQueue`, `area_key` |
| `mcore/push.py` | APNs dispatch: per-device language grouping, `_T` bilingual translation map, rate/dedup throttle |
| `mcore/interval.py` | Adaptive polling interval + jitter |
| `mcore/prewarm.py` | `PrewarmCache`: process-level session cache with TTL refresh |
| `config.py` | Global config loading, known cities, `ListingFilter`, `AutoBookConfig` |
| `users.py` | `UserConfig`, SQLite `user_configs` read/write, legacy `users.json` migration |
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
| Multi-platform data sources | `AbstractScraper` + `ScrapeTask`/`ScrapeResult` protocol | Each platform implements `scrape(task) → ScrapeResult`; `dispatch_scrape_tasks()` routes by source, isolates failures, merges results |
| H2S Cloudflare 403 | `curl_cffi` + `impersonate="chrome*"` | Emulates Chrome TLS fingerprint; no browser needed |
| OurDomain RENTCafe CF | `curl_cffi` + `impersonate="safari17_0"` | Safari fingerprint passes CF on RENTCafe form POSTs; Chrome blocked |
| RENTCafe reCAPTCHA | reCAPTCHA solving services (capsolver/2captcha) | HTTP API returns valid v3 token in 1–15s; no Playwright required |
| H2S: no useful listing HTML | Call the GraphQL API directly | H2S uses Next.js + Apollo client-side data loading |
| OurDomain: no API | Parse HTML tables with `data-selenium-id` anchors | RENTCafe is ASP.NET server-rendered; Yardi leaves test anchors |
| Unit dedup (OurDomain) | `_merge_unit()` by `unit_id` across floor plans | Same physical unit appears under multiple contract types |
| Sync scraping + async notifications | `run_in_executor` bridge | Keeps `curl_cffi` scraping simple while async notifiers still work |
| Booking race condition | Submit `try_book()` to thread pool before notifications send | Booking and notification network calls run concurrently |
| Repeated login overhead | `PrewarmedSession` cache with TTL refresh | Process-level cache; cross-round reuse; parallel to scrape |
| API rate limits | 429 backoff (30s/60s retry) + 5-min cooldown + adaptive decrease | Three-layer defence; adaptive polling stays below threshold |
| Cloudflare 403 WAF block | Immediate `BlockedError` raise (no retry) + throttled alert (1/30 min) + 15-min cooldown | 403 is permanent — waiting won't help |
| Peak-hour probing | Adaptive interval: ×0.95 on success, ×2.0 on 429, floor at `MIN_INTERVAL` | Discovers max safe frequency without manual tuning |
| iOS bilingual push | `_T` translation map + per-device language grouping in `_send_to_user` | One payload per language group; `_t(text, lang)` lookup |
| Multi-channel notifications | `BaseNotifier` + `MultiNotifier` | Shared formatting, per-channel send implementations |
| Platform-independent notifications | `WebNotifier` writes to SQLite; SSE pushes to browser | Works on VPS/Docker without OS dependency |
| iMessage on non-macOS | `is_macos()` gate in `create_user_notifier()` | Clear warning, skips gracefully |
| Concurrent SQLite access | WAL journal mode | Monitor writes; web.py reads from separate connection |
| Hot reload across platforms | Signals on Unix, reload request file fallback on Windows | Settings apply without restart |
| Theme switching without flicker | Inline `<head>` script + CSS custom properties | Theme applied before CSS paint |
| Optional panel auth | Skip auth when `WEB_PASSWORD` is empty | Local use frictionless; protection when exposed |
| Modularisation | `scrapers/` + `mcore/` + `mstorage/` + `app/routes/` | Each module single responsibility; `web.py` is an 154-line bootstrap |
| INFO noise | Separate `errors.log` (WARNING+) with `funcName:lineno` | `monitor.log` stays INFO+; `errors.log` captures sparse anomalies |

### H2S GraphQL API

| Parameter | Value |
|---|---|
| Endpoint | `POST https://api.holland2stay.com/graphql/` |
| Category UID | `category_uid: "Nw=="` (Residences) |
| Available to book | `available_to_book: { in: ["179"] }` |
| Available in lottery | `available_to_book: { in: ["336"] }` |
| Custom fields | `custom_attributesV2` → `price` (total rent incl. service costs), `living_area`, `floor`, `available_startdate`, and more |

### OurDomain / RENTCafe data

| Parameter | Value |
|---|---|
| Base URL | `https://thisisourdomain.securerc.co.uk/onlineleasing/` |
| Stage 1 | `floorplans.aspx` → extract `subPointerId` (floor plan IDs) |
| Stage 2 | `rcLoadContent.ashx?contentclass=availableunits&floorPlans={id}&MoveInDate={date}` → unit rows |
| Unit fields | `data-selenium-id="Apt{N}"` (apartment #), `SqFt` (m²), `Rent` (€), `Deposit`, `Amenity` (floor/view), `AvailDate` (availability) |
| Status | `<span class="text-success">Available</span>` / `<span class="text-warning">Wait List</span>` → mapped to "Available to book" / "Available in lottery" |
| Dedup | `_merge_unit()` by numeric `unit_id` — same physical unit appears under multiple floor plan types |
| Booking | ASP.NET multi-step form POST → `rcformsave.ashx`; reCAPTCHA v3+v2 protected (see [OURDOMAIN.md](OURDOMAIN.md) §10) |

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

**Pre-release health check:**

Run the doctor before a first deploy, after changing `.env`, or before publishing
a new build:

```bash
python -m tools.doctor

# Offline / CI-safe mode: skip proxy, SMTP and Holland2Stay network probes
python -m tools.doctor --no-network

# Also verify SMTP username/password login (does not send an email)
python -m tools.doctor --smtp-login
```

The doctor checks `.env`, writable data/log paths, SQLite integrity and required
tables, SQLite user configs, Caddy placeholder domains, APNs key/config, SMTP settings,
proxy exit IP, Holland2Stay GraphQL reachability, and Docker supervisord control.
It exits with code `1` if a blocking `FAIL` is found, which makes it safe to add
to deployment scripts.

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
python -m tools.doctor --no-network
docker compose up -d --build
```

---

## Configuration

User-level settings (notifications, filters, auto-booking) are managed in the web UI and stored in SQLite `user_configs`.
Legacy `data/users.json` is imported once using the `users_storage_migrated_v1` meta flag and kept as a permanent `.bak` migration backup.

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
users.py            UserConfig, SQLite user_configs read/write + legacy migration
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
  flatradar.spec    PyInstaller spec file
launcher.py         macOS .app entry point (imports web.app, handles --run-monitor)
.github/workflows/  GitHub Actions CI/CD (builds .dmg + .exe on tag/manual trigger)
data/               Runtime data (auto-created)
  listings.db       SQLite database
  users.json*.bak   Permanent backup created when legacy users.json is migrated
  monitor.pid       Monitor process PID for hot reload
logs/               Log files (auto-created; supervisord writes monitor.log + web.log)
```

---

## License

FlatRadar is licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).

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
