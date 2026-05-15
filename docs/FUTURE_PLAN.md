# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。它不是当前版本的必做清单，而是帮助后续开发时保持路线清晰，避免临时需求不断堆到核心模块里。

## 1. 移动端优化

### 目标

- 让 Web 面板在手机浏览器中达到“可日常使用”的体验。
- 优先支持只读查看、房源列表、地图、日历、通知查看等高频场景。
- 为未来 PWA 或 iOS 客户端打好 API 与交互基础。

### 当前状态

- ✅ v1.2.10 已完成全面移动端适配，可认为达到”手机浏览器可日常使用”。
- ✅ 房源列表 + Dashboard 移动端卡片视图（替代横滑表格）。
- ✅ 日历月视图/列表视图切换；月视图 grid 不再横向溢出。
- ✅ 全局触摸目标 ≥44px（`@media (pointer: coarse)`）。
- ✅ Safe-area 适配（nav-toggle / toast / 登录页 / 通知面板）。
- ✅ iOS Safari `100dvh` 修正（地图页、日志页）。
- ✅ Dashboard 自动刷新改为 Page Visibility API（标签页隐藏时暂停）。
- ✅ 多选筛选器空值占位文本显示”不限”/”All”。
- ✅ Toast 窄屏宽度约束。
- ✅ System 页长路径换行 + 表格 overflow-x 包裹。

### 推荐改进

- 继续打磨设置页和用户编辑页这类长表单：可以考虑分组折叠、步骤化配置、保存前摘要和更明确的错误定位。
- 为日志页增加更适合手机的查看方式，例如级别筛选、关键词搜索、错误块折叠、复制当前错误。
- 为系统页保留管理后台定位，不必追求 App 级体验，但需要确保长文本、路径、版本号和错误信息不会撑破布局。
- 针对真实 iPhone Safari 再做一轮手动验收，重点检查底部安全区、输入框聚焦时键盘遮挡、横竖屏切换。
- 加入基础 PWA 能力：`manifest.webmanifest`、应用图标、`theme-color`、静态资源 service worker 缓存。

### 建议阶段

- Medium term：优化设置页、用户表单、日志页等低频但复杂页面的移动端交互。
- Long term：实现 PWA，可安装到主屏幕，并为 iOS 客户端复用同一套 API。

## 2. `monitor.py` 和 `storage.py` 重构

### 目标

- 降低核心流程复杂度，让监控、通知、自动预订、错误恢复、状态持久化的职责更清晰。
- 让关键流程更容易测试，尤其是抓取失败、连续 cooldown、自动预订、通知失败等边界场景。
- 为后续扩展更多数据源、更多通知渠道或更复杂的预订策略留出空间。

### 当前状态

- ✅ **v1.3.0** — `monitor.py` 抽出 `mcore/` 包（interval / prewarm / booking，1,235→971 行）；`storage.py` 拆为 6 个 Mixin（`mstorage/` 包，1,177→17 行 re-export）。
  - `_base.py`（114 行）— 连接 / schema / meta / 生命周期
  - `_listings.py`（258 行）— diff / mark_notified / 面板查询 / filter helper
  - `_charts.py`（219 行）— 10 统计图表 + 2 helper
  - `_notifications.py`（72 行）— web_notifications CRUD
  - `_map_calendar.py`（96 行）— 地图坐标缓存 + 日历查询
  - `_retry.py`（35 行）— 竞败重试队列持久化
- 对外接口完全不变：`from storage import Storage` 继续可用（通过 `class Storage(ListingOps, NotificationOps, ChartOps, MapCalendarOps, RetryQueueOps, StorageBase)` 组合）。
- 561 测试全部通过，新增 88 个针对 mcore/mstorage 的单元测试。
- `run_once()` 和 `main_loop()` 保持线性函数式风格，未拆成类——当前单数据源场景下拆类无实际收益。

### 推荐改进

- `monitor.py` 后续（优先级低）：
  - `run_once()` 内部 Phase C/D/E 可各抽为独立函数，降低单函数行数。
  - `main_loop()` 保持 while True 即可——无多数据源需求时状态机无必要。
