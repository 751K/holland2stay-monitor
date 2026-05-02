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
| Web 面板通知 | ✅ 已完成 | SSE 实时铃铛 + Toast 弹窗，与平台无关 |
| 通知过滤 | ✅ 已完成 | 租金、面积、楼层、户型、片区，按用户独立设置 |
| 自动预订 | ✅ 已完成 | 全流程：加入购物车 → 下单 → 生成直链付款 URL |
| 快速预订通道 | ✅ 已完成 | Reserved → Available 状态变更在发通知前即刻提交预订 |
| Web 管理面板 | ✅ 已完成 | 仪表盘、房源列表、用户管理、全局设置 |
| 配置热重载 | ✅ 已完成 | 跨平台，修改后无需重启监控进程 |
| 智能轮询 | ✅ 已完成 | 高峰期自适应加速，自动逼近速率上限 |
| 限流防护 | ✅ 已完成 | 429 指数退避重试 + 5 分钟冷却 + 代理支持 |
| 多用户支持 | ✅ 已完成 | 每用户独立渠道 / 过滤 / 预订账号 |
| VPS / Docker 兼容 | ✅ 已完成 | 非 macOS 自动跳过 iMessage，Web 面板接管通知 |
| 日夜主题 | ✅ 已完成 | 浅色 / 深色，跟随系统偏好，无刷新闪烁 |
| 数据可视化 | ✅ 已完成 | 30 天趋势、城市 / 状态分布、价格区间图表 |
| 入住日历 | ✅ 已完成 | 月视图，按城市筛选 |
| 通知测试 | ✅ 已完成 | 一键逐渠道测试，返回成功 / 失败详情 |
| 面板鉴权 | ✅ 已完成 | Session 登录，opt-in（设置密码后启用） |

---

## 核心功能

### 数据抓取

- 每隔 N 秒（默认 5 分钟）轮询 Holland2Stay GraphQL API
- 支持 26 个荷兰城市同时监控，城市列表在 Web 面板勾选
- 检测**新上架房源**与**状态变更**（如 lottery → 可直接预订）
- 全量房源写入本地 SQLite，历史可查，同一房源不重复通知

### 智能自适应轮询

非高峰期使用常规间隔。荷兰工作日 8:30–10:00（默认，新房源上架高峰）启用自适应轮询：

- 每次高峰期从 `PEAK_INTERVAL`（默认 60 s）出发
- 每轮抓取成功后将间隔乘以 0.95（缩短 5%），自动逼近速率上限
- 最低不低于 `MIN_INTERVAL`（默认 15 s，可配置）
- 遭遇 429 限流时：间隔立即翻倍，并强制冷却 5 分钟后再恢复
- 高峰期结束后重置为 `PEAK_INTERVAL`，下次高峰期重新探测
- 每次等待都叠加 ±`JITTER_RATIO`% 随机抖动，破坏机械规律性
- 所有参数（PEAK_INTERVAL、MIN_INTERVAL、PEAK_START、PEAK_END、JITTER_RATIO、PEAK_WEEKDAYS_ONLY）均可在 Web 面板配置

### 限流防护（三层防御）

1. **scraper 层**：收到 429 后等 30 s 重试，再等 60 s 重试，仍失败则抛出 `RateLimitError`
2. **monitor 层**：捕获 `RateLimitError`，通知所有用户，强制冷却 5 分钟后继续
3. **自适应层**：自动将高峰间隔翻倍退避，从根源减少触发 429 的频率

**代理支持**：在 `.env` 中设置 `HTTPS_PROXY` / `HTTP_PROXY`，抓取和预订流量均通过代理路由；支持热重载，无需重启即可生效。

### 快速预订通道

当房源从"Reserved"等状态**直接切换为"Available to book"**时，抢占窗口往往只有几秒钟。监控程序的处理逻辑：

1. 对 diff 结果做纯内存预扫描（无网络请求）
2. **立即**将 `try_book()` 提交到线程池，早于任何通知发送
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
  1. 登录账号，取消遗留的 pending 订单（避免冲突）
  2. `addNewBooking` 将房源加入购物车
  3. `placeOrder` 下单（押金订单）
  4. `idealCheckOut` 生成直链付款 URL
  5. 推送通知，含直链，用户点击即进入付款页，**无需登录**
- 多套候选时按面积从大到小选择
- 每用户可设置独立的预订过滤条件（可比通知条件更严格）
- 支持 Dry Run 模式，走完登录/购物车验证但不实际提交
- 预订与通知并发执行（见「快速预订通道」）

### Web 管理面板

