# FlatRadar 房源监控

> FlatRadar 是一个个人租房监控工具，跨平台跟踪新房源和状态变更，支持多用户推送通知和自动预订。目前已接入 **Holland2Stay**（GraphQL API）、**OurDomain**（RENTCafe HTML）和 **Xior**（WordPress AJAX JSON）。

> **免责声明：** 本项目仅供个人非商业使用。与 Holland2Stay 无任何关联、背书或合作关系。使用者需自行遵守 Holland2Stay 的服务条款及相关法律法规。作者对任何误用或因此产生的后果不承担任何责任。

**当前版本：** v1.7.0

**在线演示：** [flatradar.app](https://flatradar.app) — 注册账号或点击「访客模式」即可浏览。  
**使用指南：** [flatradar.app/guide](https://flatradar.app/guide) — 完整图文教程。

如果这个项目对你有帮助，欢迎 **点个 Star** ⭐ —— 让更多人看到 FlatRadar。

---

## 赞助 / 支持开发者

FlatRadar 是个人独立开发的纯开源项目。服务器、API 代理、推送通知等基础设施均由开发者自费承担。你的赞助直接帮助维持服务器运行和 iOS App 上架。

- **[GitHub Sponsors](https://github.com/sponsors/751K)** — 月捐或一次性赞助
- **[赞助页面](https://flatradar.app/donate)** — 支付宝 / 微信

---

## 快速开始
项目目前支持以下三种启用方式：Docker（推荐，适合 VPS / 服务器部署）、.dmg/.exe 文件本地运行与从源代码构建。
Docker 镜像预装了 Caddy 反向代理，自动申请 Let's Encrypt 证书，提供 HTTPS 访问；本地运行则直接使用 Flask 内置服务器，适合个人电脑使用。

**Docker（推荐）：**
```bash
cp .env.example .env && mkdir -p data logs logs/caddy
# 编辑 Caddyfile，将 your.domain.com 替换为你的真实域名
docker compose up -d
# 浏览器打开 https://your.domain.com → Dashboard → 点击「启动监控」
```

**macOS：**
从 [Releases](../../releases) 下载最新 `.dmg`，拖入 Applications，双击启动即可自动打开浏览器。持久化数据存储在 `~/.flatradar/`。

**Windows：**
从 [Releases](../../releases) 下载最新 `.zip`，解压后双击 `flatradar.exe`。CMD 窗口会自动打开浏览器。持久化数据存储在 `%USERPROFILE%\.flatradar\`。


**从源代码运行：**
```bash
pip install -r requirements.txt
cp .env.example .env
python web.py  # http://127.0.0.1:8088
```

**运行测试：**
```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

[完整安装指南 →](#本地启动)

## 项目状态

| 模块 | 状态 | 说明 |
|------|------|------|
| 多源抓取架构 | ✅ 已完成 | AbstractScraper + ScrapeTask dispatch；curl_cffi 指纹池 (Chrome/Safari/Edge) |
| H2S 数据抓取 | ✅ 已完成 | GraphQL API + curl_cffi Chrome 指纹绕过 Cloudflare WAF |
| OurDomain 抓取 | ✅ 已完成 | RENTCafe HTML 表格解析；单元级（房间号、面积、楼层、朝向、押金）；2 栋楼（Diemen + South-East） |
| Xior 抓取 | ✅ 已完成 | WordPress AJAX JSON；单元级（房号、面积、月租、押金、入住日期、预订链接）；荷兰 30 栋楼 |
| Stale 状态收敛 | ✅ 已完成 | 仅对完整扫描城市执行；直订/未知 7 天，lottery 2 天 |
| 多城市监控 | ✅ 已完成 | 26 个荷兰城市 (H2S) + Amsterdam (OurDomain) + 15 城市 (Xior) |
| 多通知渠道 | ✅ 已完成 | iMessage / Telegram HTML / Email HTML / WhatsApp |
| APNs 双语推送 | ✅ 已完成 | 中英文 `_T` 翻译表；按设备语言分组发送 |
| Web 面板通知 | ✅ 已完成 | SSE 实时铃铛 + Toast 弹窗，与平台无关 |
| 通知过滤 | ✅ 已完成 | 租金、面积、楼层、户型、入住类型、城市、片区、合同、租客、促销、装修、能耗 |
| 多选下拉过滤 | ✅ 已完成 | 下拉多选 + 中英双语标签；房源列表支持城市/租客/合同筛选 |
| 短租识别 | ✅ 已完成 | Contract / Tenant / Offer 标签；按合同类型 / 租客要求 / 促销过滤 |
| 跨平台构建 | ✅ 已完成 | GitHub Actions：推送 tag 自动构建 macOS .dmg + Windows .exe |
| Photon 地理编码 | ✅ 已完成 | Komoot Photon API 快速解析地图坐标；支持手动触发按钮 |
| 自动预订 | ✅ 已完成 | 全流程：加入购物车 → 下单 → 生成直链付款 URL |
| 快速预订通道 | ✅ 已完成 | 所有 Available to book 候选（新上线 + 状态变更）均在发通知前立即提交线程池 |
| Web 管理面板 | ✅ 已完成 | 仪表盘、房源列表、用户管理、全局设置 |
| 配置热重载 | ✅ 已完成 | 跨平台，修改后无需重启监控进程 |
| 智能轮询 | ✅ 已完成 | 双高峰窗口（早+下午），自适应加速，自动逼近速率上限 |
| 限流防护 | ✅ 已完成 | 429 指数退避重试 + 5 分钟冷却 + 代理支持 |
| Cloudflare 屏蔽检测 | ✅ 已完成 | 403 WAF 识别、节流告警（30 分钟 1 次）、15 分钟冷却、可操作恢复建议 |
| 多用户支持 | ✅ 已完成 | 每用户独立渠道 / 过滤 / 预订账号 |
| VPS / Docker 兼容 | ✅ 已完成 | 非 macOS 自动跳过 iMessage，Web 面板接管通知 |
| 日夜主题 | ✅ 已完成 | 浅色 / 深色，跟随系统偏好，无刷新闪烁 |
| 移动端适配 | ✅ 已完成 | 全页面自适应：卡片布局、44px 触摸目标、安全区适配、dvh 单位、日历列表切换、响应式网格 |
| 数据可视化 | ✅ 已完成 | 10 个图表，7/30/90 天范围会联动 KPI 卡片与分布图 |
| 入住日历 | ✅ 已完成 | 月视图，按城市筛选 |
| 地图视图 | ✅ 已完成 | Leaflet.js + OpenStreetMap，自动地理编码，颜色标记 |
| i18n 中英切换 | ✅ 已完成 | 一键切换语言，cookie 持久化 |
| 通知测试 | ✅ 已完成 | 一键逐渠道测试，返回成功 / 失败详情 |
| 访客模式（RBAC）| ✅ 已完成 | 无需密码只读访问；用户/设置/日志等管理功能仅 admin 可见 |
| 面板鉴权 | ✅ 已完成 | Session 登录，opt-in（设置密码后启用）；`WEB_GUEST_MODE` 控制访客入口 |
| Web 自助注册 | ✅ 已完成 | 登录页注册入口，注册前确认使用条款与隐私政策；SQLite 用户配置存储 |
| 登录爆破防护 | ✅ 已完成 | IP 级失败计数 + 指数退避延迟 |
| HTTPS / Caddy | ✅ 已完成 | 内置 Caddyfile + docker-compose Caddy 服务，自动签发 Let's Encrypt 证书 |
| 安全加固 | ✅ 已完成 | RBAC 装饰器、通知/SSE/geocode 访客屏蔽、CSRF、DOM XSS 防护、邮箱验证 `PUBLIC_BASE_URL` fail-closed |
| 启动预检 | ✅ 已完成 | `WEB_PASSWORD` 未设置或 Caddyfile 仍为占位域名时阻止容器启动 |
| 生产 WSGI | ✅ 已完成 | Docker 中 Gunicorn 替代 Flask 内置服务器（1 worker × 8 线程，timeout=0） |
| 依赖版本锁定 | ✅ 已完成 | `requirements.lock` 精确版本，Dockerfile 从 lock 文件安装，构建可重复 |
| 代码模块化 | ✅ 已完成 | web.py 拆分 `app/`（10 路由 + 8 共享）；`mcore/`（interval / prewarm / booking）；`mstorage/`（6 个 mixin 模块）；`monitor.py` 1,235→971，`storage.py` 1,177→17 re-export |
| Prewarm Session 缓存 | ✅ 已完成 | `mcore/prewarm.py` PrewarmCache 类；进程级缓存跨轮复用；TTL 后台刷新；用户/配置变更时自动失效 |
| 错误日志（errors.log）| ✅ 已完成 | 独立 WARNING+ 日志，含 `funcName:lineno` 格式；新增 web.log 记录 Flask 应用日志；日志查看器支持文件 Tab 切换、行号、级别着色、关键词搜索、自动滚动 |
| Pytest 测试套件 | ✅ 已完成 | 62 个测试模块（950 个测试），覆盖全栈：模型、mcore、mstorage、抓取（H2S/OD/XR）、预订、推送、通知、认证、CSRF、路由、i18n |
| 代码质量 | ✅ 已完成 | Literal 类型、共享常量、Mixin 组合 Storage、解析逻辑去重 |
| **iOS App (FlatRadar)** |
| iOS 认证与 RBAC | ✅ 已完成 | Admin / User / Guest 三档登录；Keychain token 持久化；全局 401/403 自动登出 |
| iOS Dashboard | ✅ 已完成 | 实时统计卡片 + sparkline 折线图；问候语横幅；2×2 探索网格含内联迷你图表（状态/价格/户型/能源）；匹配房源预览 |
| iOS 房源列表 | ✅ 已完成 | 分页列表 + 无限滚动 + 搜索；多维筛选（城市/状态/户型/合同/能源）；6 种排序；详情页含关键明细、监控历史、官方核实提醒 |
| iOS 地图 | ✅ 已完成 | 原生 MapKit + 自定义网格聚类；颜色编码 pin（绿/橙/灰）；集群气泡点击缩放；底部 sheet 卡片 → deep link 跳详情 |
| iOS 日历 | ✅ 已完成 | 月入住日历含每日可入住数量；月份切换；选中日房源列表 → 详情 |
| iOS 通知 | ✅ 已完成 | 卡片式 inbox，TODAY/YESTERDAY/EARLIER 三分区；SSE 实时流 + 指数退避重连；滑动已读；未读 badge；APNs 推送（sandbox/production 自动切换） |
| iOS 设置 | ✅ 已完成 | 通知过滤器编辑器（10 维度多选）；推送权限与设备注册；测试推送（admin）；主题切换（浅色/深色/系统）；账户管理（注销/删除账号）；法律文档（使用条款 + 隐私政策） |
| iOS 管理面板 | ✅ 已完成 | 用户管理列表（启用/禁用/删除）；监控进程控制（启动/停止/重载 + 状态查看） |
| iOS 自适应布局 | ✅ 已完成 | iPhone：4 tab + Browse 分段选择器（列表/地图/日历）；iPad：6 tab 直接展开展开；键盘快捷键 ⌘1-6 |
| iOS Deep Link | ✅ 已完成 | `h2smonitor://listing/<id>` URL Scheme；APNs 通知点击 → 房源详情 |
| iOS 设计系统 | ✅ 已完成 | 主色 #0A84FF；语义色 #34C759 / #FF9500 / #FF3B30；所有 KPI/价格/时间戳使用 tabular-nums；元数据使用 SF Mono |
| iOS 深色模式 | ✅ 已完成 | 全页面自适应（登录 Hero / Dashboard 卡片 / Settings）；overscroll 颜色跟随 |
| iOS 多语言 | ✅ 已完成 | 174 条本地化字符串（en / zh-Hans）；覆盖所有 UI 文本、错误消息、标签 |
| iOS 用户注册 | ✅ 已完成 | 用户自助注册，bcrypt 密码哈希；后端限流（每小时每 IP 3 次）+ 冲突检测；注册即登录 |
| iOS 账号注销 | ✅ 已完成 | DELETE /me 端点；双重确认弹窗；撤销所有 token + 删除 SQLite 用户配置 |
| iOS 法律合规 | ✅ 已完成 | 首次启动 Terms 强制同意弹窗（不可跳过）；Settings 和 Login 内嵌完整使用条款和隐私政策 |
| iOS StoreKit | ✅ 已完成 | "请我喝杯咖啡" 内购（3 档 consumable：Espresso/Latte/Flat White）；StoreKit 2 交易监听 |
| iOS 安全 | ✅ 已完成 | ATS HTTPS-only；Keychain token 存储；bcrypt 密码哈希；全部 `print()` 使用 `#if DEBUG` 守卫；TTL 上限 90 天；用户名长度限制 64 字符 |
| iOS App Store 就绪 | ✅ 已完成 | PrivacyInfo.xcprivacy（UserDefaults CA92.1 + 数据收集声明）；App 图标；Info.plist 配置 |

---

## iOS App (FlatRadar)

原生 iOS 客户端，基于 SwiftUI + StoreKit 2 构建。Xcode 项目位于 `ios/FlatRadar/`。

[![Download on the App Store](https://developer.apple.com/assets/elements/badges/download-on-the-app-store.svg)](https://apps.apple.com/us/app/flarradar/id6769857080)

### 快速启动（iOS）

```bash
cd ios/FlatRadar
open FlatRadar.xcodeproj
# 选择 "FlatRadar" scheme → 模拟器或真机运行
```

默认连接 `flatradar.app`。本地调试时在 Settings 中修改 Server URL。

### 架构

```
FlatRadar/
├── Models/          # 数据模型（Listing, AuthModels, ChartData 等）
├── Networking/      # APIClient, SSE Client, KeychainManager, APIError
├── Stores/          # @Observable 状态对象（Auth, Listings, Dashboard 等）
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
├── Navigation/      # NavigationCoordinator + deep link 路由
├── Push/            # PushDelegate (APNs 桥接)
└── FlatRadarApp.swift # @main 入口，环境注入
```

### 关键功能

- **3 种角色**：Admin（监控控制）、User（过滤 + 推送）、Guest（只读浏览）
- **Dashboard**：实时统计卡片含 sparkline、2×2 Explore 网格含内联迷你图表、匹配房源预览
- **房源**：分页浏览、多维筛选、6 种排序、详情页
- **地图**：原生 MapKit + 自定义聚类、颜色 pin、点击缩放
- **日历**：月视图入住日历、每日可入住数量
- **通知**：卡片 inbox + 三区分类、SSE 实时流、APNs 推送
- **自助服务**：注册、过滤编辑、账号注销
- **捐赠**："请我喝杯咖啡" StoreKit 2 IAP（3 档，不绑定功能）
- **法律合规**：首次启动强制 Terms 弹窗、内嵌完整使用条款和隐私政策
- **多语言**：英文 / 简体中文（174 条本地化字符串）
- **深色模式**：全页面自适应
- **设计系统**：主色 #0A84FF、语义色（绿/橙/红）、tabular-nums 数字字体

### API 端点

完整移动端 API 契约见：[后端 API 文档](API.md)。iOS/Android 共用的机器可读 OpenAPI 3.1 契约见：[openapi.json](openapi.json)。

所有端点使用 `/api/v1/*` + JWT Bearer 鉴权（或 bearer_optional）：

| 端点 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/auth/login` | POST | 无 | 登录（admin/user 通过 bcrypt 或 H2S 凭据） |
| `/auth/register` | POST | 无 | 自助注册（注册即登录） |
| `/auth/logout` | POST | 需要 | 撤销当前 token |
| `/auth/me` | GET | 需要 | 当前身份 + 用户信息 |
| `/stats/public/summary` | GET | 可选 | 实时统计 |
| `/stats/public/charts/<key>` | GET | 可选 | 图表数据 |
| `/listings` | GET | 可选 | 分页列表含过滤 |
| `/map` | GET | 可选 | 全部房源含坐标 |
| `/calendar` | GET | 可选 | 入住日期房源 |
| `/notifications` | GET | 需要 | 用户通知列表 |
| `/notifications/stream` | GET (SSE) | 需要 | 实时通知流 |
| `/me/summary` | GET | 需要 | 过滤匹配统计 |
| `/me/filter` | GET/PUT | 需要 | 读取/更新通知过滤 |
| `/me` | DELETE | 需要（user） | 注销账号 |
| `/filter/options` | GET | 可选 | 过滤维度候选项 |
| `/devices/register` | POST | 需要 | 注册 APNs token |
| `/admin/users` | GET | admin | 用户列表 |
| `/admin/users/<id>/toggle` | POST | admin | 启用/禁用用户 |
| `/admin/users/<id>` | DELETE | admin | 删除用户 |
| `/admin/monitor/*` | GET/POST | admin | 监控进程控制 |

---

## 核心功能

### 多平台抓取

FlatRadar 通过统一的 `AbstractScraper` 接口监控三个房源平台：

| 平台 | 数据源 | 方式 | 粒度 |
|---|---|---|---|
| **Holland2Stay** | GraphQL API | `curl_cffi` Chrome 指纹 | 单元级（具体房号） |
| **OurDomain** | RENTCafe HTML | `curl_cffi` Safari 指纹 | 单元级（#6045, 面积, 楼层, 朝向） |
| **Xior** | WordPress AJAX JSON | `curl_cffi` + 1.5s 间隔 | 单元级（M1.30.53, 面积, 租金, 押金） |

- 每个平台实现 `AbstractScraper.scrape(task)` → 产出带 `source` 标签的 `Listing`
- `dispatch_scrape_tasks()` 按 source 路由，隔离单平台故障，合并结果
- 所有 scraper 共享 `RATE_LIMIT_BACKOFF`、`is_cloudflare_body` 和 429 重试逻辑

### 智能自适应轮询

- 双高峰窗口（默认早 8:30–10:00 和下午 13:30–15:00，仅工作日）
- 自适应间隔：成功 ×0.95（逐步逼近上限），限流 ×2.0（退避），下限 15s
- 随机 ±`JITTER_RATIO` 抖动防机械指纹
- 所有参数 Web 面板可配

### 限流与屏蔽防护

- **429**：退避重试 30s/60s → `RateLimitError` → 5 分钟冷却
- **403 Cloudflare WAF**：立即 `BlockedError`（不重试）→ 节流告警（1/30min）→ 15 分钟冷却 + 恢复建议
- **代理**：`.env` 设 `HTTPS_PROXY` / `HTTP_PROXY`，热重载生效

### 通知推送

- **渠道**：iMessage、Telegram、Email、WhatsApp —— 每用户独立，多渠道并行
- **iOS 推送**：APNs 双语支持（中英 `_T` 翻译表，按设备语言分组发送）
- **Web 面板**：SSE 实时铃铛 + Toast，全平台可用
- **过滤**：每用户 10 维度（租金、面积、楼层、户型、入住类型、城市、平台、合同、能耗等）

### 自动预订（仅 Holland2Stay）

- 全 GraphQL 流程：登录 → `createEmptyCart` → `addNewBooking` → `placeOrder` → 付款直链
- **快速通道**：预订在通知发送**之前**提交到线程池——早到服务器 1–2 秒
- **Prewarm 缓存**：登录 session 跨轮复用，配置变更自动失效
- Dry Run 验证模式；竞败重试队列

### Web 管理面板

- **仪表盘**：KPI 卡片、筛选芯片、房源表格、48h 状态变更时间线
- **房源列表**：多维度筛选（状态/城市/平台/户型/租金/能耗），可排序
- **地图**：Leaflet.js + OpenStreetMap，颜色标记，自动地理编码
- **日历**：月入住视图，按城市筛选
- **统计**：10 个 Chart.js 图表，7/30/90 天范围切换
- **用户**：CRUD、启停、逐用户渠道/过滤/预订配置
- **设置**：全局轮询、自适应参数、平台城市勾选、保存即生效
- **i18n**：一键中英切换，cookie 持久化
- **RBAC**：Admin / User / Guest 三级；自助注册 + 条款确认
- **主题**：浅色/深色，跟随系统，无闪烁
- **用户管理**：多用户 CRUD，每用户独立配置通知 / 过滤 / 预订，一键启停，一键发送测试通知
- **全局设置**：轮询间隔、自适应轮询参数（双窗口）、心跳间隔、监控城市，可视化配置无需手动编辑 `.env`
- **立即生效**：保存后点击按钮热重载配置，监控进程不中断
- **访客模式**：登录页"访客模式"按钮，无需密码只读浏览；用户/设置/系统/日志等管理页面仅 admin 可见；设 `WEB_GUEST_MODE=false` 关闭入口
- **中英切换**：侧边栏一键切换中文 / English，cookie 持久化
- **铃铛通知**：侧边栏实时通知 Bell + Toast 弹窗，SSE 推送，一键全部已读
- **极简设计**：去边框设计系统，阴影层级区分，深色/浅色双主题 + 平滑过渡动画，Inter 字体

---

## 技术架构

### 数据流

```mermaid
flowchart TD
    A["启动 monitor.py"] --> B["读取配置 .env / SQLite user_configs"]
    B --> B1["Config.scrape_tasks_v2()\nSOURCES / CITIES / OURDOMAIN_CITIES / XIOR_CITIES"]
    B1 --> C["创建 Storage / Notifier / WebNotifier"]
    C --> D["进入 main_loop 轮询"]

    D --> E["run_once()"]
    E --> F["预热 H2S 自动预订登录 session"]
    E --> G["dispatch_scrape_tasks(tasks)\n按 source 路由 ScrapeTask"]

    subgraph S["多平台抓取层 scrapers/"]
        G --> H1["holland2stay:Eindhoven...\nHollandStayScraper"]
        G --> H2["ourdomain:Diemen / South-East\nOurDomainScraper"]
        H2 --> H21["RENTCafe floorplans.aspx\n发现 floorplan ids"]
        H21 --> H22["rcLoadContent.ashx availableunits\n单元级房源"]
        H22 --> H23["解析 Unit / Area / Floor / Date\nsource = ourdomain"]
        H2 --> H24{"403 Cloudflare?"}
        H24 -->|是| H25["切换 TLS 指纹\nOURDOMAIN_IMPERSONATES"]
        H25 --> H21

        G --> H3["xior:Eindhoven Kronehoefstraat...\nXiorScraper"]
        H3 --> H31["POST admin-ajax.php\nyardi_room_availability"]
        H31 --> H32["解析 JSON 响应\napartmentId / sqm / rent"]
        H32 --> H33["单元级房源\nsource = xior"]
        H3 --> H34{"429 限流?"}
        H34 -->|是| H35["退避重试 RATE_LIMIT_BACKOFF\n请求间 1.5s 间隔"]
        H35 --> H31
    end

    H1 --> I{"抓取结果"}
    H23 --> I
    H24 -->|全部指纹失败| I
    H33 --> I
    H34 -->|重试耗尽| I

    I -->|至少一个任务成功| J["合并 listings + completeness\nsource:city key 防同名覆盖"]
    I -->|全部任务网络失败| K["ScrapeNetworkError\n连续失败计数 / cooldown"]
    I -->|全部任务 403/429| L["BlockedError / RateLimitError\nmain_loop 冷却"]
    I -->|单平台失败| M["记录 completeness=false\n隔离失败平台"]

    M --> J
    J --> N["storage.diff(fresh)\nsource-aware upsert / 状态变化"]
    N --> O["mark_stale_listings()\n按 source:city 完整扫描结果收敛"]
    O --> P["更新 last_scrape_at / last_scrape_count"]

    P --> Q["发送通知\nWeb / Telegram / Email / APNs"]
    Q --> Q1["标题/正文带平台 badge\nH2S / OD / XR"]
    Q1 --> R["记录已通知状态"]

    P --> T["扫描自动预订候选"]
    T --> U{"listing.source == holland2stay\n且可直接预订?"}
    U -->|是| V["立即提交 try_book()\n线程池并发执行"]
    U -->|否| W["跳过自动预订\nOD / XR 仅通知不预订"]

    V --> X["booker.py\n登录/复用预热 session"]
    X --> Y["createEmptyCart → addNewBooking\n→ placeOrder → idealCheckOut"]
    Y --> AE{"预订结果"}
    AE -->|成功| AF["发送 Booking 成功通知\n包含付款链接"]
    AE -->|失败| AG["发送失败原因\n可进入重试队列"]
    AF --> R
    AG --> R

    R --> AH["本轮结束"]
    W --> AH
    K --> AI["等待下一轮"]
    L --> AI
    AH --> AI
    AI --> D

    subgraph WEB["网页 / API 读取路径"]
        N --> WEB1["listings 表含 source 字段"]
        WEB1 --> WEB2["dashboard / listings / map / calendar\n显示 H2S / OD / XR badge"]
        WEB1 --> WEB3["/api/v1/listings?source=...\nfilter/options.sources"]
        WEB1 --> WEB4["stats source_dist 平台分布"]
    end
```

### 模块职责

| 文件 | 职责 |
|------|------|
| `monitor.py` | 主调度循环，自适应智能轮询（双高峰窗口），热重载，prewarm 缓存，并发预订，多源 dispatch |
| `scrapers/__init__.py` | `SCRAPER_REGISTRY`，`dispatch_scrape_tasks()` — 按 source 路由 ScrapeTask，合并结果，隔离故障 |
| `scrapers/base.py` | `AbstractScraper` ABC，`ScrapeTask`/`ScrapeResult` 数据类，共享异常 |
| `scrapers/holland2stay.py` | `HollandStayScraper`：GraphQL API，curl_cffi Chrome 指纹，翻页，429 退避，代理 |
| `scrapers/ourdomain.py` | `OurDomainScraper`：RENTCafe 两阶段抓取（floorplans→availableunits），单元去重，楼层/日期解析，Safari 指纹 |
| `scraper.py` | 向后兼容 re-export + 旧 `scrape_all()` |
| `storage.py` | SQLite 持久化 re-export（`mstorage/`） |
| `mstorage/_listings.py` | diff 检测，房源查询，状态计数，stale 收敛 |
| `models.py` | `Listing` dataclass（含 `source` 多源字段），price_display，feature_map |
| `notifier.py` | BaseNotifier ABC，iMessage/Telegram/Email/WhatsApp/WebNotifier，MultiNotifier |
| `mcore/push.py` | APNs 推送：按设备语言分组，`_T` 中英翻译表，频率/去重节流 |
| `mcore/booking.py` | 预订编排：`book_with_fallback()`，`RetryQueue`，`area_key` |
| `mcore/interval.py` | 自适应轮询间隔 + 抖动 |
| `mcore/prewarm.py` | `PrewarmCache`：进程级 session 缓存，TTL 刷新 |
| `booker.py` | PrewarmedSession；createEmptyCart → addNewBooking → placeOrder (store_id) → idealCheckOut (plateform "h")；增强错误上下文（sku/contract_id/start_date）；cancel_enabled 代理支持 |
| `config.py` | 全局配置加载，KNOWN_CITIES（26 城市），ListingFilter，AutoBookConfig |
| `users.py` | UserConfig dataclass，SQLite `user_configs` 读写，旧 `users.json` 一次性迁移 |
| `web.py` | Flask app 引导层：实例化、安全头、CSRF、Jinja 过滤器、context processor、路由注册、Web 进程日志 |
| `app/auth.py` | Session 鉴权、RBAC 装饰器（`login_required`、`admin_required`、`admin_api_required`）、访客模式、登录限流 |
| `app/csrf.py` | CSRF token 生成与校验（Unicode 安全，`.encode("utf-8")` 防 TypeError）|
| `app/db.py` | 数据库连接工厂 `get_db()` |
| `app/env_writer.py` | `.env` 文件原地写键（规避 `dotenv.set_key()` 在 Docker bind mount 上的 rename 错误） |
| `app/forms/user_form.py` | 用户表单数据提取与 `UserConfig` 构造 |
| `app/i18n.py` | 语言检测、cookie 持久化、选项本地化 |
| `app/jinja_filters.py` | Jinja2 自定义过滤器 |
| `app/process_ctrl.py` | 监控进程生命周期管理（启动/停止/重载/PID） |
| `app/safety.py` | 安全响应辅助 |
| `app/routes/dashboard.py` | 仪表盘：首页、图表 API、房源搜索；`get_distinct_cities()` 修复城市列表截断 bug |
| `app/routes/calendar_routes.py` | 入住日历视图与数据 API |
| `app/routes/map_routes.py` | 地图视图、geocode 缓存 API、片区 API |
| `app/routes/notifications.py` | 通知列表、全部已读、SSE 事件流 |
| `app/routes/control.py` | 监控控制：启动/停止/关闭/重载 |
| `app/routes/sessions.py` | 登录/登出/访客入口 |
| `app/routes/settings.py` | 全局设置：查看、保存、过滤选项 API |
| `app/routes/stats.py` | 统计图表数据 API |
| `app/routes/system.py` | 系统信息、日志查看器（文件 Tab：monitor/errors/web、行号、级别着色、关键词搜索）、清空日志、健康检查、日志文件列表 API |
| `app/routes/users.py` | 用户 CRUD、启停、通知测试 |
| `translations.py` | 120+ UI 翻译条目（中/英），模板 `_()` 函数 |
| `tools/geocode_all.py` | 一次性 Nominatim 地理编码，预热坐标缓存 |
| `static/` | `design.css`（去边框设计系统），`app.js`（主题/导航/SSE/国际化） |
| `templates/` | Jinja2 模板（`_()` 国际化），Leaflet.js 地图，Chart.js 图表，侧边栏布局 |

### 关键技术决策

| 问题 | 方案 | 原因 |
|------|------|------|
| 多平台数据源 | `AbstractScraper` + `ScrapeTask`/`ScrapeResult` 协议 | 每个平台实现 `scrape(task) → ScrapeResult`；`dispatch_scrape_tasks()` 按 source 路由，隔离故障，合并结果 |
| H2S Cloudflare 403 | `curl_cffi` + `impersonate="chrome*"` | TLS 层模拟 Chrome 指纹，无需浏览器 |
| OurDomain RENTCafe CF | `curl_cffi` + `impersonate="safari17_0"` | Safari 指纹可过 RENTCafe 的 POST 请求；Chrome 被拦 |
| RENTCafe reCAPTCHA | 第三方解决服务（capsolver/2captcha） | HTTP API 返回 v3 token，1–15s，无需 Playwright |
| H2S 页面无房源数据 | 直接请求 GraphQL API | Next.js + Apollo CSR，HTML 中无房源 DOM |
| OurDomain 无 API | 解析 HTML 表格 + `data-selenium-id` 锚点 | RENTCafe 是 ASP.NET 服务端渲染；Yardi 留有测试锚点 |
| 单元去重 (OurDomain) | `_merge_unit()` 按 `unit_id` 跨 FP 去重 | 同一物理单元可签约多种合同类型 |
| 预订竞争条件 | 通知前先提交 `try_book()` 到线程池 | 预订与通知并发执行 |
| 重复登录开销 | `PrewarmedSession` 缓存 + TTL 刷新 | 进程级缓存，跨轮复用，与抓取并行 |
| API 限流 | 429 退避 + 5 分钟冷却 + 自适应降速 | 三层防御 |
| Cloudflare 403 WAF | 立即抛 `BlockedError` + 节流告警 + 15 分钟冷却 | 403 持续性封禁，等待无效 |
| iOS 双语推送 | `_T` 翻译表 + `_send_to_user` 按语言分组 | 每语言组独立 payload；`_t(text, lang)` 查表 |
| 异步通知 + 同步抓取 | `run_in_executor` 桥接 | scraper sync，notifier async |
| 多渠道通知 | `BaseNotifier` ABC + `MultiNotifier` | 统一格式化，子类实现 `_send()` |
| iMessage 非 macOS | `is_macos()` 检测 + 跳过 | 清晰警告，优雅降级 |
| SQLite 并发访问 | WAL journal mode | monitor 写 web_notifications，web.py 独立连接只读，互不阻塞 |
| 配置热重载 | SIGHUP → asyncio.Event（Unix）/ reload 文件轮询（Windows） | 修改后配置立即生效，监控进程不中断 |
| 多用户存储 | SQLite `user_configs` | 单一真实数据源，事务写入，支持并发注册/编辑 |
| 主题切换无闪烁 | `<head>` 内联脚本 + CSS custom properties | 在 CSS 渲染前同步设置 `data-bs-theme`，避免 FOUC |
| 面板鉴权 opt-in | `WEB_PASSWORD` 为空则跳过鉴权 | 本地运行无需配置，对外暴露时一行配置即可加锁 |
| web.py 单体膨胀（1,200+ 行） | 拆分为 `app/routes/`（10 个路由模块）+ `app/`（auth、csrf、db、i18n 等） | 每个模块 15–240 行，职责单一；`web.py` 精简为 154 行引导层；路由用 `add_url_rule` 保留扁平 endpoint 名，模板零改动 |
| Prewarm Session 每轮浪费 | 进程级缓存 + 智能 TTL 刷新；跨轮复用 | 命中：零网络 IO；TTL < 300 s：后台刷新（与抓取并行）；仅 email 变更 / unknown_error 失效 |
| INFO 噪音淹没告警 | 独立 `errors.log`（WARNING+），含 `funcName:lineno` 格式，backupCount=5 | `monitor.log` 保留 INFO+ 运维视图；`errors.log` 归档稀疏但可操作的异常，精确定位源 |
| 无自动化测试 | 10 个 pytest 模块，共享 fixture（`temp_db`、`client`、`admin_client` 等） | 纯函数测试覆盖 models/crypto/safety/storage；HTTP 集成测试覆盖 auth/user/log 路由；零外部网络依赖 |

### H2S GraphQL API

| 参数 | 值 |
|------|-----|
| 端点 | `POST https://api.holland2stay.com/graphql/` |
| 分类 ID | `category_uid: "Nw=="` (Residences) |
| 可直接预订 | `available_to_book: { in: ["179"] }` |
| 摇号中 | `available_to_book: { in: ["336"] }` |
| 自定义属性 | `custom_attributesV2` → `price`（总租金含服务费）/ `living_area` / `floor` / `available_startdate` 等 |

### OurDomain / RENTCafe 数据

| 参数 | 值 |
|------|-----|
| 基础 URL | `https://thisisourdomain.securerc.co.uk/onlineleasing/` |
| 阶段 1 | `floorplans.aspx` → 提取 `subPointerId`（户型 ID） |
| 阶段 2 | `rcLoadContent.ashx?contentclass=availableunits&floorPlans={id}&MoveInDate={date}` → 单元行 |
| 单元字段 | `data-selenium-id="Apt{N}"`（房间号）、`SqFt`（面积 m²）、`Rent`（月租 €）、`Deposit`（押金）、`Amenity`（楼层/朝向）、`AvailDate`（可租状态） |
| 状态映射 | `<span class="text-success">Available</span>` → 可预订 / `<span class="text-warning">Wait List` → 可抽签 |
| 去重 | `_merge_unit()` 按数字 `unit_id` 去重——同一房间可签不同合同类型 |
| 自动预订 | ASP.NET 多步表单 POST → `rcformsave.ashx`；受 reCAPTCHA v3+v2 保护（详见 [OURDOMAIN.md](OURDOMAIN.md) §10） |

---

## 快速开始

### 安装

```bash
# 要求 Python 3.11+
pip install -r requirements.txt
cp .env.example .env
```

### 本地启动

```bash
# 1. 验证抓取是否正常（不写库、不发通知）
python monitor.py --test

# 2. 启动 Web 面板 — 唯一需要运行的命令
python web.py              # http://127.0.0.1:8088
#    进入 Dashboard 点击「启动监控」即可开始监控。
#    监控的启停、关闭都可以在 Web 面板中操作，无需 SSH 或手动管理后台进程。

# 3. 也可单独命令行运行（单次）
python monitor.py --once
```

Web 面板 Dashboard 提供 **启动监控 / 停止监控 / 关闭** 三个按钮，无需手动管理进程。

> **提示**：首次启动时，打开 Web 面板点击「新增用户」创建第一个用户，配置通知渠道和过滤条件即可。

### Docker 部署（VPS / 服务器）

要求：Docker + Docker Compose v2

内置的 `docker-compose.yml` 同时运行 **Caddy + h2s**。Caddy 负责 HTTPS（Let's Encrypt 自动证书），是唯一的外部入口——h2s 容器的 8088 端口仅在 Docker 内网可达，**不映射到宿主机**。

**启动前必做的两步：**

1. **修改 `Caddyfile`**，将占位域名替换为你自己的：
   ```
   your.domain.com {
       reverse_proxy h2s:8088
       ...
   }
   ```

2. **修改 `.env`**，设置密码并启用安全 Cookie：
   ```env
   WEB_PASSWORD=你的密码
   SESSION_COOKIE_SECURE=true
   ```

同时将域名 DNS A 记录指向 VPS IP，确保 80 和 443 端口对外可达（ACME 验证需要）。

**启动：**
```bash
cp .env.example .env   # 然后按上面说明编辑
mkdir -p data logs logs/caddy
docker compose up -d

# 查看日志
docker compose logs -f

# 停止
docker compose down
```

**发布前健康检查：**

首次部署、修改 `.env` 后、或发布新版本前，建议先运行 doctor：

```bash
python -m tools.doctor

# 离线 / CI 安全模式：跳过代理、SMTP 和 Holland2Stay 网络探测
python -m tools.doctor --no-network

# 额外验证 SMTP 用户名/密码登录（不会真正发送邮件）
python -m tools.doctor --smtp-login
```

doctor 会检查 `.env`、data/logs 路径权限、SQLite 完整性和必要表、
SQLite 用户配置、Caddy 占位域名、APNs key/config、SMTP 配置、代理出口 IP、
Holland2Stay GraphQL 连通性，以及 Docker supervisord 控制能力。发现阻断性
`FAIL` 时退出码为 `1`，可以安全接入部署脚本。

**Docker 环境启用代理：**

如果需要通过代理路由抓取和预订流量（例如用住宅代理规避 Cloudflare 403 封禁），需要在**两个位置**传入代理变量：

1. **`.env`** — 设置代理地址供程序运行时读取：
   ```env
   HTTPS_PROXY=http://user:pass@代理地址:端口
   # 或 HTTP_PROXY（如果代理走 HTTP）
   ```

2. **`docker-compose.yml`** — 在 `services.h2s.environment` 下添加，将变量从宿主机传入容器：
   ```yaml
   environment:
     - TZ=Europe/Amsterdam
     - PYTHONUNBUFFERED=1
     - HTTP_PROXY=${HTTP_PROXY}
     - HTTPS_PROXY=${HTTPS_PROXY}
     - ALL_PROXY=${ALL_PROXY}
   ```

   `${VAR}` 语法会从宿主机 shell 或同目录下的 `.env` 文件读取值（docker compose 默认读取 `.env`）。编辑后运行 `docker compose up -d` 重建容器即可生效。

容器内 `monitor.py` 和 `web.py` 由 supervisord 同时管理，崩溃自动重启，日志写入宿主机 `./logs/`。容器以非 root 用户 `appuser` 运行，`mem_limit: 512M` + `cpus: 1.0` 防止资源耗尽。

**首次配置流程：**
1. 打开 `https://你的域名` 并登录
2. 进入「用户管理」添加第一个用户，渠道选 Telegram 或 Email（iMessage 需要 macOS，非 Mac 环境自动跳过）
3. 进入「设置」选择要监控的城市
4. 点击「立即生效」热重载配置，无需重启容器

**更新版本：**
```bash
git pull
python -m tools.doctor --no-network
docker compose up -d --build
```

### 配置说明

**用户级别的配置**（通知渠道、过滤条件、自动预订）在 Web 面板 → 用户管理 中设置，存储在 SQLite `user_configs`。
旧版 `data/users.json` 会按 `users_storage_migrated_v1` meta flag 一次性导入，并永久保留 `.bak` 迁移备份。

**全局配置**可通过 Web 面板 → 全局设置，或直接编辑 `.env`：

```env
# ── Web 面板鉴权（可选）──────────────────────────────────────────
WEB_USERNAME=admin          # 默认 admin
WEB_PASSWORD=               # 留空则无需登录；填写后访问面板须先登录
FLASK_SECRET=               # 留空则自动生成并写入 .env

# ── 抓取 ────────────────────────────────────────────────────────
CHECK_INTERVAL=300          # 常规轮询间隔（秒）
CITIES=Eindhoven,29         # 监控城市，多城市用 | 分隔，建议在 Web 面板勾选
LOG_LEVEL=INFO              # DEBUG / INFO / WARNING / ERROR
TIMEZONE=Europe/Amsterdam     # IANA 时区，用于图表天边界对齐和高峰时段判定

# ── 自适应智能轮询（高峰期）──────────────────────────────────────
PEAK_INTERVAL=60            # 高峰期起始间隔 / 退避目标（秒）
MIN_INTERVAL=15             # 自适应下限，不得低于此值（秒）
PEAK_START=08:30            # 高峰窗口① 开始（荷兰本地时间）
PEAK_END=10:00              # 高峰窗口① 结束（荷兰本地时间）
PEAK_START_2=13:30          # 高峰窗口② 开始（荷兰本地时间）
PEAK_END_2=15:00            # 高峰窗口② 结束（荷兰本地时间）
PEAK_WEEKDAYS_ONLY=true     # 仅工作日启用
JITTER_RATIO=0.20           # 每次等待叠加的随机抖动比例

# ── 监控心跳 ────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_MINUTES=60   # 心跳间隔（分钟），设为 0 禁用心跳

# ── 代理（可选）──────────────────────────────────────────────────
HTTPS_PROXY=                # 例：http://user:pass@host:port
HTTP_PROXY=

# ── 数据库 ──────────────────────────────────────────────────────
DB_PATH=data/listings.db
```

### Telegram Bot 配置（一次性步骤）

1. 向 `@BotFather` 发送 `/newbot`，记下 Token
2. 向你的机器人发任意一条消息
3. 访问 `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. 找到 `"chat": {"id": 123456789}`，填入用户配置的 Chat ID

---

## 通知示例

**新房源上架**
```
✅ 新房源上架

🏠 Kastanjelaan 1-529
📌 状态：Available to book
💰 租金：€1,680/月
📅 可入住：2026-04-01

🛏 类型：2
📐 面积：149 m²
👤 入住：Two (only couples)
🏢 楼层：5
⚡ 能耗：A

🔗 https://www.holland2stay.com/residences/kastanjelaan-1-529.html
```

**状态变更（lottery → 可直接预订）**
```
🚀 状态变更

🏠 Beukenlaan 89-11
📌 Available in lottery → Available to book
💰 租金：€707/月
📅 可入住：2026-04-08

🔗 https://www.holland2stay.com/residences/beukenlaan-89-11.html
```

**自动预订成功**
```
🛒 自动预订成功！

🏠 Kastanjelaan 1-529
💰 租金：€1,680/月
📅 入住：2026-04-01

⚡ 点击链接立即付款（有时限，请尽快）：

https://account.holland2stay.com/idealcheckout/setup.php?order_id=...

⚠️ 链接直达支付页面，无需登录。
```

---


## 文件结构

```
monitor.py          主调度循环，自适应智能轮询（双窗口），热重载，prewarm 缓存（Phase B），按时心跳，双日志
scraper.py          GraphQL 抓取，curl_cffi，自动翻页，429 退避含累计等待，代理支持
storage.py          SQLite：listings / status_changes / web_notifications / meta / geocode_cache，chart 聚合，get_distinct_cities()
models.py           Listing dataclass，price_display，feature_map
notifier.py         BaseNotifier → iMessage（AppleScript 转义加固）/ Telegram / Email / WhatsApp / WebNotifier
booker.py           登录 → createEmptyCart → addNewBooking → placeOrder (store_id=54) → idealCheckOut (plateform "h")；增强错误上下文
config.py           全局配置加载，KNOWN_CITIES（26 城市），ListingFilter，AutoBookConfig
users.py            UserConfig，SQLite user_configs 读写 + 旧 users.json 迁移
translations.py     中/英翻译字典，120+ 键覆盖全部页面
tools/
  geocode_all.py      一次性脚本：通过 Nominatim 预加载所有房源坐标
  reset_db.py         一次性脚本：清空数据库用于测试
web.py              Flask app 引导层 — 安全头、CSRF、i18n、路由注册
app/
  __init__.py       包初始化
  auth.py           Session 鉴权，RBAC 装饰器，访客模式，登录限流
  csrf.py           CSRF token 生成与校验
  db.py             数据库连接工厂
  env_writer.py     .env 文件原地写键
  i18n.py           语言检测与 cookie 持久化
  jinja_filters.py  Jinja2 自定义过滤器
  process_ctrl.py   监控进程生命周期（启动/停止/重载）
  safety.py         安全响应辅助
  forms/
    user_form.py    用户表单数据提取
  routes/
    __init__.py     路由注册协调器
    dashboard.py    仪表盘、图表 API、房源搜索
    calendar_routes.py  日历视图与数据
    map_routes.py   地图、geocode 缓存、片区
    notifications.py    通知列表、已读、SSE 流
    control.py      监控启动/停止/关闭/重载
    sessions.py     登录/登出/访客入口
    settings.py     全局设置查看/保存、过滤选项
    stats.py        图表数据 API
    system.py       系统信息、日志查看器（Tab、行号、级别着色、搜索）、健康检查
    users.py        用户 CRUD、启停、通知测试
static/
  design.css        极简设计系统（去边框，阴影层级，暗/亮双主题，Inter 字体）
  app.js            前端交互：主题切换，移动端导航，SSE 通知，国际化
templates/
  base.html         侧边栏布局，铃铛通知（SSE + Toast），语言切换，日夜主题
  login.html        登录页（独立布局）
  index.html        仪表盘（KPI 卡片 + 最新房源 + 变更记录）
  listings.html     房源列表（状态筛选 + 关键词搜索）
  map.html          地图视图（Leaflet.js + OpenStreetMap，颜色标记）
  calendar.html     入住日历（月视图，城市筛选，详情面板）
  stats.html        数据统计（Chart.js：趋势 / 分布 / 价格区间）
  users.html        用户管理列表（卡片式，渠道 / 过滤 / 操作）
  user_form.html    用户新增 / 编辑表单（4 步：基本信息 / 渠道 / 过滤 / 预订）
  settings.html     全局设置（抓取配置 / 智能轮询 / 城市 / 危险操作区）
pytest.ini          Pytest 配置（strict markers、deprecation 过滤）
requirements-dev.txt Pytest 开发依赖（Docker 镜像不需要）
tests/
  conftest.py       共享 fixture：temp_db、app_ctx、fresh_crypto、test_app、client、admin_client、guest_client
  test_applescript_escape.py   AppleScript 转义加固
  test_auth_routes.py          认证路由（登录/登出/访客/session）
  test_crypto.py               加密/解密往返
  test_log_routes.py           日志 API（文件白名单、清空、路径穿越防护）
  test_models_filter.py        ListingFilter 过滤逻辑（pass/reject 边界）
  test_prewarm_cache.py        Prewarm session 缓存生命周期
  test_safety.py               safe_next_url / 安全跳转辅助
  test_storage_diff.py         SQLite diff 检测（新增/变更/过期）
  test_user_form.py            用户表单数据提取
  test_user_routes.py          用户 CRUD 路由（RBAC 鉴权）
Dockerfile          单容器镜像（python:3.11-slim + supervisord）
docker-compose.yml  卷挂载（data/、logs/、.env），端口映射，健康检查
.dockerignore       排除 .env、data/、logs/、__pycache__ 等不进镜像
docker/
  supervisord.conf    同时管理 monitor.py + web.py，含日志轮转和自动重启
  entrypoint.sh       Docker 入口脚本（首次运行自动创建 .env 和目录）
requirements.txt    curl_cffi, python-dotenv, flask, tzdata
.env.example        配置模板
packaging/
  asset/              应用图标源文件（1024x1024 PNG）
  build_dmg.sh        macOS .dmg 构建脚本（PyInstaller + .app 打包 + 图标生成）
  build.bat           Windows 构建脚本（PyInstaller + ZIP）
  flatradar.spec    PyInstaller 打包配置
launcher.py         macOS .app 入口（导入 web.app，处理 --run-monitor）
.github/workflows/  GitHub Actions CI/CD（推送 tag 或手动触发构建 .dmg + .exe）
data/               运行时自动生成
  listings.db       SQLite 数据库
  users.json*.bak   旧 users.json 迁移后的永久备份
  monitor.pid       监控进程 PID，供热重载使用
logs/               日志文件（supervisord 写入 monitor.log + web.log）
```

---

## 许可证

FlatRadar 基于 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) 许可。

**允许：**
- 个人使用
- 教育用途
- 研究用途
- 非商业修改和再分发

**未经事先书面许可禁止：**
- 商业用途
- 公司或营利性组织使用
- 销售、再许可、作为付费服务托管，或将本项目集成到商业产品或工作流中

完整条款见 [LICENSE](../LICENSE) 文件。