- 统一错误类型和返回结果对象，避免核心流程依赖字符串判断。
- 考虑为 `web.py` / `app/` 路由层做类似拆分（当前 ~50 个路由分布在 11 个模块中，尚可接受）。

### 建议阶段

- ~~Short term：抽出纯函数或小服务~~ ✅ 已完成（v1.3.0，`mcore/` + `mstorage/`）。
- Medium term：为 `run_once()` 内部阶段抽出独立函数；统一错误类型。
- Long term：为多数据源/多实例部署做准备（当前无需求）。

## 3. iOS 客户端开发

### 目标

- 提供比手机网页更顺手的查看体验。
- 聚焦查看和提醒，不急于把所有管理功能搬到手机端。
- 尽量复用现有 Web API，避免维护两套业务逻辑。

### 仓库组织

第一阶段建议把 iOS 客户端作为同仓库下的独立子项目，而不是马上拆成独立仓库：

```text
holland2stay-monitor/
  app/
  templates/
  static/
  tests/
  docs/
  ios/
    FlatRadar/
      FlatRadarApp.swift
      API/
      Models/
      Views/
      State/
    FlatRadar.xcodeproj
```

这样可以让后端 API、权限逻辑和 iOS 数据模型在同一个 PR 中同步演进，同时又不会让 Xcode 工程文件污染 Python 后端目录。等 iOS 客户端有独立发布节奏、独立 CI 或多人协作需求后，再考虑拆成单独仓库。

### 推荐定位

- 第一版 iOS 客户端建议做成 companion app，而不是完整管理后台。
- 核心功能：
  - Dashboard 状态查看。
  - 房源列表与筛选。
  - 房源详情、地图、日历。
  - Web notifications 历史记录。
  - Monitor 状态查看。
- 管理功能可以后置：
  - 用户管理。
  - 全局设置。
  - 自动预订配置。
  - 日志查看。

### 第一版功能范围

第一版建议只做只读能力，减少安全面和维护成本：

- 服务器连接设置：填写服务器 URL，保存到 Keychain / AppStorage。
- 登录或 token 配置：保存短期 token，不在本地明文保存管理员密码。
- Dashboard：显示 monitor 运行状态、最新抓取时间、本轮数量、今日新增、状态变化。
- Listings：房源列表、状态/城市/租金筛选、点击打开 Holland2Stay 原始链接。
- Listing detail：展示价格、面积、楼层、入住日期、城市、合同类型、租客类型、energy label。
- Notifications：展示 Web notifications 历史、未读数量、错误/booking/result 分类。
- Map / Calendar：可以先只做其中一个，优先地图或列表详情，避免第一版范围过大。

第一版不建议包含：

- 修改用户配置。
- 修改全局设置。
- 启停 monitor。
- 自动预订配置。
- 清空数据库、清日志、reload、shutdown 等高风险管理动作。

这些功能可以等认证、审计和移动端交互稳定后再逐步加入。

### 前置条件

- API 需要更稳定的版本化约定，例如 `/api/v1/...`。
- 需要明确认证方式：session cookie 对浏览器友好，但移动客户端更适合 token 或短期 app password。
- 需要审查 guest/admin 权限边界，确保移动端不会绕过 Web 面板的 RBAC。
- 需要考虑通知策略：继续依赖 Telegram/Email/Web 通知，还是接入 APNs。
- 需要明确公开部署场景下的安全边界，例如 HTTPS、rate limit、登录失败限制、设备丢失后的 token revoke。

### API 准备

iOS 客户端开发前，建议先整理一组稳定的只读 API：

```text
GET /api/v1/status
GET /api/v1/listings?city=&status=&q=&limit=&offset=
GET /api/v1/listings/{id}
GET /api/v1/notifications?limit=&offset=
POST /api/v1/notifications/read
GET /api/v1/calendar
GET /api/v1/map
```

接口建议统一返回结构：

```json
{
  "ok": true,
  "data": {},
  "error": null
}
```