- **仪表盘**：总房源 / 今日新增 / 今日变更 / 最近抓取时间，加最新房源列表与近 48h 变更记录
- **房源列表**：全量数据，支持按状态筛选 + 关键词搜索
- **入住日历**：所有有入住日期的房源按月历展示，按城市筛选
- **统计图表**：30 天新增趋势、状态变更趋势、城市分布、状态分布、价格区间直方图
- **用户管理**：多用户 CRUD，每用户独立配置通知 / 过滤 / 预订，一键启停，一键发送测试通知
- **全局设置**：轮询间隔、自适应轮询参数、监控城市，可视化配置无需手动编辑 `.env`
- **立即生效**：保存后点击按钮，通过 SIGHUP 热重载配置，监控进程不中断
- **铃铛通知**：导航栏实时通知，Bell + Toast 弹窗，SSE 推送，点击标记已读

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
        │                       └── 登录 → 取消 pending 订单 → addNewBooking
        │                              → placeOrder → idealCheckOut → 付款直链
        │
        └── Web 面板只读查询 → web.py（Flask + Bootstrap 5）
                 ├── /api/charts
                 ├── /api/events  （SSE 流）
                 └── /api/notifications
```

### 模块职责

| 文件 | 职责 |
|------|------|
| `monitor.py` | 主调度循环，自适应智能轮询，SIGHUP 热重载，PID 管理，并发预订 |
| `scraper.py` | GraphQL 抓取，curl_cffi，自动翻页，多城市，429 重试，代理支持 |
| `storage.py` | SQLite 持久化，diff 检测，chart 聚合，meta 键值，web_notifications 表 |
| `models.py` | Listing dataclass，price_display，feature_map |
| `notifier.py` | BaseNotifier ABC，iMessage（macOS 检测），Telegram，Email，WhatsApp，WebNotifier，MultiNotifier |
| `booker.py` | 登录 → 取消 pending 订单 → addNewBooking → placeOrder → idealCheckOut → 付款直链，代理支持 |
| `config.py` | 全局配置加载，KNOWN_CITIES（26 城市），ListingFilter，AutoBookConfig |
| `users.py` | UserConfig dataclass，users.json 读写，.env 配置迁移 |
| `web.py` | Flask 面板，Session 鉴权，用户 CRUD，SSE 流，通知 API，/api/reload |
| `templates/` | Bootstrap 5.3，铃铛通知（SSE + Toast），日夜主题，Chart.js，日历视图 |

### 关键技术决策

| 问题 | 方案 | 原因 |
|------|------|------|
| Cloudflare 403 | `curl_cffi` + `impersonate="chrome110"` | TLS 层模拟 Chrome 指纹，无需启动浏览器 |
| 页面无房源数据 | 直接请求 GraphQL API | Next.js + Apollo CSR，HTML 中无房源 DOM |
| 预订竞争条件 | 通知前先提交 `try_book()` 到线程池 | 预订与通知并发执行，早到服务器 2–4 秒 |
| API 限流 | 429 退避重试（30s / 60s）+ 5 分钟冷却 + 自适应降速 | 三层防御：scraper 重试、monitor 冷却、自适应间隔守住安全阈值 |
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
# 要求 Python 3.11+
pip install -r requirements.txt
cp .env.example .env
```

### 本地启动

```bash
# 1. 验证抓取是否正常（不写库、不发通知）
python monitor.py --test

# 2. 启动 Web 面板，在「用户管理」页面添加第一个用户
python web.py              # http://127.0.0.1:5000

# 3. 单次运行，验证完整通知流程
python monitor.py --once

# 4. 持续监控（后台运行）
nohup python monitor.py > logs/monitor.log 2>&1 &
```

> **提示**：首次启动时，若 `data/users.json` 不存在且 `.env` 中配置了旧版通知变量，会自动迁移为默认用户，无需手动配置。

### Docker 部署（VPS / 服务器）

要求：Docker + Docker Compose v2

```bash
# 1. 从模板创建 .env（Docker 运行时会挂载此文件）
cp .env.example .env
#    编辑 .env，至少设置 WEB_PASSWORD

# 2. 提前创建运行时目录，防止 Docker 把它们当成文件挂载
mkdir -p data logs

# 3. 构建镜像
docker compose build

# 4. 后台启动
docker compose up -d

# 5. 实时查看日志
docker compose logs -f

# 6. 停止
docker compose down
```

容器内 `monitor.py` 和 `web.py` 由 supervisord 同时管理，进程崩溃会自动重启，日志写入宿主机的 `./logs/` 目录。

