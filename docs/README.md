# FlatRadar

[![Website](https://img.shields.io/badge/Website-flatradar.app-0057CC?style=flat-square)](https://flatradar.app) [![User Guide](https://img.shields.io/badge/Guide-docs-10B981?style=flat-square)](https://flatradar.app/guide) [![Support](https://img.shields.io/badge/Support-help-64748B?style=flat-square)](https://flatradar.app/support) [![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/751K) [![License](https://img.shields.io/github/license/751K/holland2stay-monitor?style=flat-square)](../LICENSE) [![Release](https://img.shields.io/github/v/release/751K/holland2stay-monitor?style=flat-square)](https://github.com/751K/holland2stay-monitor/releases) [![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![iOS](https://img.shields.io/badge/iOS-SwiftUI-000000?style=flat-square&logo=apple&logoColor=white)](../ios/FlatRadar) [![Android](https://img.shields.io/badge/Android-Compose-3DDC84?style=flat-square&logo=android&logoColor=white)](../android)

> Chinese version: [README_cn.md](README_cn.md)

FlatRadar is a self-hostable rental listing monitor for the Dutch housing market. It watches supported housing platforms, tracks new listings and status changes, sends real-time alerts, and provides a web dashboard with listings, map, calendar, charts, and account management.

It currently supports **Holland2Stay**, **OurDomain**, and **Xior**. Holland2Stay listings can optionally use the built-in auto-booking flow; OurDomain and Xior are notify-only.

FlatRadar is an independent, unofficial tool. It is not affiliated with, endorsed by, sponsored by, maintained by, or operated by any housing platform it monitors. Use it only for personal, non-commercial purposes and follow each platform's terms.

**Website:** [flatradar.app](https://flatradar.app)  
**User guide:** [flatradar.app/guide](https://flatradar.app/guide)  
**Support:** [flatradar.app/support](https://flatradar.app/support)

**Contact**: [surrport@flatradar.app](mailto:surrport@flatradar.app)

> The demo environment has auto-booking disabled for user accounts. To enable it, please contact us via email or [deploy locally](#quick-start).

## What It Does

- Monitors listings across Holland2Stay, OurDomain, and Xior.
- Sends alerts through Web, Telegram, Email, WhatsApp, iOS push, and Android push.
- Lets each user keep independent filters, notification channels, and account settings.
- Shows listings in list, map, calendar, dashboard, and chart views.
- Supports guest, user, and admin roles.
- Supports English and Chinese in the web dashboard.
- Can auto-book eligible Holland2Stay listings and return the payment URL.
- Runs locally, on a VPS with Docker, or as pre-built desktop packages.

## Supported Platforms

| Platform | Coverage | Notes |
|---|---|---|
| Holland2Stay | Dutch cities configured in Settings | Listings, alerts, filters, and optional auto-booking |
| OurDomain | Amsterdam Diemen / South-East | Listing alerts and dashboard views |
| Xior | 30 Dutch buildings across 15 cities | Listing alerts and dashboard views |

Platform coverage changes over time as third-party websites change. Always verify listing details, prices, eligibility, and booking status on the official platform before making decisions.

## Apps And Interfaces

| Interface | Status | Notes |
|---|---|---|
| Web dashboard | Stable | Primary admin and self-hosted interface |
| iOS app | Maintenance | Available on the App Store; feature-complete for current scope |
| Android app | Beta | Kotlin + Compose client, feature-complete (57 files, ~9.5k lines), FCM push verified, Play Store prep in progress |
| Desktop packages | Available | macOS `.dmg` and Windows `.zip` from GitHub Releases |

[Download on the App Store](https://apps.apple.com/us/app/flarradar/id6769857080) · [Download Android App](https://github.com/751K/holland2stay-monitor/releases/latest/download/app-release.aab)

## Quick Start

### Docker

Docker is recommended for a VPS or always-on home server.

```bash
cp .env.example .env
mkdir -p data logs logs/caddy
# Edit Caddyfile and .env before exposing the service publicly.
docker compose up -d
```

Then open your domain, sign in, add users and notification channels, choose monitored platforms/cities, and start the monitor from the dashboard.

For public deployments, set at minimum:

```env
WEB_PASSWORD=change-me
SESSION_COOKIE_SECURE=true
PUBLIC_BASE_URL=https://your.domain.com
SUPPORT_EMAIL=support@example.com
```

### Local Run

```bash
pip install -r requirements.txt
cp .env.example .env
python web.py
```

Open `http://127.0.0.1:8088`.

### Desktop Releases

Download the latest release from [GitHub Releases](https://github.com/751K/holland2stay-monitor/releases):

- macOS: `.dmg`
- Windows: `.zip`

## Notification Channels

FlatRadar can send alerts through:

- Web dashboard notifications
- Telegram bot messages
- Email, including the shared Resend sender mode
- WhatsApp through Twilio
- iOS push through APNs
- Android push through FCM
- iMessage on macOS hosts

Each user can have separate filters and channels.

## Auto-Booking

Auto-booking is available only for Holland2Stay. It uses the configured user account, attempts eligible directly bookable listings, and stops at the payment URL. It does not complete payment.

OurDomain and Xior remain notify-only because their booking flows involve third-party forms and anti-abuse protections.

## Configuration

Most day-to-day settings are managed from the web dashboard:

- monitored sources, cities, and buildings;
- polling interval and peak-hour behavior;
- user notification channels;
- user filters;
- auto-booking settings;
- theme, language, and account management.

Global deployment settings live in `.env`. Start from [.env.example](../.env.example), then edit values for your server and notification channels.

Before a production deploy, run:

```bash
python -m tools.doctor --no-network
```

## Documentation

- [User Guide](https://flatradar.app/guide) for screenshots and daily use.
- [Backend API Reference](API.md) for mobile and integration contracts.
- [Android Plan](ANDROID_PLAN.md) for current Android work.
- [iOS Maintenance Notes](iOS_README.md) for iOS release checks.
- [OurDomain Notes](OURDOMAIN.md), [Xior Notes](XIOR.md), and [Scraping Recon](SCRAPING_RECON.md) for source-specific research.
- [Changelog](CHANGELOG.md) for detailed release history.

## Contributing And Support

FlatRadar is a solo-driven open source project. Server costs, push infrastructure, and App Store maintenance are paid out of pocket.

- Star the repository if the project is useful.
- Sponsor development through [GitHub Sponsors](https://github.com/sponsors/751K) or [flatradar.app/donate](https://flatradar.app/donate).
- Use [flatradar.app/support](https://flatradar.app/support) for support and contact.

## License

This project is licensed under the terms in [LICENSE](../LICENSE).
