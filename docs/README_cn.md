# Holland2Stay 房源监控

> 自动监控荷兰租房平台 [Holland2Stay](https://www.holland2stay.com)，第一时间向多个用户推送新房源和状态变更，并支持对符合条件的房源自动完成预订流程。

> **免责声明：** 本项目仅供个人非商业使用。与 Holland2Stay 无任何关联、背书或合作关系。使用者需自行遵守 Holland2Stay 的服务条款及相关法律法规。作者对任何误用或因此产生的后果不承担任何责任。

**在线演示：** [flatradar.app](https://flatradar.app) — 登录页点击「访客模式」即可只读浏览。

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
从 [Releases](../../releases) 下载最新 `.dmg`，拖入 Applications，双击启动即可自动打开浏览器。持久化数据存储在 `~/.h2s-monitor/`。

**Windows：**
从 [Releases](../../releases) 下载最新 `.zip`，解压后双击 `h2s-monitor.exe`。CMD 窗口会自动打开浏览器。持久化数据存储在 `%USERPROFILE%\.h2s-monitor\`。


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

---

## 项目状态

| 模块 | 状态 | 说明 |
|------|------|------|
| 数据抓取 | ✅ 已完成 | GraphQL API + curl_cffi 绕过 Cloudflare WAF |
| 多城市监控 | ✅ 已完成 | 26 个荷兰城市，Web 面板复选框选择 |
| 多通知渠道 | ✅ 已完成 | iMessage / Telegram / Email / WhatsApp，可同时启用 |
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
| 数据可视化 | ✅ 已完成 | 10 个图表：趋势、城市/状态/价格/面积/楼层/户型/能耗/租客/合同分布、24h 上线时间 |
| 入住日历 | ✅ 已完成 | 月视图，按城市筛选 |
| 地图视图 | ✅ 已完成 | Leaflet.js + OpenStreetMap，自动地理编码，颜色标记 |
| i18n 中英切换 | ✅ 已完成 | 一键切换语言，cookie 持久化 |
| 通知测试 | ✅ 已完成 | 一键逐渠道测试，返回成功 / 失败详情 |
| 访客模式（RBAC）| ✅ 已完成 | 无需密码只读访问；用户/设置/日志等管理功能仅 admin 可见 |
| 面板鉴权 | ✅ 已完成 | Session 登录，opt-in（设置密码后启用）；`WEB_GUEST_MODE` 控制访客入口 |
| 登录爆破防护 | ✅ 已完成 | IP 级失败计数 + 指数退避延迟 |
| HTTPS / Caddy | ✅ 已完成 | 内置 Caddyfile + docker-compose Caddy 服务，自动签发 Let's Encrypt 证书 |
| 安全加固 | ✅ 已完成 | RBAC 装饰器、通知/SSE/geocode 访客屏蔽、CSRF 防护、DOM XSS 防护（地图 geocode 错误详情、设置数值校验） |
| 启动预检 | ✅ 已完成 | `WEB_PASSWORD` 未设置或 Caddyfile 仍为占位域名时阻止容器启动 |
| 生产 WSGI | ✅ 已完成 | Docker 中 Gunicorn 替代 Flask 内置服务器（1 worker × 8 线程，timeout=0） |
| 依赖版本锁定 | ✅ 已完成 | `requirements.lock` 精确版本，Dockerfile 从 lock 文件安装，构建可重复 |
| 代码模块化 | ✅ 已完成 | web.py 拆分为 `app/` 子包（10 个路由模块 + 8 个共享模块），1,200 行精简至 154 行引导层 |
| Prewarm Session 缓存 | ✅ 已完成 | 进程级缓存跨轮复用；Token TTL 后台刷新；用户/配置变更时自动失效 |
| 错误日志（errors.log）| ✅ 已完成 | 独立 WARNING+ 日志，含 `funcName:lineno` 格式；新增 web.log 记录 Flask 应用日志；日志查看器支持文件 Tab 切换、行号、级别着色、关键词搜索、自动滚动 |
| Pytest 测试套件 | ✅ 已完成 | 25 个测试模块（486 个测试），覆盖全栈：模型、存储、抓取、预订、通知、认证、CSRF、路由、i18n |
| 代码质量 | ✅ 已完成 | Literal 类型、共享常量、Storage 抽象统一、解析逻辑去重 |

---

## 核心功能

### 数据抓取

- 每隔 N 秒（默认 5 分钟）轮询 Holland2Stay GraphQL API
- 支持 26 个荷兰城市同时监控，城市列表在 Web 面板勾选
- 检测**新上架房源**与**状态变更**（如 lottery → 可直接预订）
- 全量房源写入本地 SQLite，历史可查，同一房源不重复通知

### 智能自适应轮询

非高峰期使用常规间隔。荷兰工作日两个高峰时段（默认早 8:30–10:00，下午 13:30–15:00）启用自适应轮询：

- 每次高峰期从 `PEAK_INTERVAL`（默认 60 s）出发
- 每轮抓取成功后将间隔乘以 0.95（缩短 5%），自动逼近速率上限
- 最低不低于 `MIN_INTERVAL`（默认 15 s，可配置）
- 遭遇 429 限流时：间隔立即翻倍，并强制冷却 5 分钟后再恢复
- 高峰期结束后重置为 `PEAK_INTERVAL`，下次高峰期重新探测
- 每次等待都叠加 ±`JITTER_RATIO`% 随机抖动，破坏机械规律性
- 所有参数（PEAK_INTERVAL、MIN_INTERVAL、PEAK_START、PEAK_END、PEAK_START_2、PEAK_END_2、JITTER_RATIO、PEAK_WEEKDAYS_ONLY）均可在 Web 面板配置

### 限流与屏蔽防护

**429（限流）— 临时，可自动恢复：**

1. **scraper 层**：收到 429 后等 30 s 重试，再等 60 s 重试，仍失败则抛出 `RateLimitError`
2. **monitor 层**：捕获 `RateLimitError`，通知所有用户，强制冷却 5 分钟后继续
3. **自适应层**：自动将高峰间隔翻倍退避，从根源减少触发 429 的频率

**403（Cloudflare WAF 屏蔽）— 持续，需人工介入：**

- **scraper 层**：检测 Cloudflare 挑战页（HTML 特征如 `no-js ie6 oldie`），立即抛出 `BlockedError`，不重试（与 429 不同）
- **monitor 层**：捕获 `BlockedError`，通知用户（节流至 30 分钟 1 次），冷却 15 分钟
- **可操作建议**：错误消息含三条恢复路径 — 换代理 IP / 重启 monitor 重建 TLS 指纹 / 暂停几小时冷却

**代理支持**：在 `.env` 中设置 `HTTPS_PROXY` / `HTTP_PROXY`，抓取和预订流量均通过代理路由；支持热重载，无需重启即可生效。

### 快速预订通道

无论是**新上线**的 Available to book 房源，还是**状态切换**到 Available to book 的房源，抢占窗口往往只有几秒钟。监控程序的处理逻辑：

1. 对 diff 结果做纯内存预扫描（无网络请求）
2. **立即**将 `try_book()` 提交到线程池（**所有**自动预订候选均走快速通道），早于任何通知发送
3. 预订 HTTP 请求与通知发送**并发执行**
4. 通知发完后再 await 预订结果——此时预订往往已经完成

与原来"先发完通知再预订"的顺序执行相比，这套方案将预订请求到达服务器的延迟从 2–5 秒缩短到约 0–1 秒。

### 多用户支持

- 每个用户独立拥有：通知渠道 + 凭证、房源过滤条件、自动预订账号
- 抓取一次共享，通知和预订按各用户条件分发，N 用户 ≠ N 倍 API 请求
- 用户数据存储于 `data/users.json`，Web 面板增删改、一键启停
- **零配置升级**：首次启动自动从旧 `.env` 通知配置迁移为默认用户

### 通知推送

**用户独立的推送渠道**（iMessage、Telegram、Email、WhatsApp）：

- 每用户可独立选择一个或多个渠道同时启用
- 通知内容：房源名称、状态、租金、面积、楼层、能耗、入住日期、直链
- 每用户可独立设置过滤条件，只接收符合自己需求的房源
- 配置页一键发送测试消息，逐渠道返回成功 / 失败原因

**iMessage 平台检测**：iMessage 依赖 macOS 和 Messages.app。在 Linux / Windows / Docker 上，该渠道自动跳过并打印警告；用户配置页面若检测到服务器非 macOS，会显示提示横幅，建议改用其他渠道。

**Web 面板通知（与平台无关）**：

- 每个事件（新房源、状态变更、预订结果、错误、心跳）都会写入 `web_notifications` SQLite 表
- 导航栏铃铛图标显示未读数角标，点击展开最近通知下拉面板
- 新事件自动弹出滑入式 Toast 通知
- 基于 Server-Sent Events（SSE）实时推送，断连后浏览器自动重连
- 在所有平台（VPS、Docker、本地 Mac/Linux/Windows）均可使用，无额外依赖

### 自动预订

- 检测到符合条件的 "Available to book" 房源时，自动完成完整预订流程：
  1. 登录账号
  2. `createEmptyCart` 创建全新购物车
  3. `addNewBooking` 将房源加入购物车并创建预订
  4. `placeOrder` 下单（含 `store_id=54`，与官方前端一致）
  5. `idealCheckOut` 生成直链付款 URL（含 `plateform="h"`）
  6. 推送通知，含直链，用户点击即进入付款页，**无需登录**
- 流程通过浏览器 DevTools 抓包验证，与 H2S 官方前端预订流程一致
- 若 `placeOrder` 返回 "another unit reserved" 且用户开启了 `cancel_enabled`，通过 `cancelOrder` mutation 自动取消旧订单后重试整个流程
- `cancel_enabled` 默认关闭：H2S 平台默认未启用 `cancelOrder`，开启前需确认平台支持
- 多套候选时按面积从大到小选择
- 每用户可设置独立的预订过滤条件（可比通知条件更严格）
- 支持 Dry Run 模式，走完登录/购物车验证但不实际提交
- 预订与通知并发执行（见「快速预订通道」）

### Web 管理面板

- **仪表盘**：总房源 / 今日新增 / 今日变更 / 最近抓取时间，加最新房源列表与近 48h 变更记录
- **房源列表**：全量数据，支持按状态筛选 + 关键词搜索
- **地图视图**：Leaflet.js 交互地图，Nominatim 自动地理编码 + 坐标缓存，颜色标记（绿=直订/橙=摇号/灰=其他），点击弹窗详情，亮色/暗色底图滤镜
- **入住日历**：所有有入住日期的房源按月历展示，按城市筛选
- **统计图表**：Chart.js 折线图 / 环形图 / 柱状图，7/30/90 天可切换；租金分布 9 区间（最高 >€1600）；24h 上线时间分布
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

```
Holland2Stay 网站（Next.js + Magento）
        │
        │  页面数据由 Apollo Client 发起 GraphQL 请求
        ▼
api.holland2stay.com/graphql/   ← Magento GraphQL 后端
        │
        │  curl_cffi impersonate="chrome110"  绕过 Cloudflare WAF
        ▼
   scraper.py  ──→  models.py（Listing dataclass）
        │
        ▼
   storage.py（SQLite diff：比对新旧快照）
        │
        ├── 新房源 / 状态变更
        │        │
        │        ├── WebNotifier → web_notifications 表
        │        │     └── /api/events SSE → 浏览器铃铛 + Toast
        │        │
        │        └── 遍历 users.json 中每个启用的用户
        │                 │
        │                 ├── ListingFilter.passes() → notifier.py
        │                 │     └── iMessage（macOS）/ Telegram / Email / WhatsApp
        │                 │
        │                 └── AutoBookConfig.passes() → booker.py  [并发执行]
        │                       └── 预登录 session（与通知并行完成）
        │                              → createEmptyCart → addNewBooking
        │                              → placeOrder (store_id=54) → idealCheckOut → 付款直链
        │
        └── Web 面板只读查询 → web.py（Flask + Bootstrap 5）
                 ├── /api/charts
                 ├── /api/events  （SSE 流）
                 └── /api/notifications
```

### 模块职责

| 文件 | 职责 |
|------|------|
| `monitor.py` | 主调度循环，自适应智能轮询（双高峰窗口），热重载，prewarm 缓存（Phase B 跨轮复用），并发预订，按时心跳，双日志（monitor.log + errors.log） |
| `scraper.py` | GraphQL 抓取，curl_cffi，自动翻页，多城市，429 退避含累计等待，代理支持，增强错误上下文日志 |
| `storage.py` | SQLite 持久化，diff 检测，chart 聚合，meta 键值，web_notifications 表，`get_distinct_cities()` |
| `models.py` | Listing dataclass，price_display，feature_map |
| `notifier.py` | BaseNotifier ABC，iMessage（macOS 检测 + AppleScript 转义加固），Telegram，Email，WhatsApp，WebNotifier，MultiNotifier |
| `booker.py` | PrewarmedSession；createEmptyCart → addNewBooking → placeOrder (store_id) → idealCheckOut (plateform "h")；增强错误上下文（sku/contract_id/start_date）；cancel_enabled 代理支持 |
| `config.py` | 全局配置加载，KNOWN_CITIES（26 城市），ListingFilter，AutoBookConfig |
| `users.py` | UserConfig dataclass，users.json 读写，.env 配置迁移 |
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
| Cloudflare 403 | `curl_cffi` + `impersonate="chrome110"` | TLS 层模拟 Chrome 指纹，无需启动浏览器 |
| 页面无房源数据 | 直接请求 GraphQL API | Next.js + Apollo CSR，HTML 中无房源 DOM |
| 预订竞争条件 | 通知前先提交 `try_book()` 到线程池 | 预订与通知并发执行，早到服务器 2–4 秒 |
| 重复登录开销 | `PrewarmedSession`：每轮只登录一次，多套候选复用 | 预登录与通知并发进行；每次预订节省约 0.7 秒（建连 + 登录往返） |
| API 限流 | 429 退避重试（30s / 60s）+ 5 分钟冷却 + 自适应降速 | 三层防御：scraper 重试、monitor 冷却、自适应间隔守住安全阈值 |
| Cloudflare 403 WAF 屏蔽 | 立即抛 `BlockedError`（不重试）+ Cloudflare 挑战页识别 + 15 分钟冷却 + 节流告警（最多 1 次/30 分钟） | 403 是持续性封禁，等待无效；错误消息含可操作的恢复步骤 |
| 高峰期频率探测 | 自适应间隔：×0.95 成功，×2.0 限流，下限 MIN_INTERVAL | 自动发现最大安全频率，无需手动调参 |
| 异步通知 + 同步抓取 | `run_in_executor` 桥接 | scraper 用 sync curl_cffi，notifier 用 async subprocess |
| 多渠道通知 | `BaseNotifier` ABC + `MultiNotifier` 聚合 | 统一格式化逻辑，子类只实现 `_send()` |
| 与平台无关的通知 | `WebNotifier` 写 SQLite，SSE 推送浏览器 | 在 VPS / Docker 上无需任何 OS 依赖 |
| iMessage 非 macOS | `is_macos()` 检测，`create_user_notifier()` 跳过 | 清晰警告，优雅降级，Web 面板接管 |
| SQLite 并发访问 | WAL journal mode | monitor 写 web_notifications，web.py 独立连接只读，互不阻塞 |
| 配置热重载 | SIGHUP → asyncio.Event（Unix）/ reload 文件轮询（Windows） | 修改后配置立即生效，监控进程不中断 |
| 多用户存储 | `data/users.json` | 无额外依赖，结构清晰，Web 面板直接 CRUD |
| 主题切换无闪烁 | `<head>` 内联脚本 + CSS custom properties | 在 CSS 渲染前同步设置 `data-bs-theme`，避免 FOUC |
| 面板鉴权 opt-in | `WEB_PASSWORD` 为空则跳过鉴权 | 本地运行无需配置，对外暴露时一行配置即可加锁 |
| web.py 单体膨胀（1,200+ 行） | 拆分为 `app/routes/`（10 个路由模块）+ `app/`（auth、csrf、db、i18n 等） | 每个模块 15–240 行，职责单一；`web.py` 精简为 154 行引导层；路由用 `add_url_rule` 保留扁平 endpoint 名，模板零改动 |
| Prewarm Session 每轮浪费 | 进程级缓存 + 智能 TTL 刷新；跨轮复用 | 命中：零网络 IO；TTL < 300 s：后台刷新（与抓取并行）；仅 email 变更 / unknown_error 失效 |
| INFO 噪音淹没告警 | 独立 `errors.log`（WARNING+），含 `funcName:lineno` 格式，backupCount=5 | `monitor.log` 保留 INFO+ 运维视图；`errors.log` 归档稀疏但可操作的异常，精确定位源 |
| 无自动化测试 | 10 个 pytest 模块，共享 fixture（`temp_db`、`client`、`admin_client` 等） | 纯函数测试覆盖 models/crypto/safety/storage；HTTP 集成测试覆盖 auth/user/log 路由；零外部网络依赖 |

### GraphQL API 参数

| 参数 | 值 |
|------|-----|
| 端点 | `POST https://api.holland2stay.com/graphql/` |
| 分类 ID | `category_uid: "Nw=="` (Residences) |
| 可直接预订 | `available_to_book: { in: ["179"] }` |
| 摇号中 | `available_to_book: { in: ["336"] }` |
| 自定义属性 | `custom_attributesV2` → `price`（总租金含服务费）/ `living_area` / `floor` / `available_startdate` 等 |

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

> **提示**：首次启动时，若 `data/users.json` 不存在且 `.env` 中配置了旧版通知变量，会自动迁移为默认用户，无需手动配置。

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
docker compose up -d --build
```

### 配置说明

**用户级别的配置**（通知渠道、过滤条件、自动预订）在 Web 面板 → 用户管理 中设置，存储在 `data/users.json`。

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
users.py            UserConfig，users.json 读写，.env 配置迁移
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
  h2s_monitor.spec    PyInstaller 打包配置
launcher.py         macOS .app 入口（导入 web.app，处理 --run-monitor）
.github/workflows/  GitHub Actions CI/CD（推送 tag 或手动触发构建 .dmg + .exe）
data/               运行时自动生成
  listings.db       SQLite 数据库
  users.json        用户配置（通知渠道 / 过滤 / 预订账号）
  monitor.pid       监控进程 PID，供热重载使用
logs/               日志文件（supervisord 写入 monitor.log + web.log）
```

---

## 许可证

Holland2Stay Monitor 基于 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) 许可。

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
