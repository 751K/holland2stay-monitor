# FlatRadar

[![Website](https://img.shields.io/badge/Website-flatradar.app-0057CC?style=flat-square)](https://flatradar.app) [![User Guide](https://img.shields.io/badge/Guide-docs-10B981?style=flat-square)](https://flatradar.app/guide) [![Support](https://img.shields.io/badge/Support-help-64748B?style=flat-square)](https://flatradar.app/support) [![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/751K) [![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue?style=flat-square)](../LICENSE) [![Release](https://img.shields.io/github/v/release/751K/holland2stay-monitor?style=flat-square)](https://github.com/751K/holland2stay-monitor/releases) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![iOS](https://img.shields.io/badge/iOS-SwiftUI-000000?style=flat-square&logo=apple&logoColor=white)](https://github.com/751K/FlatRadar-iOS) [![Android](https://img.shields.io/badge/Android-Compose-3DDC84?style=flat-square&logo=android&logoColor=white)](https://github.com/751K/FlatRadar-Android)

> 中文版：[README_cn.md](README_cn.md)

FlatRadar monitors the Dutch rental market. It runs as a hosted service at
[flatradar.app](https://flatradar.app) and can also be self-hosted. It tracks
Holland2Stay, OurDomain, OurCampus, Xior, Magis, Student Experience and Plaza,
and as soon as a listing matches your criteria it sends a notification through
the channels you enabled, with a direct link included. For Holland2Stay it goes
one step further: it first carries the booking through to the payment page, then
sends the payment link along with the alert, so only the payment itself remains.
For Plaza, it can submit applications automatically when the user enables it.

**Paying on your behalf is outside what FlatRadar does, and it cannot guarantee
a booking succeeds.** Whether you get the unit still depends on the competition
on the platform.

## How to use it

| Path | Best for | Start here |
|---|---|---|
| **Hosted service** | Most people | Open [flatradar.app](https://flatradar.app). The first sign-in creates an account automatically; set filters and notification channels in the panel. Nothing to install. |
| **Mobile apps** | Alerts on your phone | [iOS](https://apps.apple.com/us/app/flarradar/id6769857080) · [Android APK](https://github.com/751K/FlatRadar-Android/releases/latest/download/app-release.apk) |
| **Desktop builds** | macOS / Windows | Download from [Releases](https://github.com/751K/holland2stay-monitor/releases) |
| **Self-hosting** | Your own instance, database and delivery | [Quick start](#quick-start) below: one Python command on your own computer, or Docker for a public server. About 2 GB of RAM; a public server may also need a residential proxy. |

The hosted service and self-hosting run the same codebase.

Guide: [flatradar.app/guide](https://flatradar.app/guide) ·
Support: [flatradar.app/support](https://flatradar.app/support) ·
[support@flatradar.app](mailto:support@flatradar.app)

> FlatRadar is an independent, unofficial tool. It is not affiliated with,
> endorsed by, sponsored by, maintained by, or operated by any housing platform
> it monitors. Use it only for personal, non-commercial purposes and follow each
> platform's terms. Always verify listing details, prices, eligibility, and
> booking status on the official platform before making decisions.

---

## Feature overview

| | |
|---|---|
| **Coverage** | Seven platforms: Holland2Stay, OurDomain, OurCampus, Xior, Magis, Student Experience and Plaza. Polling tightens during the hours when new listings tend to appear |
| **Alert channels** | Web, Telegram, Email, WhatsApp, iOS push, Android push, iMessage; several may be enabled at once |
| **Filters** | Maximum rent, minimum area, minimum floor, type, occupancy, city, neighbourhood, platform, contract type, tenant requirements, and others |
| **Views** | List, map, calendar, dashboard and charts, in English or Chinese |
| **Multiple users** | Supported. Guest, user and admin roles; every user keeps independent filters and credentials |
| **Automatic booking and applications** | Holland2Stay stops at the payment page; Plaza applications can be submitted automatically and are off by default — see [Auto-booking](#auto-booking) |

### Platform coverage

| Platform | Coverage | Scraper maturity | Booking |
|---|---|---|---|
| Holland2Stay | Any Dutch city you configure | Proven — the bulk of what lands | Auto-booking supported |
| OurDomain | Amsterdam Diemen / South-East | Proven | Notify only (booking flow built, not enabled) |
| Xior | Any of 30 buildings across 14 cities | Proven | Notify only (booking flow built, not enabled) |
| OurCampus | Amsterdam Diemen (1 building) | Checked against real markup; very low volume | Notify only |
| Magis | 17 buildings across 5 cities (9 in Eindhoven) | Verified against live data; plain HTTP, no anti-bot | Notify only |
| Student Experience | Amsterdam (4 sites) and Leiden | Verified against live data; the listing date lives only on the detail page, so each unit costs an extra request | Notify only |
| Plaza | 13 Dutch cities | Verified against live data; a paid account (€27.50/yr) is required to *apply*, not to see listings | Automatic applications supported, off by default |

A platform can also run in **shadow mode** (`SHADOW_SOURCES`): scraped and stored
as usual, but it sends no notifications. That is how a newly added scraper is
validated against live data without anyone's alerts depending on it yet.

Coverage shifts as third-party sites change. The scrapers are documented in
[H2S.md](H2S.md), [XIOR.md](XIOR.md), [OURDOMAIN.md](OURDOMAIN.md),
[PLAZA.md](PLAZA.md) and [SCRAPING_RECON.md](SCRAPING_RECON.md).

### Clients

| Interface | Status |
|---|---|
| Web dashboard | Stable — the primary interface for self-hosting |
| [iOS app](https://apps.apple.com/us/app/flarradar/id6769857080) | Maintenance — on the App Store, feature-complete for current scope |
| [Android app](https://github.com/751K/FlatRadar-Android/releases/latest/download/app-release.apk) | Beta — signed `.apk`, sideload it. FCM push verified. Not going to Play Store — direct download is the distribution channel |
| Desktop packages | macOS `.dmg` and Windows `.zip` from [Releases](https://github.com/751K/holland2stay-monitor/releases) |

---

## Requirements

|  | Minimum | Notes |
|---|---|---|
| RAM | **2 GB** | Each Cloudflare-protected source keeps a headless Chromium resident (~200–400 MB each). 1 GB is only enough for a single source. |
| Disk | ~1.5 GB | The patched Chromium alone unpacks to ~700 MB |
| Docker | Engine 20.10+ with the Compose plugin | Only for the public deployment. `docker compose`, not the old `docker-compose` script |
| Python | 3.11+ | For running on your own computer. The Docker image ships 3.11; CI and desktop builds use 3.12 |
| OS | Any for personal use; Linux for a server | See the [macOS note](#1-on-your-own-computer) below |
| Network | Residential or mobile proxy may be required | Some platforms have returned 403 to datacenter exit IPs. Cloud deployment reads `HTTPS_PROXY`; a proxy account is an extra cost, and none of it is needed for the hosted service |
| Domain | Optional | Needed only for the public deployment |

---

## Quick start

### 1. On your own computer

For alerts just for you. No Docker, domain, password or proxy:

```bash
git clone https://github.com/751K/holland2stay-monitor.git
cd holland2stay-monitor
pip install -r requirements.txt
python launcher.py
```

The first run creates `.env` with working defaults and downloads the browser
engine once (~700 MB unpacked). Then the panel opens at `http://127.0.0.1:8088`
and monitoring starts on its own. Out of the box it watches Holland2Stay in
Eindhoven; enable other platforms and cities under **Settings**.

To get alerts, open **Users**, create a user for yourself, turn on a notification
channel and send a test. Telegram and email through your own SMTP need nothing
configured in `.env`.

Closing the terminal (or Ctrl+C) stops both the panel and the monitor. Run
`python launcher.py` again next time; your data stays in `data/`.
`python launcher.py --no-monitor` opens only the panel.

> ⚠️ The panel has no password and listens on this computer only. Do not expose
> it to a network; for that, use the public deployment below.

> **macOS**: headless Chromium can crash on macOS, so a browser window may pop up
> while it checks Cloudflare-protected platforms. Leave it open. If you would
> rather not see it, run the same local setup in Docker Desktop:
> `cp .env.example .env && mkdir -p data logs && docker compose -f docker-compose.yml -f docker-compose.local.yml up -d h2s`.

### 2. Public deployment (Docker)

For a server that other people sign in to. Set up the networking side yourself
before you begin. DNS, firewall ports, TLS certificates, client IP handling and replacing Caddy with your own reverse proxy
are covered in [DEPLOYMENT.md](DEPLOYMENT.md).

**Step 1** — create the config and data directories:

```bash
cp .env.example .env
mkdir -p data logs logs/caddy
```

**Step 2** — edit `.env`:

```env
WEB_PASSWORD=a-long-random-string
HTTPS_PROXY=http://user:pass@proxy-host:port
PUBLIC_BASE_URL=https://your.domain.com
SESSION_COOKIE_SECURE=true
```

- `WEB_PASSWORD` — the container refuses to start while it is empty.
- `HTTPS_PROXY` — configure an appropriate residential or mobile proxy if the
  server's exit is blocked. Datacenter exits have previously received 403s;
  availability depends on the actual network and platform response.
- `PUBLIC_BASE_URL` — verification emails are built from it; unset, the app
  refuses to send them.
- `SESSION_COOKIE_SECURE` — `.env.example` ships `false`; set it to `true` for
  the HTTPS deployment.

`SUPPORT_EMAIL` and `TIMEZONE` arrive pre-filled (`support@example.com`,
`Europe/Amsterdam`) and every other key has a working default — change them only
if the default is wrong for you.

**Step 3** — edit `Caddyfile` and replace `your.domain.com` with your domain.

**Step 4** — start it:

```bash
docker compose up -d
```

Caddy obtains and renews the certificate on its own. Once it is up, open your
domain and sign in with the password you set in step 2. The username is
`admin` unless you set `WEB_USERNAME` yourself.

> The entrypoint validates exactly two things before starting: that
> `WEB_PASSWORD` is non-empty, and that `Caddyfile` no longer contains
> `your.domain.com`. Either one failing prints a `FATAL` line and stops the
> container. Nothing else is checked — a missing `HTTPS_PROXY` or
> `PUBLIC_BASE_URL` starts up normally and only surfaces later in the logs.

### Confirming it works

Both processes should report `RUNNING`:

```bash
docker exec h2s supervisorctl -c /etc/supervisor/conf.d/app.conf status
```

Follow the first round:

```bash
docker compose logs -f h2s
```

The first round is slower than later ones; that is expected. A healthy round ends
with `本轮完整扫描: N/N 城市 (...)` followed by `本轮结束: ... 新房源`.

Once signed in, add users and notification channels, then pick the platforms and
cities to monitor. Both user-level and system-level settings are managed from the
panel; `.env` only needs editing when credentials, the domain or the timezone
change.

### First run checklist

1. Sign in to the panel as admin.
2. Open the Users page and create the user(s) who should receive alerts.
3. For each user, enable at least one notification channel and send a test.
4. Open the Settings page and enable the platforms **and** their cities or
   buildings — both are required; ticking only cities does nothing.
5. Watch `/monitoring` for the first complete round and confirm that the
   expected sources report data.
6. Keep day-to-day changes in the panel. `.env` is only for deployment settings,
   secrets and proxy configuration; writing runtime keys back into `.env`
   overrides the dashboard.

### Development

`python launcher.py` runs the panel and the monitor together. While working on
the code you may want them separately, each in its own terminal:

```bash
python web.py       # panel only
python monitor.py   # scraper loop
```

They are independent processes that share SQLite and use process-control
requests for reloads — running just `web.py` gives you a panel that never gets
new data.

---

## Upgrading and backups

```bash
cd /path/to/holland2stay-monitor
docker exec h2s python -c "import sqlite3; sqlite3.connect('data/listings.db').execute('VACUUM INTO ?', ('data/listings.db.bak.$(date +%Y%m%dT%H%M%S)',))"
git pull
docker compose build h2s
docker compose up -d --force-recreate h2s
```

Two things need backing up, and they **must be backed up together**:

- `data/listings.db` — listings, users, credentials, device tokens, and
  system settings (`app_settings`)
- `.env` — the secrets, in particular `DATA_ENCRYPTION_KEY`

Passwords and platform credentials in the database are encrypted with the key in
`.env`; restoring one without the other leaves you with credentials nobody can
decrypt. As of v1.16.0 the monitoring scope and polling cadence also live in the database; restoring only `.env` falls back to code defaults.

**Do not copy the database file directly while the container is running** — you
will miss recent writes. For a consistent snapshot:

```bash
docker exec h2s python -c "import sqlite3; \
  sqlite3.connect('data/listings.db').execute('VACUUM INTO \"data/backup.db\"')"
```

---

## Where configuration lives

Configuration lives in three places:

| What | Where | Edited via |
|---|---|---|
| Per-user: channels, filters, auto-booking | SQLite `user_configs` | Dashboard → Users |
| System: sources, cities, intervals | SQLite `app_settings` | Dashboard → Settings |
| Deployment: credentials, paths, timezone, base URL | `.env` | Text editor |

Monitoring scope and polling cadence are managed in the database. Runtime keys —
`SOURCES`, `CITIES`, `*_CITIES`, `AVAILABILITY_FILTERS`,
`CHECK_INTERVAL`, `PEAK_*` and friends — live in the database and are managed
from the dashboard. First start migrates them out of `.env` automatically,
backing up the whole file to `.env.bak.<timestamp>` beforehand and logging
exactly what moved.

Deployment configuration, credentials and advanced tuning can remain in `.env`:

| Group | Notes |
|---|---|
| Server credentials | Passwords, API keys, encryption key and proxy URLs. Per-user platform and channel credentials are stored encrypted in user configuration. |
| Deployment facts | `DB_PATH`, `TIMEZONE`, `PUBLIC_BASE_URL`, `SESSION_COOKIE_SECURE`, `SUPPORT_EMAIL`. `DB_PATH` must be available before opening the database. |
| Thresholds and switches | Notification channel toggles, quotas and other advanced settings; most have defaults. |

The ones worth knowing up front:

| Key | Default | What it controls |
|---|---|---|
| `WEB_PASSWORD` | — | **Required.** The container refuses to start without it |
| `HTTPS_PROXY` | — | Proxy for scraping when the server's exit is blocked |
| `PUBLIC_BASE_URL` | — | Required for verification emails; without a valid HTTPS URL the service refuses to generate the link |
| `MONITOR_HEARTBEAT_MAX_AGE` | `900` | How long the monitor may be silent before `/health` reports unhealthy |
| `HEALTH_*` / `WATCHDOG_*` | see `.env.example` | Thresholds for the data-degradation alerts behind `/monitoring` |
| `STALE_RESERVED_HOURS` / `STALE_OCCUPIED_HOURS` | `0.5` / `2` | How long a listing must be absent from the feed before it is inferred Reserved, then Occupied — see [Listing status](#listing-status) |

The full list is in [.env.example](../.env.example), which documents every key;
key names and their groups are registered in `env_registry.py`.

### Environment variables win over the database

For runtime settings, precedence is **startup environment > runtime entries left in `.env` > `app_settings` > code default**.
The environment layer exists as an override hatch for containerised debugging:

```bash
docker compose run -e CHECK_INTERVAL=30 h2s python monitor.py
```

An override can make the saved value differ from the value in use. The dashboard
flags "overridden by an environment variable,
changes here will not take effect", and the monitor warns at startup about any
such key left in `.env`. **Use the dashboard for day-to-day changes** — writing
these keys back into `.env` overrides it. Deployment settings, secrets and tuning
settings do not hot-reload; restart the affected process after changing them.
Restart the whole application if the database path changes.

### Input validation

Misspelled key names are no longer silent. The monitor audits `.env` at startup
and warns about unregistered keys, with the closest match:

```
PEAK_STRAT is not a key this project knows; it will have no effect (did you mean PEAK_START?)
```

Malformed scope values are caught too, with two different outcomes: format
errors (delimiters, field counts, non-numeric ids) are rejected by the dashboard
outright; unknown entities (a city id or platform name absent from the known
tables) only warn and still save — official registries change, and hard-failing
would turn a newly launched city into a save error.

### Enabling a new platform

In Dashboard → Settings, tick the platform **and** its cities or buildings.
Both are required; ticking only the cities does nothing. Sources listed in
`SHADOW_SOURCES` are scraped and stored but send **no** notifications — useful
for validating a new platform before exposing it to users. Entries missing from
`SOURCES` are ignored with a warning: a source listed there but not enabled is
simply off, not shadowed.

Before a production deploy:

```bash
python -m tools.doctor --no-network
```

Run it on the host, in the repository directory — `tools/` is deliberately not
copied into the image. It is read-only: it never writes config, sends
notifications, or touches the monitor process.

Its `Settings` section answers the most common post-deploy question: whether the migration ran (how many rows are in `app_settings`), whether any key left in `.env` is overriding the dashboard, and whether any key name is misspelled.

---

## Listing status

Platform feeds do not always say when a listing has been removed. Some report
unavailable or occupied statuses directly; for other listings the system infers
state from absence in complete scans. The panel distinguishes inferred states
from upstream reports.

For feeds that do not cover reservations, absence normally leads to an inferred
Reserved state and then Occupied. When the configured Holland2Stay feed includes
Reserved, the intermediate inference can be skipped:

```mermaid
stateDiagram-v2
    direction LR

    state "Available / lottery / Unknown" as A
    state "Reserved" as R
    state "Occupied" as O

    [*] --> A: first seen in the feed
    A --> R: gone 30 min
    R --> O: gone 2 h
    A --> O: reservations covered and gone 2 h
    R --> A: seen again
    O --> A: seen again
```

Anything the system inferred carries an **inferred** badge in the panel and a
`status_is_inferred` flag in the API, so a reported status and a deduced one are
never confused — in practice most `Occupied` rows you see will be inferred.
Inferred transitions do not generate notifications.

The two-hour default is informed by Holland2Stay's payment window; the result
remains an inference and may differ from the platform's current state. The
intermediate Reserved stop exists so that being wrong is
cheap: `Reserved → Available` is an ordinary transition, whereas jumping
straight to Occupied and then seeing the listing return produces a wave of false
"back on the market" alerts. Both thresholds are configurable
(`STALE_RESERVED_HOURS` / `STALE_OCCUPIED_HOURS`); the reasoning and the failure
modes behind them are in
[Architecture: listing status and completeness](ARCHITECTURE.md#status-convergence).

Convergence only runs for source/city pairs that were **completely** scanned
that round. A failed scrape is never read as "the listings are gone".

---

## Auto-booking

For Holland2Stay, it signs in with the account you configure,
attempts eligible directly-bookable listings, and **stops at the payment URL** —
it never completes a payment.

If the Holland2Stay booking outcome is uncertain, auto-booking is paused for
that account and the user is asked to check it. Sign in to Holland2Stay to check
your orders or reservations, pay or cancel as appropriate, then select
“Checked; resume auto-booking” in user settings. Protection cannot be removed
while the booking task is running; stop monitoring first if it is stuck. This
protection survives monitor restarts and applies to all users sharing the account.

For Plaza, users can configure their username and password and separately enable
automatic applications. The switch is off by default. An application is submitted
directly and does not guarantee a successful rental. See [Plaza](PLAZA.md).
If an application outcome is uncertain, check your applications on Plaza and
select “Checked; resume applications for this listing” in user settings. Only
that account and listing are paused; other listings can still be processed.
Protection cannot be removed while the application task is running.

Xior, OurDomain and OurCampus use RENTCafe and share an implementation
([`bookers/rentcafe.py`](../bookers/rentcafe.py)). Captcha solving and parts of
the application flows are implemented, but their end-to-end booking paths have
not been verified and remain **switched off**.

| Platform | Reached | Missing |
|---|---|---|
| Xior | A real account reached the application form and saved a draft (2026-08-03) | Saving after the system uploads the ID document has not been verified. A draft **does not hold the unit**; the next page asks for IBAN/SWIFT. |
| OurDomain | Entry leg verified against the live site (2026-08-04): floorplans → available units → terms POST | The post-login path needs real-account verification. If the application form is missing, the implementation retries unit selection once and aborts if the form is still unavailable. |
| OurCampus | `OurCampusBooker` registered and credentials can be entered in the panel, but **off for users** (not in `_AUTO_BOOK_SOURCES`). Verified with a real account: unit selection, terms page, Start Application (v3 accepted first try) and login all work | Landing on Applicant Info after login does not work yet. The premise that starting an application holds the unit is disproved: the site states it only enters you into the lottery |
| Magis | Nothing | Booking flow has never been scouted |

> The hosted demo has auto-booking disabled for user accounts. Email us or
> [self-host](#quick-start) to use it.

---

## Troubleshooting

Start at **`/monitoring`** (admin). It shows per-source health, the last 30 rounds
broken down as `listings (complete/targets)`, and any active alerts — most of the
table below can be answered there without shelling into the container.

`/health` and `/monitoring` answer different questions. `/health` is *"is the loop
alive"* (heartbeat freshness) and drives the container's health status.
`/monitoring` is *"is the data still right"* — a parser broken by an upstream
redesign leaves `/health` green. Data degradation alerts admins; it deliberately
does **not** flip the container unhealthy, because restarting cannot fix a parser
mismatch and would only interrupt a scrape in progress.

| Symptom | Cause and fix |
|---|---|
| No alerts at all, dashboard shows stale listings | The monitor process is down. `supervisorctl status` to confirm, then `supervisorctl start monitor`. `/health` returns 503 once the heartbeat is older than `MONITOR_HEARTBEAT_MAX_AGE`. |
| A source shows `down` or `warn` on `/monitoring` | The card lists which rule fired. See [health and troubleshooting](ARCHITECTURE.md#health-and-troubleshooting). |
| Need logs from a specific time window | `/logs` filters server-side by keyword, level and `since`/`until` — it is not limited to what is currently on screen. |
| Logs repeat `H2S source 熔断` | Cloudflare is blocking your exit IP. The breaker pauses that one source and retries a single city later. If it persists, set `HTTPS_PROXY`. |
| Logs repeat `CF 挑战 ... 未解开` | Same cause — usually IP reputation rather than anything local. |
| One platform always returns 0 listings | May be genuine. Check whether the round was marked complete; see [listing status and completeness](ARCHITECTURE.md#status-convergence). |
| A listing you know is taken still shows as available | Its city was not completely scanned, so it never became eligible for convergence. Check `(complete/targets)` on `/monitoring`. |
| A batch of listings turned Occupied at once | Expected if they carry the **inferred** badge — that is aging catching up after a restart or a threshold change, and it generates no notifications. Without the badge, the platform actually reported it. |
| Logs repeat `抓到 0 个单元，第 N/3 轮` | Normal. An empty result from OurDomain/OurCampus has to repeat for 3 rounds before it counts, so a single bad round cannot converge a whole building. |
| Container gets OOM-killed | Raise `mem_limit`. Each Cloudflare-protected source holds a resident browser. |
| Container will not start, log says `FATAL` | The entrypoint preflight. Either `Caddyfile` still says `your.domain.com` or `WEB_PASSWORD` is empty. Both are refusals to expose an unauthenticated panel. |
| Cannot sign in after setting `WEB_PASSWORD` | The username is `admin` unless you set `WEB_USERNAME`. |
| `supervisorctl` says "no such file" | Its socket is not at the default path. Add `-c /etc/supervisor/conf.d/app.conf`. |

The container reporting `healthy` covers both web and monitor, but an unhealthy
container is **not** restarted automatically — wire the health status into your
own monitoring if you want to be paged.

---

## Documentation

| Document | For |
|---|---|
| [User Guide](https://flatradar.app/guide) | Panel settings and daily use |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Docker topology, Caddy and TLS, ports, logs, and using a different reverse proxy |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System architecture, key failure boundaries and troubleshooting entry points |
| [API.md](API.md) | Backend contracts for mobile and integrations |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [H2S.md](H2S.md) · [H2S_BOOKING_OPS.md](H2S_BOOKING_OPS.md) · [RENTCAFE.md](RENTCAFE.md) · [XIOR.md](XIOR.md) · [OURDOMAIN.md](OURDOMAIN.md) · [PLAZA.md](PLAZA.md) · [SCRAPING_RECON.md](SCRAPING_RECON.md) | Per-platform research; `RENTCAFE.md` covers the booking flow shared by Xior, OurDomain and OurCampus, and `PLAZA.md` covers Plaza booking only — Plaza's scraping side, with Magis and Student Experience, is in `SCRAPING_RECON.md` |
| [iOS_README.md](https://github.com/751K/FlatRadar-iOS/blob/master/docs/iOS_README.md) | iOS client — moved to [FlatRadar-iOS](https://github.com/751K/FlatRadar-iOS) |
| [ANDROID_PLAN.md](https://github.com/751K/FlatRadar-Android/blob/master/docs/ANDROID_PLAN.md) | Android client — moved to [FlatRadar-Android](https://github.com/751K/FlatRadar-Android) |
| [dataflow_en.mmd](dataflow_en.mmd) · [dataflow_ch.mmd](dataflow_ch.mmd) | Full scrape/notify flow as a Mermaid diagram |

---

## Support the project

FlatRadar is built and run by one person. Server costs, push infrastructure and
App Store fees come out of pocket.

- Star the repository if it is useful to you.
- Sponsor via [GitHub Sponsors](https://github.com/sponsors/751K) or
  [flatradar.app/donate](https://flatradar.app/donate).
- Questions and bug reports: [flatradar.app/support](https://flatradar.app/support).

## License

[PolyForm Noncommercial License 1.0.0](../LICENSE) — free for personal and other
noncommercial use; commercial use is not granted by this licence.
