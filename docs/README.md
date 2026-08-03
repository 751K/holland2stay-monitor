# FlatRadar

[![Website](https://img.shields.io/badge/Website-flatradar.app-0057CC?style=flat-square)](https://flatradar.app) [![User Guide](https://img.shields.io/badge/Guide-docs-10B981?style=flat-square)](https://flatradar.app/guide) [![Support](https://img.shields.io/badge/Support-help-64748B?style=flat-square)](https://flatradar.app/support) [![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/751K) [![License](https://img.shields.io/github/license/751K/holland2stay-monitor?style=flat-square)](../LICENSE) [![Release](https://img.shields.io/github/v/release/751K/holland2stay-monitor?style=flat-square)](https://github.com/751K/holland2stay-monitor/releases) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![iOS](https://img.shields.io/badge/iOS-SwiftUI-000000?style=flat-square&logo=apple&logoColor=white)](../ios/FlatRadar) [![Android](https://img.shields.io/badge/Android-Compose-3DDC84?style=flat-square&logo=android&logoColor=white)](../android)

> 中文版：[README_cn.md](README_cn.md)

Dutch rental listings appear and disappear within hours. FlatRadar polls the
platforms you care about, tells you the moment something matching your filters
shows up, and — for Holland2Stay — can hold the booking up to the payment page
while you are still reading the alert.

It is self-hostable: one container, one SQLite file, no external services
required beyond the notification channels you choose to enable.

**Website:** [flatradar.app](https://flatradar.app) ·
**User guide:** [flatradar.app/guide](https://flatradar.app/guide) ·
**Support:** [flatradar.app/support](https://flatradar.app/support) ·
**Contact:** [surrport@flatradar.app](mailto:surrport@flatradar.app)

> FlatRadar is an independent, unofficial tool. It is not affiliated with,
> endorsed by, sponsored by, maintained by, or operated by any housing platform
> it monitors. Use it only for personal, non-commercial purposes and follow each
> platform's terms. Always verify listing details, prices, eligibility, and
> booking status on the official platform before making decisions.

---

## What you get

| | |
|---|---|
| **Monitoring** | Holland2Stay, OurDomain and Xior, polled on an adaptive interval that tightens during peak listing hours |
| **Alerts** | Web, Telegram, Email, WhatsApp, iOS push, Android push, iMessage — each user picks their own channels and filters |
| **Views** | List, map, calendar, dashboard and charts, in English or Chinese |
| **Accounts** | Guest, user and admin roles; every user keeps independent filters and credentials |
| **Auto-booking** | Holland2Stay only — see [Auto-booking](#auto-booking) for what it does and does not do |

### Platform coverage

| Platform | Coverage | Booking |
|---|---|---|
| Holland2Stay | Any Dutch city you configure | Auto-booking supported |
| OurDomain | Amsterdam Diemen / South-East | Notify only |
| OurCampus | Amsterdam Diemen (1 building) | Notify only |
| Xior | 30 buildings across 14 cities | Notify only |

Coverage shifts as third-party sites change. The scrapers are documented in
[XIOR.md](XIOR.md), [OURDOMAIN.md](OURDOMAIN.md) and
[SCRAPING_RECON.md](SCRAPING_RECON.md).

### Clients

| Interface | Status |
|---|---|
| Web dashboard | Stable — the primary interface for self-hosting |
| [iOS app](https://apps.apple.com/us/app/flarradar/id6769857080) | Maintenance — on the App Store, feature-complete for current scope |
| [Android app](https://github.com/751K/holland2stay-monitor/releases/latest/download/app-release.apk) | Beta — signed `.apk`, sideload it. FCM push verified. Not going to Play Store — direct download is the distribution channel |
| Desktop packages | macOS `.dmg` and Windows `.zip` from [Releases](https://github.com/751K/holland2stay-monitor/releases) |

---

## Requirements

|  | Minimum | Notes |
|---|---|---|
| RAM | **2 GB** | Each Cloudflare-protected source keeps a headless Chromium resident (~200–400 MB each). 1 GB is only enough for a single source. |
| Disk | ~1.5 GB | The patched Chromium alone unpacks to ~700 MB |
| Python | 3.11+ | Docker image ships 3.11; CI and desktop builds use 3.12 |
| OS | Linux recommended | See the [macOS note](#running-from-source) below |

---

## Quick start

### Docker (recommended)

```bash
cp .env.example .env
mkdir -p data logs logs/caddy
```

Edit `.env` before starting. For anything reachable from the internet, set at
minimum:

```env
WEB_PASSWORD=change-me
SESSION_COOKIE_SECURE=true
PUBLIC_BASE_URL=https://your.domain.com
SUPPORT_EMAIL=support@example.com
```

Edit `Caddyfile` to point at your domain, then:

```bash
docker compose up -d
```

Confirm both processes came up — you should see `monitor` and `web` both
`RUNNING`:

```bash
docker exec h2s supervisorctl -c /etc/supervisor/conf.d/app.conf status
```

The first scrape takes longer than later ones because it has to pass a
Cloudflare challenge (10–35 s on a small VPS). Follow along with:

```bash
docker compose logs -f h2s
```

A healthy round ends with `本轮完整扫描: N/N 城市 (...)` — the count is the
number of configured source/city pairs — followed by `本轮结束: ... 新房源`.

Then open your domain, sign in, and add users, notification channels and the
cities you want monitored.

### Running from source

```bash
pip install -r requirements.txt
python -m cloakbrowser install   # patched Chromium, ~700 MB unpacked
cp .env.example .env
python web.py
```

Open `http://127.0.0.1:8088`.

> **macOS**: the free CloakBrowser build for macOS lags the Linux one, and
> headless mode can abort, so local runs fall back to a visible browser window.
> Use Linux/Docker for anything you actually depend on.

---

## Configuration

Day-to-day settings — sources, cities, intervals, filters, channels,
auto-booking, theme — live in the web dashboard. Deployment-level settings live
in `.env`; start from [.env.example](../.env.example), which documents every key.

The ones worth knowing up front:

| Key | Default | What it controls |
|---|---|---|
| `SOURCES` | `holland2stay` | Which platforms to poll, comma-separated |
| `CITIES` | `Eindhoven,29` | Holland2Stay cities, as `Name,id` pairs joined by `\|` |
| `OURDOMAIN_CITIES` / `OURCAMPUS_CITIES` / `XIOR_CITIES` | — | Same format for the other sources; building keys are listed in each scraper |
| `SHADOW_SOURCES` | — | Sources that are scraped and stored but send **no** notifications. For validating a new platform before exposing it to users |
| `CHECK_INTERVAL` | `300` | Seconds between rounds outside peak hours |
| `PEAK_INTERVAL` | `60` | Seconds between rounds during peak hours |
| `MONITOR_HEARTBEAT_MAX_AGE` | `900` | How long the monitor may be silent before `/health` reports unhealthy |
| `HEALTH_*` / `WATCHDOG_*` | see `.env.example` | Thresholds for the data-degradation alerts behind `/monitoring` |
| `HTTPS_PROXY` | — | Route scraping through another exit IP when Cloudflare gets aggressive |

Enabling a source requires **both** `SOURCES` and that source's city list.
Setting only the city list does nothing.

Before a production deploy:

```bash
python -m tools.doctor --no-network
```

---

## Auto-booking

Available for Holland2Stay only. It signs in with the account you configure,
attempts eligible directly-bookable listings, and **stops at the payment URL** —
it never completes a payment.

OurDomain and Xior stay notify-only: their booking flows run through third-party
forms with anti-abuse protection that is not reliably automatable.

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
| A source shows `down` or `warn` on `/monitoring` | The card lists which rule fired. Thresholds and their rationale: [ARCHITECTURE.md §5.12](ARCHITECTURE.md). |
| Need logs from a specific time window | `/logs` filters server-side by keyword, level and `since`/`until` — it is not limited to what is currently on screen. |
| Logs repeat `H2S source 熔断` | Cloudflare is blocking your exit IP. The breaker pauses that one source and retries a single city later. If it persists, set `HTTPS_PROXY`. |
| Logs repeat `CF 挑战 ... 未解开` | Same cause — usually IP reputation rather than anything local. |
| One platform always returns 0 listings | May be genuine. Check whether the round was marked complete; see [ARCHITECTURE.md §5.7](ARCHITECTURE.md#57-completeness-决定能否做状态收敛). |
| Container gets OOM-killed | Raise `mem_limit`. Each Cloudflare-protected source holds a resident browser. |
| `supervisorctl` says "no such file" | Its socket is not at the default path. Add `-c /etc/supervisor/conf.d/app.conf`. |

The container reporting `healthy` covers both web and monitor, but an unhealthy
container is **not** restarted automatically — wire the health status into your
own monitoring if you want to be paged.

---

## Documentation

| Document | For |
|---|---|
| [User Guide](https://flatradar.app/guide) | Screenshots and daily use |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system runs, and every failure mode worth knowing before you debug one |
| [API.md](API.md) | Backend contracts for mobile and integrations |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [XIOR.md](XIOR.md) · [OURDOMAIN.md](OURDOMAIN.md) · [SCRAPING_RECON.md](SCRAPING_RECON.md) | Per-platform scraping research |
| [ANDROID_PLAN.md](ANDROID_PLAN.md) · [iOS_README.md](iOS_README.md) | Mobile client work |
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

See [LICENSE](../LICENSE).