错误响应也保持稳定：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "unauthorized",
    "message": "Login required"
  }
}
```

认证建议：

- 短期方案：后台生成一个只读 API token，iOS 使用 `Authorization: Bearer <token>`。
- 中期方案：支持 token revoke、过期时间、最后使用时间和设备备注。
- 不建议：让 iOS 客户端长期保存 Web 管理员密码。

### SwiftUI 项目结构

推荐使用 SwiftUI + async/await + URLSession，保持依赖轻量：

```text
ios/FlatRadar/
  App/
    FlatRadarApp.swift
  API/
    APIClient.swift
    APIError.swift
    APIModels.swift
  Models/
    Listing.swift
    MonitorStatus.swift
    NotificationItem.swift
  State/
    AppState.swift
    AuthState.swift
  Views/
    DashboardView.swift
    ListingsView.swift
    ListingDetailView.swift
    NotificationsView.swift
    SettingsView.swift
  Components/
    ListingCard.swift
    StatusBadge.swift
    EmptyStateView.swift
```

网络层职责：

- `APIClient` 统一管理 `baseURL`、token、请求头、HTTP 错误和 JSON decode。
- `APIModels` 定义后端响应 envelope。
- `AuthState` 管理 token、登录状态、退出登录。
- `AppState` 管理全局刷新、错误 toast、当前服务器连接状态。

### UI 信息架构

推荐使用底部 Tab：

- Dashboard：状态概览。
- Listings：房源列表和筛选。
- Notifications：通知历史。
- More：服务器设置、账号、关于。

如果加入地图和日历：

- 地图可以作为 Listings 的右上角切换入口，而不是独立一级 Tab。
- 日历可以放在 More 或 Listings 的筛选视图里，避免第一版导航过重。

### 安全与隐私

- token 存 Keychain，不存 UserDefaults。
- App 内不要展示完整代理密码、H2S 密码、SMTP 密码等敏感字段。
- 所有请求强制 HTTPS；开发环境可以允许手动配置 `http://localhost`，但默认生产配置必须拒绝明文 HTTP。
- iOS 客户端只读 token 应该不能访问 settings、users、logs、shutdown、reset database 等接口。
- 后端应记录 token 最近使用时间和来源设备备注，方便用户撤销。

### 测试与发布

- 后端先补 API contract tests，确保 iOS 依赖的响应字段不会随意变化。
- iOS 侧至少覆盖：
  - API decode 测试。
  - 空数据状态。
  - 401/403/500 错误展示。
  - token 缺失和服务器 URL 无效。
- CI 可以后置。早期可以本地 Xcode 构建；准备发布时再加入 GitHub Actions macOS runner。
- 上架 App Store 前需要确认项目用途、免责声明、隐私说明和 Holland2Stay 非官方关系表达是否足够清晰。

### 技术路线

- Short term：先把 Web 做成 PWA，验证移动端信息架构和 API 是否够用。
- Medium term：整理 `/api/v1` API 契约，补充 OpenAPI 或简化版 API 文档，并加入只读 token。
- Long term：在同仓库 `ios/` 下使用 SwiftUI 开发原生 iOS 客户端，优先做只读信息展示和通知中心，再逐步加入管理功能。

## 4. iOS 客户端后端规划（APNs + 三档鉴权）

> 本节是 §3 的可执行落地方案。前提：VPS Docker 已部署，主程序运行在 VPS。
> iOS App 定位为"查看 + 接收推送"，后期再加设置功能。

### 4.0 硬约束（来自需求方）

1. **现有推送方式完全不动**。iMessage / Telegram / WhatsApp / Email 四个渠道沿用 `notifier.py` 既有逻辑；APNs 作为**新增的并行第 5 渠道**，不替换任何现有通道，也不修改 `users.json` 现有字段语义。
2. **App 端鉴权分三档**：
   | 角色 | 来源 | 能力 |
   |---|---|---|
   | **admin** | `WEB_PASSWORD`（与 Web 后台共用） | 全部数据 + 后期管理 API |
   | **user** | `users.json` 每条用户独立账号 | 自己关心的数据 + 自己的 APNs 推送 |
   | **guest** | 不登录、无 token | 仅统计面板（与 Web 端访客模式对齐） |
3. **APNs 推送仅对 user 角色生效，且严格按该用户的 `listing_filter` 过滤**。复用现有 `ListingFilter.matches(listing)`，零代码重复。admin 不接收"用户口径"的房源推送（admin 想收的话可以同时登记一个 user 身份）。guest 没有 token 不能注册设备，自然没有推送。

### 4.1 总体架构

