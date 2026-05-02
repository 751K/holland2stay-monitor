# Holland2Stay 房源监控

> 自动监控荷兰租房平台 [Holland2Stay](https://www.holland2stay.com)，第一时间向多个用户推送新房源和状态变更，并支持对符合条件的房源自动完成预订流程。

**个人开发，严禁商业使用，欢迎 Star 和 Fork！如有任何问题或建议，请随时提交 Issue 或 Pull Request**

---

## 项目状态

| 模块 | 状态 | 说明 |
|------|------|------|
| 数据抓取 | ✅ 已完成 | GraphQL API + curl_cffi 绕过 Cloudflare WAF |
| 多城市监控 | ✅ 已完成 | 26 个荷兰城市，Web 面板复选框选择 |
| 多通知渠道 | ✅ 已完成 | iMessage / Telegram / Email / WhatsApp，可同时启用 |
| 通知过滤 | ✅ 已完成 | 租金、面积、楼层、户型、片区，按用户独立设置 |
| 自动预订 | ✅ 已完成 | 全流程：加入购物车 → 下单 → 生成直链付款 URL，多渠道推送 |
| Web 管理面板 | ✅ 已完成 | 仪表盘、房源列表、用户管理、全局设置 |
| 配置热重载 | ✅ 已完成 | SIGHUP 信号，修改后无需重启监控进程 |
| 智能轮询 | ✅ 已完成 | 荷兰时间 8:30–10:00 自动加速至 60 秒间隔 |
| 多用户支持 | ✅ 已完成 | 每用户独立渠道 / 过滤 / 预订账号，users.json 管理 |
| 日夜主题 | ✅ 已完成 | 浅色 / 深色主题切换，跟随系统偏好，无刷新闪烁 |
| 数据可视化 | ✅ 已完成 | 30天趋势、城市分布、状态分布、价格区间图表 |
| 入住日历 | ✅ 已完成 | 按城市筛选，日历视图查看所有房源入住时间 |
| 通知测试 | ✅ 已完成 | 一键测试各渠道，逐渠道返回成功/失败详情 |
| 面板鉴权 | ✅ 已完成 | Session 登录，opt-in（设置密码后启用） |

---

## 核心功能

### 数据抓取
- 每隔 N 秒（默认 5 分钟）轮询 Holland2Stay GraphQL API
- 支持 26 个荷兰城市同时监控，城市列表在 Web 面板勾选
- 检测**新上架房源**与**状态变更**（如 lottery → 可直接预订）
- 全量房源写入本地 SQLite，历史可查，同一房源不重复通知

### 智能轮询
- 荷兰工作日 8:30–10:00（新房源上架高峰）自动缩短轮询间隔至 60 秒
- 其余时间恢复正常间隔（默认 300 秒），兼顾时效性与系统资源
- 高峰时段、开始/结束时间、是否仅工作日均可在 Web 面板配置

### 多用户支持
- 每个用户独立拥有：通知渠道 + 凭证、房源过滤条件、自动预订账号
- 抓取一次共享，通知和预订按各用户条件分发，N 用户 ≠ N 倍 API 请求
- 用户数据存储于 `data/users.json`，Web 面板增删改、一键启停
- **零配置升级**：首次启动自动从旧 `.env` 通知配置迁移为默认用户

### 通知推送
- 支持四个渠道，每用户可独立选择，可同时启用多个：
  - **iMessage**（macOS，免费，需运行在 Mac 上）
  - **Telegram Bot**（跨平台，免费，配置最简单）
  - **Email（SMTP）**（跨平台，适合作为备用渠道）
  - **WhatsApp via Twilio**（跨平台，Twilio 付费服务）
- 通知内容：房源名称、状态、租金、面积、楼层、能耗、入住日期、直链
- 每用户可独立设置过滤条件，只接收符合自己需求的房源通知
- **通知测试**：用户配置页一键发送测试消息，逐渠道返回成功 / 失败原因

### 自动预订
- 检测到符合条件的 "Available to book" 房源时，自动完成完整流程：
  1. 登录账号，取消遗留的 pending 订单（避免冲突）
  2. `addNewBooking` 将房源加入购物车
  3. `placeOrder` 下单（押金订单）
  4. `idealCheckOut` 生成直链付款 URL
  5. iMessage / Telegram / Email 推送通知，含直链，用户点击即进入付款页，**无需登录**
- 多套候选时按面积从大到小选择
- 每用户可设置独立的预订过滤条件（可比通知条件更严格）
- 支持 Dry Run 模式，走完登录/购物车验证但不实际提交，用于验证配置

### Web 管理面板
- **仪表盘**：总房源 / 今日新增 / 今日变更 / 最近抓取时间，加最新房源列表与近 48h 变更记录
- **房源列表**：全量数据，支持按状态筛选 + 关键词搜索
- **入住日历**：所有有入住日期的房源按日历展示，按城市筛选，点击日期查看房源详情
- **统计图表**：30 天新增趋势、状态变更趋势、城市分布、状态分布、价格区间直方图
- **用户管理**：多用户 CRUD，每用户独立配置通知/过滤/预订，一键启停，一键发送测试通知
- **全局设置**：轮询间隔、智能轮询参数、监控城市，可视化配置无需手动编辑 `.env`
- **立即生效**：保存后点击按钮，通过 SIGHUP 热重载配置，监控进程不中断
- **登录鉴权**：设置 `WEB_PASSWORD` 后启用 Session 登录，未设置则无需登录（本地使用友好）
- **日夜主题**：导航栏一键切换浅色/深色，偏好自动保存，首次访问跟随系统设置

---

## 技术架构

### 数据流

```
Holland2Stay 网站（Next.js + Magento）
        │
        │  抓包发现：页面数据由 Apollo Client 发起 GraphQL 请求
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
        │        └── 遍历 users.json 中每个启用的用户
        │                 │
        │                 ├── ListingFilter.passes() → notifier.py
        │                 │     └── iMessage / Telegram / Email / WhatsApp
        │                 │
        │                 └── AutoBookConfig.passes() → booker.py
        │                       └── 登录 → 取消 pending 订单 → addNewBooking → placeOrder → idealCheckOut → 付款直链
        │
        └── Web 面板只读查询 → web.py（Flask + Bootstrap 5）
                 └── /api/charts → stats.html（Chart.js CDN）
```

### 模块职责

| 文件 | 职责 |
|------|------|
| `monitor.py` | 主调度循环，智能轮询，SIGHUP 热重载，PID 管理 |
| `scraper.py` | GraphQL 抓取，curl_cffi，自动翻页，多城市并发 |
| `storage.py` | SQLite 持久化，diff 检测，chart 聚合查询，meta 键值存储 |
| `models.py` | Listing dataclass，price_display，feature_map |
| `notifier.py` | BaseNotifier ABC，IMessage / Telegram / Email / WhatsApp / MultiNotifier |
| `booker.py` | 登录 → 取消 pending 订单 → addNewBooking → placeOrder → idealCheckOut → 付款直链 |
| `config.py` | 全局配置加载，KNOWN_CITIES（26 城市），ListingFilter，AutoBookConfig |
| `users.py` | UserConfig dataclass，users.json 读写，.env 配置迁移 |
| `web.py` | Flask 面板，用户 CRUD，Session 鉴权，/api/charts，/api/reload |
| `templates/` | Bootstrap 5.3，日夜主题，Chart.js 图表，日历视图 |

### 关键技术决策

| 问题 | 方案 | 原因 |
|------|------|------|
| Cloudflare 403 | `curl_cffi` + `impersonate="chrome110"` | TLS 层模拟 Chrome 指纹，无需启动浏览器，速度极快 |
| 页面无房源数据 | 直接请求 GraphQL API | Next.js + Apollo CSR，HTML 中无房源 DOM |
| 异步通知 + 同步抓取 | `run_in_executor` 桥接 | scraper 用 sync curl_cffi，notifier 用 async subprocess |
| 多渠道通知 | `BaseNotifier` ABC + `MultiNotifier` 聚合 | 统一格式化逻辑，子类只实现 `_send()` |
| 配置热重载 | SIGHUP → `asyncio.Event` | `wait_for(event, timeout=interval)` 提前唤醒，零停机 |
| 多用户存储 | `data/users.json` | 无额外依赖，结构清晰，Web 面板直接 CRUD |
| 主题切换无闪烁 | `<head>` 内联脚本 + CSS custom properties | 在 CSS 渲染前同步设置 `data-bs-theme`，避免 FOUC |
| 面板鉴权 opt-in | `WEB_PASSWORD` 为空则跳过鉴权 | 本地运行无需配置，对外暴露时一行配置即可加锁 |
| 通知测试 | 逐渠道独立实例化、`asyncio.run()` 单次发送 | 获得 per-channel 结果，Flask 同步路由内直接运行异步代码 |

### GraphQL API 参数

| 参数 | 值 |
|------|-----|
| 端点 | `POST https://api.holland2stay.com/graphql/` |
| 分类 ID | `category_uid: "Nw=="` (Residences) |
| 可直接预订 | `available_to_book: { in: ["179"] }` |
| 摇号中 | `available_to_book: { in: ["336"] }` |
| 自定义属性 | `custom_attributesV2` → `basic_rent` / `living_area` / `floor` / `available_startdate` 等 |

---

## 快速开始

### 安装

```bash
# 要求 Python 3.11+（macOS 内置）
pip install -r requirements.txt
cp .env.example .env
```

### 启动

```bash
# 1. 验证抓取是否正常（不写库、不发通知）
python monitor.py --test

# 2. 启动 Web 面板，在「用户管理」页面添加第一个用户
python web.py              # http://127.0.0.1:5000

# 3. 单次运行，验证完整通知流程
python monitor.py --once

# 4. 持续监控（另开终端或后台运行）
python monitor.py
nohup python monitor.py > logs/monitor.log 2>&1 &
```

> **提示**：首次启动 `monitor.py` 时，若 `data/users.json` 不存在且 `.env` 中配置了 `IMESSAGE_RECIPIENT` 或 `NOTIFICATION_CHANNELS`，会自动迁移为默认用户，无需手动配置。

### 配置说明

**用户级别的配置**（通知渠道、过滤条件、自动预订）在 Web 面板 → 用户管理 中设置，存储在 `data/users.json`。

**全局配置**通过 Web 面板 → 全局设置，或直接编辑 `.env`：

```env
# ── Web 面板鉴权（可选）──────────────────────────────────────────
WEB_USERNAME=admin          # 默认 admin，可修改
WEB_PASSWORD=               # 留空则无需登录；填写后访问面板须先登录
FLASK_SECRET=               # 留空则自动生成并写入 .env（重启不失效）

# ── 抓取 ────────────────────────────────────────────────────────
CHECK_INTERVAL=300          # 常规轮询间隔（秒）
CITIES=Eindhoven,29         # 监控城市，多城市用 | 分隔，建议在 Web 面板勾选
LOG_LEVEL=INFO              # DEBUG / INFO / WARNING / ERROR

# ── 智能轮询（高峰期自动加速）──────────────────────────────────
PEAK_INTERVAL=60            # 高峰期轮询间隔（秒）
PEAK_START=08:30            # 高峰开始（荷兰本地时间）
PEAK_END=10:00              # 高峰结束（荷兰本地时间）
PEAK_WEEKDAYS_ONLY=true     # 仅工作日启用

# ── 数据库 ──────────────────────────────────────────────────────
DB_PATH=data/listings.db    # SQLite 路径
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

## 路线图

### 🔴 高优先级

#### Docker 化部署
- **当前痛点**：监控程序必须跑在本地 Mac 上，Mac 休眠或关机即中断；iMessage 依赖 macOS 系统，无法迁移到 Linux VPS
- **方案**：`Dockerfile` + `docker-compose.yml`，剥离 iMessage 依赖（VPS 上使用 Telegram/Email/WhatsApp），部署到 €5/月的 VPS 实现 24/7 不间断
- **价值**：彻底解除对本地设备的依赖，是稳定运行的基础

---

### 🟡 中优先级

#### Lottery 摇号自动报名
- 对 "Available in lottery" 房源自动提交申请，无需手动操作
- 探索 `registerInterest` 等 GraphQL mutation
- 风险：需账号鉴权，且平台规则可能限制频繁报名

#### 每日摘要推送
- 每天定时（如早 8 点）推送汇总消息：当日新增 N 套、状态变更 M 套、数据库共 X 套
- 替代心跳消息，信息密度更高，减少打扰

#### Discord 通知渠道
- Webhook 实现，零额外费用
- 适合多人共享房源信息（朋友 / 家人群组）

#### 价格历史追踪
- 记录同一房源的租金变化（`price_history` 表）
- 检测降价并推送通知，辅助比较决策

---

## 文件结构

```
monitor.py          主调度循环，智能轮询，SIGHUP 热重载，PID 管理
scraper.py          GraphQL 抓取，curl_cffi，自动翻页，多城市
storage.py          SQLite：listings / status_changes / meta，chart 聚合查询
models.py           Listing dataclass，price_display，feature_map
notifier.py         BaseNotifier → IMessage / Telegram / Email / WhatsApp / Multi
booker.py           登录 → 取消 pending 订单 → addNewBooking → placeOrder → idealCheckOut → 付款直链
config.py           全局配置加载，KNOWN_CITIES（26 城市），ListingFilter
users.py            UserConfig，users.json 读写，.env 配置迁移
web.py              Flask 面板，Session 鉴权，用户 CRUD，/api/charts，/api/reload
templates/
  base.html         布局，导航栏，日夜主题（CSS variables + Anti-FOUC）
  login.html        登录页（独立，支持日夜主题，密码显示切换）
  index.html        仪表盘
  listings.html     房源列表（状态筛选 + 关键词搜索）
  calendar.html     入住日历（月视图，城市筛选，点击查看详情）
  stats.html        数据统计（Chart.js：趋势 / 分布 / 价格区间）
  users.html        用户管理列表（含通知测试按钮）
  user_form.html    用户新增 / 编辑表单（含内联测试结果）
  settings.html     全局设置（轮询 / 城市 / 智能轮询）
requirements.txt    curl_cffi, python-dotenv, flask, tzdata
.env.example        配置模板
data/               运行时自动生成
  listings.db       SQLite 数据库
  users.json        用户配置（通知渠道 / 过滤 / 预订账号）
  monitor.pid       监控进程 PID，供热重载使用
```
