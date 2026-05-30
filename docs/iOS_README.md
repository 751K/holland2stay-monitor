# FlatRadar iOS Maintenance Notes

The iOS app is feature-complete for the current FlatRadar product scope and is now in maintenance mode. Large feature development has moved to Android Play Store launch (A6), backend reliability, and multi-platform data quality. Android parity (A0–A5) is complete.

[Download on the App Store](https://apps.apple.com/us/app/flarradar/id6769857080)

## Current Scope

- Native SwiftUI app under `ios/FlatRadar/`.
- Connects to the shared Flask API under `/api/v1/*`.
- Supports admin, user, and guest roles.
- Covers Dashboard, Listings, Listing Detail, Map, Calendar, Notifications, Settings, Admin tools, legal pages, and StoreKit coffee donations.
- Uses APNs for iOS push notifications and SSE for live in-app updates.
- Conditional GET caching (URLCache 2MB memory + 20MB disk) with backend ETag/304 support (v1.7.10).
- Performance optimizations: DateFormatter static instances, featureMap key pre-normalization, non-blocking notification first screen, background map clustering (v1.7.10).
- Supports English and Simplified Chinese.

## Maintenance Policy

iOS should now receive:

- compatibility fixes for new iOS / Xcode releases;
- crash, navigation, notification, and API contract fixes;
- App Store metadata, privacy, and legal text updates;
- small UI polish that keeps parity with the shared product;
- security and dependency hygiene.

iOS should not be the default place for large new product experiments. New cross-platform behavior should first be specified in [API.md](API.md) and then implemented consistently across Web, Android, and iOS as needed.

## Quick Start

```bash
cd ios/FlatRadar
open FlatRadar.xcodeproj
```

Run the `FlatRadar` scheme on a simulator or physical device. The production app connects to `flatradar.app`; development builds can point to another server from Settings when that option is available.

## Architecture Pointers

```text
ios/FlatRadar/FlatRadar/
├── FlatRadarApp.swift          # app entry, environment injection
├── Models/                     # Codable API models and display helpers
├── Networking/                 # APIClient, APIError, SSE, Keychain
├── Stores/                     # @Observable state and business logic
├── Navigation/                 # tab/path/deep-link coordination
├── Push/                       # APNs delegate bridge
└── Views/                      # SwiftUI screens
```

Use [API.md](API.md) for the human-readable mobile API contract and [openapi.json](openapi.json) for the machine-readable contract shared by iOS and Android.

## Release Checklist

- Build with the current stable Xcode.
- Verify login, guest mode, listings/detail, map, calendar, notifications, settings, legal pages, and account deletion.
- Verify APNs registration, foreground notification handling, and notification deep links on a physical device.
- Confirm `PrivacyInfo.xcprivacy`, App Store privacy answers, support URL, terms, and privacy policy still match current behavior.
- Run the iOS unit test target when touching models, stores, navigation, networking, or push behavior.

## Ownership Notes

- Legal text is no longer maintained as an iOS-only source of truth. The backend legal API is canonical, with local app text used only as fallback.
- Listing and chart models should tolerate unknown fields. Backend additions should not require an iOS release unless the UI needs to expose the new data.
- APNs remains iOS-specific, but device registration uses the shared `platform` field so backend push routing can coexist with Android FCM.