```
                  ┌────────────────────────────────────────────┐
                  │              VPS Docker（已部署）            │
                  │                                            │
  iPhone ──HTTPS─▶│  /            HTML 后台（不变，session）   │
                  │  /api/v1/*    JSON API（新增，Bearer）     │
                  │       │                                    │
                  │       ▼                                    │
                  │  monitor.py（不变）                         │
                  │   └─ notifier 多路发送：                    │
                  │        iMessage / Telegram / Email /        │ ← 现有，不动
                  │        WhatsApp                             │
                  │        + APNs（新增钩子，fire-and-forget）  │ ← 新增
                  └────────────────────────────────────────────┘
                                ▲                ▲
                                │ APNs HTTP/2    │ 现有渠道
                                ▼                ▼
                          Apple APNs        Telegram / SMTP / ...
                                │                │
                                └──── iPhone ────┘
                              （两套通知都会收到）
```

APNs 是 notifier 内部新增的第 5 个渠道，复用现有"多用户 × 多渠道 × 过滤"骨架，不破坏任何旧链路。

### 4.2 鉴权系统设计

#### 三档角色来源对齐

```
admin   ← WEB_PASSWORD（.env）
            App 端 username=__admin__ + password=WEB_PASSWORD 登录

user    ← users.json 每条记录
            UserConfig 新增 app_password_hash 字段（bcrypt）
            App 端 username=<UserConfig.name> + password=<明文> 登录

guest   ← 无凭证
            App 启动选择"以访客继续"，本地标记 role=guest
            仅可调用 /api/v1/stats/public/* 白名单端点
```

把 admin 也走 token 化的目的：让 Web HTML 后台（cookie session）和 App API（Bearer）彻底分离，将来撤销/换密码互不影响。Web 后台 cookie session 行为**完全不动**。

#### users.json 字段增量（向后兼容）

```jsonc
{
  "name": "kong",
  "id": "ea7dfce1",
  /* ... 现有字段全部不动 ... */

  // 新增 ↓
  "app_password_hash": "$2b$12$....",     // bcrypt；空字符串=禁止 App 登录
  "app_login_enabled": false              // 默认禁用更安全；Web 管理页可启用
}
```

`users.py` 的 `UserConfig` dataclass 加两个字段，`load()` / `save()` 自动迁移（缺字段补默认值）。

#### 新表：app_tokens

```sql
CREATE TABLE app_tokens (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash      TEXT UNIQUE NOT NULL,    -- sha256(token)；明文仅签发时返回一次
  role            TEXT NOT NULL,           -- 'admin' | 'user'
  user_id         TEXT,                    -- role=user 时指向 UserConfig.id；admin 为 NULL
  device_name     TEXT NOT NULL,           -- "iPhone 15 Pro"
  created_at      TEXT NOT NULL,
  last_used_at    TEXT,
  expires_at      TEXT,                    -- NULL=永不过期；建议默认 90 天滚动
  revoked         INTEGER DEFAULT 0
);
CREATE INDEX idx_app_tokens_user ON app_tokens(user_id, revoked);
```

#### 新表：device_tokens

```sql
CREATE TABLE device_tokens (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  app_token_id    INTEGER NOT NULL,        -- FK app_tokens.id，token 撤销时连带失效
  device_token    TEXT NOT NULL,           -- APNs hex token，每次启动可能轮换
  env             TEXT NOT NULL,           -- 'production' | 'sandbox'（TestFlight=production）
  platform        TEXT NOT NULL DEFAULT 'ios',
  model           TEXT,
  bundle_id       TEXT,
  created_at      TEXT NOT NULL,
  last_seen       TEXT NOT NULL,
  disabled_at     TEXT,
  disabled_reason TEXT,
  UNIQUE(app_token_id, device_token)
);
CREATE INDEX idx_device_tokens_active
   ON device_tokens(app_token_id) WHERE disabled_at IS NULL;
```

存储层放到 `mstorage/_tokens.py` + `mstorage/_devices.py` 两个新 mixin，沿用 `_retry.py` 风格。

#### 鉴权中间件骨架

