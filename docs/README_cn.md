# FlatRadar 房源监控

[![Website](https://img.shields.io/badge/Website-flatradar.app-0057CC?style=flat-square)](https://flatradar.app) [![Guide](https://img.shields.io/badge/Guide-Chinese-10B981?style=flat-square)](https://flatradar.app/guide?lang=zh) [![Support](https://img.shields.io/badge/Support-help-64748B?style=flat-square)](https://flatradar.app/support) [![Sponsor](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/751K) [![License](https://img.shields.io/github/license/751K/holland2stay-monitor?style=flat-square)](../LICENSE) [![Release](https://img.shields.io/github/v/release/751K/holland2stay-monitor?style=flat-square)](https://github.com/751K/holland2stay-monitor/releases) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![iOS](https://img.shields.io/badge/iOS-SwiftUI-000000?style=flat-square&logo=apple&logoColor=white)](../ios/FlatRadar) [![Android](https://img.shields.io/badge/Android-Compose-3DDC84?style=flat-square&logo=android&logoColor=white)](../android)

> English version: [README.md](README.md)

荷兰的房源常常几小时内就上线又消失。FlatRadar 帮你盯住关心的平台，一旦出现
符合过滤条件的房源立刻通知；对 Holland2Stay 还能在你刚看到通知时，就把预订
推进到支付页面。

可自部署：一个容器、一个 SQLite 文件，除了你自己选用的通知渠道之外不依赖任何
外部服务。

**官网：** [flatradar.app](https://flatradar.app) ·
**使用指南：** [flatradar.app/guide](https://flatradar.app/guide?lang=zh) ·
**支持：** [flatradar.app/support](https://flatradar.app/support) ·
**联系：** [surrport@flatradar.app](mailto:surrport@flatradar.app)

> FlatRadar 是独立的第三方工具，与所监控的任何房源平台均无隶属、背书、赞助或
> 合作关系。请仅用于个人非商业用途，并遵守各平台的服务条款。做决定前，请务必
> 到官方平台核实房源详情、价格、资格与预订状态。

---

## 能做什么

| | |
|---|---|
| **监控** | Holland2Stay、OurDomain、Xior，采用自适应轮询间隔，上架高峰时段自动加密 |
| **通知** | Web、Telegram、邮件、WhatsApp、iOS 推送、Android 推送、iMessage —— 每个用户独立选择渠道和过滤条件 |
| **视图** | 列表、地图、日历、仪表盘、图表，支持中英文 |
| **账号** | 访客 / 用户 / 管理员三种角色，各用户的过滤条件与凭据互相独立 |
| **自动预订** | 仅 Holland2Stay —— 边界见[自动预订](#自动预订) |

### 平台覆盖

| 平台 | 覆盖范围 | 预订 |
|---|---|---|
| Holland2Stay | 任意配置的荷兰城市 | 支持自动预订 |
| OurDomain | Amsterdam Diemen / South-East | 仅通知 |
| OurCampus | Amsterdam Diemen（1 栋） | 仅通知 |
| Xior | 14 个城市共 30 栋楼 | 仅通知 |

第三方站点会变，覆盖范围随之变化。各平台的抓取实现见
[XIOR.md](XIOR.md)、[OURDOMAIN.md](OURDOMAIN.md)、[SCRAPING_RECON.md](SCRAPING_RECON.md)。

### 客户端

| 入口 | 状态 |
|---|---|
| Web 面板 | 稳定 —— 自部署的主要入口 |
| [iOS App](https://apps.apple.com/us/app/flarradar/id6769857080) | 维护中 —— 已上架 App Store，当前范围内功能完整 |
| [Android App](https://github.com/751K/holland2stay-monitor/releases/latest/download/app-release.apk) | Beta —— 已签名 `.apk`，直接安装。FCM 推送已验证；尚未上架 Play Store |
| 桌面版 | macOS `.dmg` / Windows `.zip`，见 [Releases](https://github.com/751K/holland2stay-monitor/releases) |

---

## 环境要求

|  | 最低 | 说明 |
|---|---|---|
| 内存 | **2 GB** | 每个受 Cloudflare 保护的 source 会常驻一个 headless Chromium（各约 200–400 MB）。1 GB 只够跑单个 source。 |
| 磁盘 | 约 1.5 GB | 仅 patched Chromium 解压后就约 700 MB |
| Python | 3.11+ | Docker 镜像用 3.11；CI 与桌面版构建用 3.12 |
| 系统 | 推荐 Linux | macOS 的限制见下方[从源码运行](#从源码运行) |

---

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env
mkdir -p data logs logs/caddy
```

启动前先改 `.env`。任何公网可访问的部署至少要设置：

```env
WEB_PASSWORD=change-me
SESSION_COOKIE_SECURE=true
PUBLIC_BASE_URL=https://your.domain.com
SUPPORT_EMAIL=support@example.com
```

再把 `Caddyfile` 里的域名换成你自己的，然后：

```bash
docker compose up -d
```

确认两个进程都起来了——`monitor` 和 `web` 应该都是 `RUNNING`：

```bash
docker exec h2s supervisorctl -c /etc/supervisor/conf.d/app.conf status
```

首轮会比之后慢，因为要先过一次 Cloudflare 挑战（小型 VPS 上 10–35 秒）。可以
跟着日志看：

```bash
docker compose logs -f h2s
```

正常的一轮以 `本轮完整扫描: N/N 城市 (...)` 结尾（N 是配置的 source×城市
组合数），随后是 `本轮结束: ... 新房源`。

然后打开你的域名登录，添加用户、通知渠道和要监控的城市。

### 从源码运行

```bash
pip install -r requirements.txt
python -m cloakbrowser install   # patched Chromium，解压后约 700 MB
cp .env.example .env
python web.py
```

打开 `http://127.0.0.1:8088`。

> **macOS**：免费版 CloakBrowser 的 macOS 构建落后于 Linux，且 headless 模式
> 可能崩溃，所以本地运行会自动退回到可见窗口模式。真正依赖的部署请用
> Linux / Docker。

---

## 配置

日常设置——source、城市、间隔、过滤条件、通知渠道、自动预订、主题——都在 Web
面板里改。部署级设置在 `.env`，从 [.env.example](../.env.example) 开始，里面
对每个键都有说明。

先了解这几个：

| 键 | 默认值 | 作用 |
|---|---|---|
| `SOURCES` | `holland2stay` | 启用哪些平台，逗号分隔 |
| `CITIES` | `Eindhoven,29` | Holland2Stay 的城市，格式 `名称,id`，多个用 `\|` 分隔 |
| `OURDOMAIN_CITIES` / `OURCAMPUS_CITIES` / `XIOR_CITIES` | — | 其它 source 的同格式配置，楼栋 key 在各自 scraper 里 |
| `SHADOW_SOURCES` | — | 列出的 source 照常抓取入库但**不发任何通知**，用于新平台对用户开放前的静默验证 |
| `CHECK_INTERVAL` | `300` | 非高峰时段的轮询间隔（秒） |
| `PEAK_INTERVAL` | `60` | 高峰时段的轮询间隔（秒） |
| `MONITOR_HEARTBEAT_MAX_AGE` | `900` | monitor 静默多久后 `/health` 报 unhealthy |
| `HTTPS_PROXY` | — | Cloudflare 拦得紧时，让抓取走另一个出口 IP |

启用一个 source 需要**同时**配 `SOURCES` 和该 source 的城市列表。只配城市列表
不会生效。

上生产前建议跑一次预检：

```bash
python -m tools.doctor --no-network
```

---

## 自动预订

仅 Holland2Stay 可用。它用你配置的账号登录，尝试符合条件的可直订房源，
**停在支付 URL —— 不会完成支付**。

OurDomain 和 Xior 保持仅通知：它们的预订流程走第三方表单，带有反滥用保护，
无法可靠自动化。

> 线上 demo 对普通用户关闭了自动预订。需要的话请邮件联系，或[自行部署](#快速开始)。

---

## 排障

| 现象 | 原因与处理 |
|---|---|
| 完全没有通知，面板显示旧数据 | monitor 进程挂了。用 `supervisorctl status` 确认，再 `supervisorctl start monitor`。心跳超过 `MONITOR_HEARTBEAT_MAX_AGE` 后 `/health` 会返回 503。 |
| 日志反复出现 `H2S source 熔断` | Cloudflare 在拦你的出口 IP。熔断只暂停该 source，稍后用单个城市试探恢复。持续出现就配 `HTTPS_PROXY`。 |
| 日志反复出现 `CF 挑战 ... 未解开` | 同上，通常是 IP 信誉问题，不是本地配置问题。 |
| 某个平台一直 0 条 | 可能是真没房。先看该轮是否标记为完整扫描，参考 [ARCHITECTURE.md §5.7](ARCHITECTURE.md#57-completeness-决定能否做状态收敛)。 |
| 容器被 OOM 杀掉 | 调高 `mem_limit`。每个受 CF 保护的 source 会常驻一个浏览器。 |
| `supervisorctl` 报 "no such file" | 它的 socket 不在默认路径，命令要带 `-c /etc/supervisor/conf.d/app.conf`。 |

容器的 `healthy` 状态已经覆盖 web 和 monitor 两者，但 unhealthy **不会**自动
重启——想要告警，需要把健康状态接到你自己的监控上。

---

## 文档

| 文档 | 用途 |
|---|---|
| [使用指南](https://flatradar.app/guide?lang=zh) | 截图和日常使用 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统怎么跑，以及排障前值得先知道的所有失败模式 |
| [API.md](API.md) | 移动端与集成的后端契约 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |
| [XIOR.md](XIOR.md) · [OURDOMAIN.md](OURDOMAIN.md) · [SCRAPING_RECON.md](SCRAPING_RECON.md) | 各平台抓取侦察 |
| [ANDROID_PLAN.md](ANDROID_PLAN.md) · [iOS_README.md](iOS_README.md) | 移动端开发 |
| [dataflow_ch.mmd](dataflow_ch.mmd) · [dataflow_en.mmd](dataflow_en.mmd) | 完整抓取/通知流程的 Mermaid 图 |

---

## 支持开发

FlatRadar 由一个人开发和维护，服务器、推送基础设施和 App Store 费用都是自掏
腰包。

- 觉得有用的话给个 Star。
- 通过 [GitHub Sponsors](https://github.com/sponsors/751K) 或
  [flatradar.app/donate](https://flatradar.app/donate) 赞助。
- 问题反馈：[flatradar.app/support](https://flatradar.app/support)。

## 许可证

见 [LICENSE](../LICENSE)。