**VPS 首次配置流程：**
1. `docker compose up -d` 后在浏览器打开 `http://<服务器IP>:5000`
2. 进入「用户管理」添加第一个用户，渠道选择 Telegram 或 Email（iMessage 需要 macOS，非 Mac 环境会自动跳过）
3. 进入「设置」选择要监控的城市
4. 点击「立即生效」热重载配置，无需重启容器

**更新版本：**
```bash
git pull
docker compose build --no-cache
docker compose up -d
```

**配置 HTTPS（推荐）：**

在 VPS 上用 nginx 做反向代理并终止 TLS，关键是要为 SSE 关闭缓冲：
```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # SSE 实时推送必须关闭 nginx 缓冲，否则通知会积压后才到达浏览器
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
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

# ── 自适应智能轮询（高峰期）──────────────────────────────────────
PEAK_INTERVAL=60            # 高峰期起始间隔 / 退避目标（秒）
MIN_INTERVAL=15             # 自适应下限，不得低于此值（秒）
PEAK_START=08:30            # 高峰开始（荷兰本地时间）
PEAK_END=10:00              # 高峰结束（荷兰本地时间）
PEAK_WEEKDAYS_ONLY=true     # 仅工作日启用
JITTER_RATIO=0.20           # 每次等待叠加的随机抖动比例

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

## 路线图

### ~~🔴 高优先级~~ ✅ 已完成

**Docker 打包部署**：`Dockerfile` + `docker-compose.yml` + `supervisord.conf` 已实现，monitor + Web 面板打包为单一容器，支持 VPS 24/7 不间断运行。iMessage 非 macOS 自动跳过，Web 面板 SSE 通知接管。详见上方「[Docker 部署](#docker-部署vps--服务器)」章节。

---

### 🟡 中优先级

**Lottery 摇号自动报名**
- 对 "Available in lottery" 房源自动提交申请，探索 `registerInterest` 等 GraphQL mutation
- 风险：需账号鉴权，平台规则可能限制频繁报名

**每日摘要推送**
- 每天定时推送汇总消息：当日新增 N 套、状态变更 M 套、数据库共 X 套
- 替代心跳消息，信息密度更高，减少打扰

**Discord 通知渠道**
- Webhook 实现，零额外费用，适合多人共享（朋友 / 家人群组）

**价格历史追踪**
- 记录同一房源的租金变化（`price_history` 表），检测降价并推送通知

---

## 文件结构

```
monitor.py          主调度循环，自适应智能轮询，热重载，并发预订
scraper.py          GraphQL 抓取，curl_cffi，自动翻页，429 重试，代理支持
storage.py          SQLite：listings / status_changes / web_notifications / meta，chart 聚合
models.py           Listing dataclass，price_display，feature_map
notifier.py         BaseNotifier → iMessage（macOS 检测）/ Telegram / Email / WhatsApp / WebNotifier
booker.py           登录 → 取消 pending 订单 → addNewBooking → placeOrder → idealCheckOut → 付款直链
config.py           全局配置加载，KNOWN_CITIES（26 城市），ListingFilter，AutoBookConfig
users.py            UserConfig，users.json 读写，.env 配置迁移
web.py              Flask 面板，Session 鉴权，SSE 流，通知 API，用户 CRUD，/api/reload
templates/
  base.html         布局，导航栏，铃铛通知（SSE + Toast），日夜主题（Anti-FOUC）
  login.html        登录页
  index.html        仪表盘
  listings.html     房源列表（状态筛选 + 关键词搜索）
  calendar.html     入住日历（月视图，城市筛选）
  stats.html        数据统计（Chart.js：趋势 / 分布 / 价格区间）
  users.html        用户管理列表
  user_form.html    用户新增 / 编辑表单（含 iMessage 平台警告）
  settings.html     全局设置（轮询 / 城市 / 自适应智能轮询）
Dockerfile          单容器镜像（python:3.11-slim + supervisord）
supervisord.conf    同时管理 monitor.py + web.py，含日志轮转和自动重启
docker-compose.yml  卷挂载（data/、logs/、.env），端口映射，健康检查
.dockerignore       排除 .env、data/、logs/、__pycache__ 等不进镜像
requirements.txt    curl_cffi, python-dotenv, flask, tzdata
.env.example        配置模板
data/               运行时自动生成
  listings.db       SQLite 数据库
  users.json        用户配置（通知渠道 / 过滤 / 预订账号）
  monitor.pid       监控进程 PID，供热重载使用
logs/               日志文件（supervisord 写入 monitor.log + web.log）
```