```python
# app/api_auth.py
def bearer_required(allow_roles=("admin", "user")):
    def deco(f):
        @wraps(f)
        def w(*a, **kw):
            tok = _extract_bearer()
            if not tok:
                return _err(401, "unauthorized")
            row = _validate(tok)                 # hash + revoked + expires_at + LRU 缓存
            if not row or row["role"] not in allow_roles:
                return _err(403, "forbidden")
            g.api_role = row["role"]
            g.api_user_id = row["user_id"]       # admin 时为 None
            g.api_token_id = row["id"]
            _schedule_touch(row["id"])           # last_used_at 异步落盘
            return f(*a, **kw)
        return w
    return deco

def public_endpoint(f):
    """guest 也能访问；不强制 token，但若带了 token 仍解析（统一日志/限流）。"""
    ...
```

实现要点：
- **每请求不读 SQLite**：token 验证用 5-min TTL 内存缓存（dict 或 `cachetools.TTLCache`）。
- **last_used_at 异步**：用 `set[int]` 收集需更新的 token_id，后台任务每 30 秒批量 flush。
- **失败登录沿用** `app/auth.py:check_login_rate` 的 IP 退避。

#### 端点权限矩阵

| 端点 | guest | user | admin |
|---|:-:|:-:|:-:|
| `POST /api/v1/auth/login` | ✅ | ✅ | ✅ |
| `POST /api/v1/auth/logout` |  | ✅ | ✅ |
| `GET  /api/v1/auth/me` |  | ✅ | ✅ |
| `GET  /api/v1/stats/public/summary` | ✅ | ✅ | ✅ |
| `GET  /api/v1/stats/public/charts/<key>` | ✅ | ✅ | ✅ |
| `GET  /api/v1/listings` |  | ✅（应用本人 filter）| ✅（全量） |
| `GET  /api/v1/listings/<id>` |  | ✅ | ✅ |
| `GET  /api/v1/map` |  | ✅ | ✅ |
| `GET  /api/v1/calendar` |  | ✅ | ✅ |
| `GET  /api/v1/notifications` |  | ✅（仅本人）| ✅（全部）|
| `POST /api/v1/notifications/read` |  | ✅ | ✅ |
| `GET  /api/v1/notifications/stream`（SSE）|  | ✅ | ✅ |
| `POST /api/v1/devices/register` |  | ✅ | ✅* |
| `DELETE /api/v1/devices/<id>` |  | ✅ | ✅ |
| `GET  /api/v1/me/filter` |  | ✅ |  |
| `PUT  /api/v1/me/filter`（后期） |  | ✅ |  |
| `/api/v1/admin/...`（后期） |  |  | ✅ |

\* admin 注册设备仅用于"以 admin 身份调试"，不触发 user-scoped 推送。

#### user 视角的数据隔离

user 角色访问数据接口时，Storage 查询要按 `g.api_user_id` 过滤：

- `/listings`：用 `UserConfig.listing_filter` 在 SQL 层或 Python 层过滤
- `/notifications`：`WHERE user_id = ?` —— 由 `web_notifications` 新增 `user_id` 列实现（见 §4.6 schema 演进）
- `/devices`：`WHERE app_token_id = g.api_token_id`

admin 不加这层过滤。

### 4.3 APNs 子系统

#### 模块结构

```
notifier.py                            # 现有；主体逻辑不动
└── send_to_user(listing, user) 末尾增加：
       asyncio.create_task(push.dispatch(user, listing, kind="new"))

mcore/push.py                          ★ 新增：调度入口
  ├─ async dispatch(user, listing, kind="new"|"booked"|"status_change")
  │    ├─ 查 storage.get_active_devices(user.id)
  │    ├─ 构造 payload（标题/正文/collapse-id）
  │    ├─ 节流去重（§4.3.6）
  │    └─ 并发调用 ApnsClient.send_many()
  ├─ async dispatch_aggregate(user, listings, kind="round")
  └─ async dispatch_error(user, message, kind="blocked"|...)

notifier_channels/apns.py              ★ 新增：APNs HTTP/2 客户端
  ├─ class ApnsClient
  │    ├─ _provider_token()     # JWT(ES256, .p8)，缓存 30min
  │    ├─ async send_one(device_token, payload, headers) -> ApnsResult
  │    └─ async send_many(...)  # http2 多路复用，并发 16
  ├─ class ApnsResult(status, reason, retry_after)
  └─ 410/400 → mark device disabled
```

