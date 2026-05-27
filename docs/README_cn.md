# FlatRadar 房源监控

[![Website](https://img.shields.io/badge/Website-flatradar.app-0057CC?style=flat-square)](https://flatradar.app) [![Guide](https://img.shields.io/badge/Guide-Chinese-10B981?style=flat-square)](https://flatradar.app/guide?lang=zh) [![Support](https://img.shields.io/badge/Support-help-64748B?style=flat-square)](https://flatradar.app/support) [![Sponsor](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/751K) [![License](https://img.shields.io/github/license/751K/holland2stay-monitor?style=flat-square)](../LICENSE) [![Release](https://img.shields.io/github/v/release/751K/holland2stay-monitor?style=flat-square)](https://github.com/751K/holland2stay-monitor/releases) [![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![iOS](https://img.shields.io/badge/iOS-SwiftUI-000000?style=flat-square&logo=apple&logoColor=white)](../ios/FlatRadar) [![Android](https://img.shields.io/badge/Android-Compose-3DDC84?style=flat-square&logo=android&logoColor=white)](../android)

> English version: [README.md](README.md)

FlatRadar 是一个可自托管的荷兰租房房源监控工具。它会监控支持的房源平台，跟踪新房源和状态变化，发送实时通知，并提供 Web 仪表盘、房源列表、地图、日历、图表和账号管理。

当前支持 **Holland2Stay**、**OurDomain** 和 **Xior**。Holland2Stay 支持可选自动预订流程；OurDomain 和 Xior 当前仅通知。

FlatRadar 是独立的非官方工具，与其监控的任何房源平台均无关联、背书、赞助、维护或运营关系。本项目仅供个人非商业使用，使用者需自行遵守各平台服务条款。

**官网：** [flatradar.app](https://flatradar.app)  
**使用指南：** [flatradar.app/guide](https://flatradar.app/guide?lang=zh)  
**支持页面：** [flatradar.app/support](https://flatradar.app/support)

## 它能做什么

- 监控 Holland2Stay、OurDomain 和 Xior 房源。
- 通过 Web、Telegram、Email、WhatsApp、iOS 推送和 Android 推送发送提醒。
- 每个用户可以独立配置筛选条件、通知渠道和账号设置。
- 提供房源列表、地图、日历、仪表盘和统计图表。
- 支持 guest、user、admin 三种角色。
- Web 面板支持中文和英文。
- 可为符合条件的 Holland2Stay 直订房源执行自动预订，并返回付款链接。
- 可本地运行、VPS Docker 部署，或使用预构建桌面版本。

## 支持的平台

| 平台 | 覆盖范围 | 说明 |
|---|---|---|
| Holland2Stay | Settings 中配置的荷兰城市 | 房源、通知、筛选和可选自动预订 |
| OurDomain | Amsterdam Diemen / South-East | 房源通知和仪表盘展示 |
| Xior | 荷兰 15 个城市 30 栋楼 | 房源通知和仪表盘展示 |

第三方平台页面和接口可能变化。做决定前，请始终在官方平台核实房源详情、价格、资格要求和可订状态。

## 应用和入口

| 入口 | 状态 | 说明 |
|---|---|---|
| Web 仪表盘 | 稳定 | 自托管和管理的主要入口 |
| iOS App | 维护阶段 | 已上架 App Store，当前产品范围的大功能已完成 |
| Android App | 开发中 | Kotlin + Compose，正在推进 parity 和 Play Store 准备 |
| 桌面版本 | 可用 | GitHub Releases 提供 macOS `.dmg` 和 Windows `.zip` |

[Download on the App Store](https://apps.apple.com/us/app/flarradar/id6769857080)

联系方式：[surrport@flatradar.app](mailto:surrport@flatradar.app)

> Demo 环境中已关闭 user 用户的自动预订功能。如需启用，请通过邮箱联系或[本地部署](#快速开始)。

## 快速开始

### Docker

Docker 推荐用于 VPS 或长期运行的家用服务器。

```bash
cp .env.example .env
mkdir -p data logs logs/caddy
# 公开部署前请先修改 Caddyfile 和 .env。
docker compose up -d
```

然后打开你的域名，登录，添加用户和通知渠道，选择监控平台/城市，并在 Dashboard 启动监控。

公开部署至少需要设置：

```env
WEB_PASSWORD=change-me
SESSION_COOKIE_SECURE=true
PUBLIC_BASE_URL=https://your.domain.com
SUPPORT_EMAIL=support@example.com
```

### 本地运行

```bash
pip install -r requirements.txt
cp .env.example .env
python web.py
```

打开 `http://127.0.0.1:8088`。

### 桌面版本

从 [GitHub Releases](https://github.com/751K/holland2stay-monitor/releases) 下载最新版本：

- macOS：`.dmg`
- Windows：`.zip`

## 通知渠道

FlatRadar 支持：

- Web 面板通知
- Telegram bot 消息
- Email，包括共享 Resend 发信模式
- WhatsApp，通过 Twilio
- iOS APNs 推送
- Android FCM 推送
- macOS 主机上的 iMessage

每个用户可以独立设置筛选条件和通知渠道。

## 自动预订

自动预订仅支持 Holland2Stay。FlatRadar 会使用配置好的用户账号尝试符合条件的直订房源，并停在付款链接，不会替你完成付款。

OurDomain 和 Xior 当前仅通知，因为它们的预订流程涉及第三方表单和反滥用保护。

## 配置

日常配置主要在 Web 面板完成：

- 监控平台、城市和楼盘；
- 轮询间隔和高峰期策略；
- 用户通知渠道；
- 用户筛选条件；
- 自动预订设置；
- 主题、语言和账号管理。

全局部署配置在 `.env` 中。请从 [.env.example](../.env.example) 复制后，按你的服务器和通知渠道修改。

生产部署前建议运行：

```bash
python -m tools.doctor --no-network
```

## 文档

- [使用指南](https://flatradar.app/guide?lang=zh)：截图和日常使用说明。
- [后端 API 文档](API.md)：移动端和集成接口契约。
- [Android 计划](ANDROID_PLAN.md)：当前 Android 开发状态。
- [iOS 维护说明](iOS_README.md)：iOS 发布检查和维护边界。
- [OurDomain 记录](OURDOMAIN.md)、[Xior 记录](XIOR.md)、[抓取侦察](SCRAPING_RECON.md)：平台专项研究。
- [Changelog](CHANGELOG.md)：详细版本历史。

## 支持开发

FlatRadar 是个人独立维护的开源项目。服务器、推送基础设施和 App Store 维护成本都由开发者承担。

- 如果项目对你有用，可以给仓库点 star。
- 可以通过 [GitHub Sponsors](https://github.com/sponsors/751K) 或 [flatradar.app/donate](https://flatradar.app/donate) 赞助。
- 支持和联系入口：[flatradar.app/support](https://flatradar.app/support)。

## 许可证

本项目使用 [LICENSE](../LICENSE) 中的许可证。