#### 与现有 notifier 的接合

在 `notifier.py` 的 `send_to_user` 末尾追加钩子：

```python
# 现有 4 个渠道发送逻辑保持不变
results.append(await _imessage_send(...))
results.append(await _telegram_send(...))
results.append(await _email_send(...))
results.append(await _whatsapp_send(...))

# ↓ 新增（fire-and-forget；失败不影响其他渠道）
asyncio.create_task(push.dispatch(user, listing, kind="new"))
```

设计原则：
- **APNs 失败不阻塞其他渠道**，反之亦然
- "是否发送"判断**与现有渠道完全一致**：`user.enabled && user.notifications_enabled && user.listing_filter.matches(listing)`
- "具体内容"由 `mcore/push.py` 独立组织（推送通知短一点更合适），**不复用 iMessage 长文本模板**

#### Payload 规范

```jsonc
// 单条新房源（默认）
{
  "aps": {
    "alert": {
      "title": "🏠 Eindhoven 新房源",
      "body":  "Studio Centrum · €700 · 26m² · 6/1 入住"
    },
    "sound": "default",
    "badge": 1,
    "thread-id": "listings",
    "mutable-content": 1
  },
  "kind": "new",
  "listing_id": "MA12345",
  "deep_link": "h2smonitor://listing/MA12345"
}

// 一轮聚合（同一用户当轮匹配 ≥3 套时改聚合，避免刷屏）
{
  "aps": {
    "alert": {
      "title": "🏠 本轮 5 套新房源",
      "body":  "Eindhoven 3 · Amsterdam 2 · 点开查看"
    },
    "thread-id": "listings"
  },
  "kind": "round",
  "round_id": "2026-05-15T08:30:00Z"
}

// 自动预订成功
{
  "aps": { "alert": {...}, "sound": "default" },
  "kind": "booked",
  "listing_id": "MA12345"
}
```

**关键 header**：
- `apns-topic`: Bundle ID
- `apns-priority`: `10`（alert）
- `apns-push-type`: `alert`
- `apns-expiration`: now + 1h（时效优先，过期就丢）
- `apns-collapse-id`: `round_id`（同轮新通知覆盖旧的）

#### .p8 密钥与配置

`.env` 增量：
```bash
APNS_ENABLED=true
APNS_KEY_PATH=/secrets/AuthKey_XXXXXXXXXX.p8
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=YYYYYYYYYY
APNS_TOPIC=com.kong.h2smonitor
APNS_ENV_DEFAULT=production
APNS_CONCURRENCY=16
```

Docker：
- `.p8` 通过 Docker secret 或只读 volume `./secrets:/secrets:ro` 挂载
- `.gitignore` 加 `secrets/*.p8`
- `requirements.txt` 增加 `pyjwt[crypto] >= 2.8`、`httpx[http2] >= 0.27`

#### 错误处理与回收

| 状态 | reason | 处理 |
|---|---|---|
| 200 | — | 正常 |
| 400 | `BadDeviceToken` | `device_tokens.disabled_at = now` |
| 410 | `Unregistered` | 同上 |
| 403 | `InvalidProviderToken` | 强制刷新 JWT，重试一次 |
| 429 | `TooManyRequests` | 退避 + 重试（`apns-retry-after`） |
| 500/503 | — | 重试一次，仍失败写 `errors.log` |

所有失败写 `data/errors.log`（沿用现有 errors logger），不影响 listing 入库或其他渠道。

#### 节流与防刷屏（push.py 内实现）

- **每用户每分钟上限** 默认 10 条，超出聚合为 1 条 `round` 推送
- **重复抑制** 同一 `(user_id, listing_id, kind)` 5 分钟内最多 1 条
- **403/error 推送** 复用现有 `monitor.py:_should_notify_block()` 30 分钟节流

### 4.4 API v1 路由清单

```
app/routes/api_v1/
├── __init__.py              # bp = Blueprint("api_v1", url_prefix="/api/v1")
│                            # CSRF 对此 bp 全免（Bearer 不来自浏览器 cookie）
├── auth.py                  # /auth/{login, logout, me}
├── stats_public.py          # /stats/public/*   ← guest 可访问
├── listings.py              # /listings, /listings/<id>
├── map.py                   # /map
├── calendar.py              # /calendar
├── notifications.py         # /notifications, /notifications/read, /notifications/stream
├── devices.py               # /devices/{register, list, delete}
├── me.py                    # /me/{filter, summary}
└── admin.py                 # /admin/*（后期）

app/api_auth.py              # bearer_required / public_endpoint / role 矩阵
app/api_errors.py            # 统一错误壳：{ok:false, error:{code,message}}
```

统一响应壳：
```jsonc
{ "ok": true,  "data": { ... } }
{ "ok": false, "error": { "code": "unauthorized", "message": "登录已过期" } }
```

错误码：`unauthorized` / `forbidden` / `not_found` / `validation` / `rate_limited` / `server_error` / `apns_disabled` / `device_disabled`。

### 4.5 部署改动（VPS Docker 现状增量）

| 改动 | 内容 |
|---|---|
| Dockerfile | 无需改动（依赖通过 requirements.txt） |
| docker-compose.yml | 增加 `./secrets:/secrets:ro` 挂载；环境变量传 `APNS_*` |
| .env / 配置 | 新增 `APNS_*` 六项；建议加 `APP_TOKEN_DEFAULT_TTL_DAYS=90` |
| 反向代理 | `/api/v1/` 无特殊配置；SSE 端点要禁 buffer：`proxy_buffering off`（nginx）/ `flush_interval -1`（Caddy） |
| 日志 | `data/errors.log` 增加 APNs 错误源标签 |
| 备份 | `data/listings.db` 自然包含新表，备份策略不变 |

### 4.6 Schema 演进与数据迁移

新增 2 张表，并对 `web_notifications` 加 1 列：

```sql
ALTER TABLE web_notifications ADD COLUMN user_id TEXT;
CREATE INDEX idx_web_notif_user ON web_notifications(user_id, read);
```

迁移策略（沿用 `mstorage/_base.py:_migrate()`）：
- 三处变更进 `_migrate()`，用 `CREATE TABLE IF NOT EXISTS` 和 `ALTER TABLE ADD COLUMN`（带 `try/except`，老库 ADD 失败时忽略）
- 旧通知 `user_id = NULL`，UI 视为"系统通知"
- 不需要 stop-the-world，运行中迁移即可

### 4.7 分阶段实施

#### Phase 1 · 鉴权 + API 框架（2-3 天）

- [ ] 1.1 `app/api_auth.py`：Bearer decorator + 三档角色 + LRU 缓存
- [ ] 1.2 `mstorage/_tokens.py`：`app_tokens` 表 + CRUD（含 last_used 异步落盘）
- [ ] 1.3 `users.py`：`UserConfig` 加 `app_password_hash` + `app_login_enabled`；bcrypt 加密；`users.json` 自动迁移
- [ ] 1.4 `app/routes/api_v1/auth.py`：`/auth/{login, logout, me}`，复用 `check_login_rate`
- [ ] 1.5 `app/routes/api_v1/stats_public.py`：guest 白名单端点
- [ ] 1.6 Web 后台 `/settings/app-accounts` 页：admin 看/撤销所有签发 token
- [ ] 1.7 curl 跑通 admin/user/guest 三档登录与权限边界

**交付物**：`curl -H "Authorization: Bearer xxx"` 能拿到 `/api/v1/auth/me`，正确区分三档身份。

#### Phase 2 · 只读数据端点（2 天）

- [ ] 2.1 `listings.py` / `map.py` / `calendar.py` / `notifications.py` / `me.py`
- [ ] 2.2 user 视角的 SQL 过滤（应用 `listing_filter`）
- [ ] 2.3 SSE `/notifications/stream` 复用现有发布器 + user_id 过滤
- [ ] 2.4 `web_notifications.user_id` schema 演进 + 写入路径补 user_id

**交付物**：iOS 模拟器能拿到分用户的数据；admin 拿到全量。

#### Phase 3 · APNs 子系统（2-3 天）

- [ ] 3.1 Apple Developer 申请 + .p8 下载（你并行做，1-3 天审批）
- [ ] 3.2 `notifier_channels/apns.py`：JWT 签名 + httpx http2
- [ ] 3.3 `mstorage/_devices.py`：`device_tokens` 表 + CRUD + disable
- [ ] 3.4 `app/routes/api_v1/devices.py`：register / list / delete
- [ ] 3.5 `mcore/push.py`：dispatch / aggregate / error；节流去重
- [ ] 3.6 `notifier.py` 钩子：`send_to_user` 末尾调 `push.dispatch`
- [ ] 3.7 单元测试：mock `ApnsClient.send_one`，验证过滤、聚合、节流、410 回收

**交付物**：iOS 真机能收到 VPS 推出的本机 APNs 通知，内容符合该用户的 filter。

#### Phase 4 · iOS 客户端 MVP（5-7 天，可与 Phase 3 部分并行）

详见本文 §3。关键接合：
- 启动注册 APNs → `POST /api/v1/devices/register`
- Universal Link / `h2smonitor://` deep link → 跳房源详情
- 三档登录界面：admin（密码）/ user（用户名+密码）/ guest（"以访客继续"）

#### Phase 5 · 后期写操作（可选，1-2 周）

- [ ] `/api/v1/me/filter` PUT：user 自助改过滤条件
- [ ] `/api/v1/admin/*`：用户 CRUD、启停监控、配置
- [ ] iOS 设置 tab 按 role 显示

### 4.8 测试策略

| 层级 | 覆盖点 |
|---|---|
| 单元 | bcrypt 哈希、token 校验、filter 过滤、APNs payload 构造、JWT 缓存 |
| 集成 | curl 跑三档登录与权限矩阵；mock APNs server 验证 410 回收 |
| 端到端 | 真机收到 APNs；user 视角数据隔离；guest 越权 403 |
| 回归 | 现有 579 测试继续通过；新增 ≥30 测试覆盖新模块 |

APNs 客户端要可 mock（注入 `transport` 参数，测试用 `httpx.MockTransport`）。

### 4.9 风险与缓解

| 风险 | 缓解 |
|---|---|
| Apple Developer 审批延迟 1-3 天 | Phase 1+2 不依赖 Apple，并行推进；Phase 3 等密钥就绪 |
| .p8 密钥泄漏 | Docker secret + `.gitignore` + 仅 read 权限；定期到 developer.apple.com 轮换 |
| device_token 轮换 | 每次 App 启动重新 register；`UNIQUE(app_token_id, device_token)` 去重 |
| TestFlight 用 production endpoint | 注册时传 `env` 字段，后端按字段路由 endpoint，default 用 production |
| Cloudflare 屏蔽抓取（VPS IP 风险） | 已在 booker 处理 403 节流；本方案不放大风险 |
| 用户改 `users.json` 后 App 登录失效 | Web 改密码同步更新 `app_password_hash` |
| 推送刷屏 | §4.3.6 节流策略 + collapse-id |
| 老库 schema 演进 | `ALTER TABLE ... ADD COLUMN` 幂等；try/except 吞已存在错误 |

### 4.10 与 §3 的关系

- **§3** 偏长期路线、仓库组织、UI 设计哲学
- **§4（本节）** 偏短期可落地的后端方案：鉴权、APNs、API 契约
- Phase 完成后摘要回填到 §3 状态行

---

## 推荐优先级

1. ~~移动端 Web 体验问题~~ ✅ v1.2.10 已完成。
2. ~~`monitor.py` 低风险拆分~~ ✅ v1.2.10 已完成（`mcore/` 包）。
3. ~~`storage.py` 按 repository 拆分~~ ✅ v1.3.0 已完成（`mstorage/` 包，6 个 Mixin）。
4. **iOS 客户端后端 §4 — Phase 1：鉴权 + API 框架**（高优，2-3 天）
5. **iOS 客户端后端 §4 — Phase 2：只读数据端点**（高优，2 天）
6. **iOS 客户端后端 §4 — Phase 3：APNs 子系统**（高优，等 Apple Developer 密钥）
7. iOS 客户端 SwiftUI MVP（§3 + §4 Phase 4）
8. 设置页/用户表单等低频页面的移动端交互优化（分组折叠、步骤化）。
9. iOS 客户端写操作（§4 Phase 5）。
