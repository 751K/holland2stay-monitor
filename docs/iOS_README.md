# FlatRadar iOS 客户端 & API 后端

## 概览

FlatRadar 是 Holland2Stay 房源监控系统的 iOS 客户端，通过 REST API 连接到自托管 Flask 后端。

```
┌─────────────────────┐       Bearer Token        ┌──────────────────────┐
│   iOS App (SwiftUI)  │ ◄──────────────────────► │  Flask Server (VPS)  │
│   /ios/FlatRadar/    │     /api/v1/*             │  web.py :8088        │
└─────────────────────┘                            └──────────────────────┘
```

---

## iOS 客户端

### 技术栈

- **语言**：Swift 6.3
- **UI 框架**：SwiftUI（`@Observable @MainActor` 宏模式）
- **网络层**：`APIClient` + `URLSession`（async/await）+ `SSEClient`（实时推送）
- **持久化**：Keychain（Bearer token）+ UserDefaults 降级 + `@AppStorage`

### 功能矩阵

| 功能 | 状态 | 说明 |
|------|------|------|
| 三档登录（admin/user/guest） | ✅ | admin=WEB_PASSWORD，user=H2S 凭据回退验证 |
| Bearer Token 持久化 | ✅ | Keychain + UserDefaults 降级 |
| Dashboard 实时统计 | ✅ | 公开统计 + user 专属匹配卡片 |
| Listings 列表 + 搜索 + 翻页 | ✅ | searchable + 无限滚动 + 状态胶囊标签 |
| Listing 详情页 | ✅ | 全字段 + feature_map 网格 + H2S 链接 |
| Map 地图视图 | ✅ | MapKit + 状态色 pin + deep link + 选中卡片 |
| Calendar 日历视图 | ✅ | 月格 + 每日可入住数 + 房源列表 |
| Notifications 列表 + 翻页 | ✅ | 左滑标记已读 + 全部已读 + 类型图标 |
| Notifications 未读 badge | ✅ | Tab 角标 + App 图标 badge 实时更新 |
| SSE 实时推送 | ✅ | `/notifications/stream` + 指数退避重连 + Live/Idle 指示器 |
| APNs 设备注册 + Test Push | ✅ | Settings 一键自测，验证端到端链路 |
| Deep link | ✅ | `h2smonitor://listing/<id>` + APNs `listing_id` |
| 深色模式 | ✅ | Light / Dark / System 三选一 + 语义色自适应 |
| 多语言 en + zh-Hans | ✅ | String Catalog 154 条，跟随系统自动切换 |
| 错误展示打磨 | ✅ | APIError 分类 UI + 全局 401/403 自动登出 + Try Again |
| iPad 适配 | ✅ | 底栏 6 tab（List/Map/Calendar 直接展开）+ 3 列网格 + ⌘1-⌘6 |
| 键盘快捷键 | ✅ | ⌘1-6 切换 tab（iPad 外接键盘） |

### 文件结构

```
ios/FlatRadar/FlatRadar/
├── FlatRadarApp.swift              # @main 入口，环境注入，深色模式/SSE 管理
├── Models/
│   ├── APIResponse.swift           # 通用信封 + 分页/Device/Me/Map/Calendar 响应体
│   ├── Listing.swift               # 房源模型（priceValue/featureMap）
│   ├── ListingFilter.swift         # 用户过滤条件模型
│   ├── MapListing.swift            # 地图房源（坐标 + 状态）
│   ├── CalendarListing.swift       # 日历房源（入住日期 + 分组）
│   ├── NotificationItem.swift      # 通知模型（markedRead）
│   ├── AuthModels.swift            # 登录请求/响应体
│   ├── UserInfo.swift              # 用户信息（来自 /auth/me）
│   ├── MonitorStatus.swift         # 公开统计（Dashboard）
│   └── ChartData.swift             # 图表数据
├── Networking/
│   ├── APIClient.swift             # HTTP 客户端（@MainActor，Bearer 鉴权，auth 失败通知）
│   ├── APIError.swift              # 统一错误类型（LocalizedError + 分类 icon + 恢复建议）
│   ├── SSEClient.swift             # SSE 流客户端（AsyncThrowingStream）
│   └── KeychainManager.swift       # Keychain 读写封装
├── Push/
│   └── PushDelegate.swift          # UIApplicationDelegate 桥接（APNs token + 通知点击）
├── Navigation/
│   └── NavigationCoordinator.swift # @Observable 导航状态（tab/路径/deep link 协调）
├── Stores/
│   ├── AuthStore.swift             # 登录态管理（restore/login/logout + 全局 auth 失败监听）
│   ├── DashboardStore.swift        # Dashboard 公开统计 + Me 摘要
│   ├── ListingsStore.swift         # 房源分页/搜索/过滤
│   ├── MapStore.swift              # 地图房源缓存
│   ├── CalendarStore.swift         # 日历房源 + 按日分组 dict
│   ├── NotificationsStore.swift    # 通知分页/标记已读/SSE 连接管理
│   └── PushStore.swift             # APNs 设备注册/解绑/测试推送
└── Views/
    ├── ContentView.swift           # 根视图（登录 vs 主页切换）
    ├── MainTabView.swift           # 响应式 TabView（iPhone 4 tab / iPad 6 tab + 玻璃底栏）
    ├── Auth/
    │   ├── LoginView.swift         # 三种登录模式 + 错误弹窗
    │   └── LoginModePicker.swift   # admin/user/guest 选择器
    ├── Browse/
    │   └── BrowseView.swift        # iPhone Browse tab：NavigationStack + segmented picker
    ├── Dashboard/
    │   ├── DashboardView.swift     # 响应式网格卡片（2/3 列）+ sheet 弹出图表
    │   ├── StatCard.swift          # 单张统计卡片组件
    │   └── ChartDetailView.swift   # 通用图表详情（Swift Charts + 明细表格）
    ├── Listings/
    │   ├── ListingsView.swift      # 房源列表（搜索/翻页/状态标签）+ 错误状态
    │   ├── ListingRow.swift        # 单行房源（含状态胶囊）
    │   └── ListingDetailView.swift # 房源详情（全字段 + feature 网格 + H2S 链接）
    ├── Map/
    │   └── MapView.swift           # MapKit 地图（状态色 pin + 选中卡片 + 计数 badge）
    ├── Calendar/
    │   └── CalendarView.swift      # 月历（月格+入住计数+选中日房源列表）
    ├── Notifications/
    │   ├── NotificationsView.swift # 通知列表（左滑已读/全部已读/SSE 指示器/badge）
    │   └── NotificationRow.swift   # 单行通知（类型图标 + 颜色）
    └── Settings/
        └── SettingsView.swift      # Server URL / 角色 / 退出 / 深色模式 / 推送 / 设备管理
```

### 登录流程

```
用户输入凭据
    │
    ├─ __admin__ + WEB_PASSWORD  →  后端 hmac.compare_digest  →  role=admin
    │
    └─ 其他用户名 + 密码  →  后端 users.json 查 UserConfig
            │
            ├─ app_password_hash 匹配  →  role=user
            │
            └─ 未匹配  →  H2S GraphQL generateCustomerToken 验证  →  role=user
```

Token 签发后在 Keychain 持久化，App 重启时调用 `/auth/me` 验证有效性。全局捕获 401/403 自动登出。

### 数据流

```
View ──(task/refreshable)──► Store ──(async)──► APIClient ──(URLSession)──► Flask
  │                            │                    │
  │  @Observable               │  @MainActor        │  @MainActor
  │  auto-redraw               │  state mutation    │  data-race safe
```

View 通过 `@Environment(Store.self)` 注入，Store 状态变化自动触发 View 重绘。

### 身份角色

| 角色 | 登录方式 | 可见内容 |
|------|---------|---------|
| guest | 无需登录 | Dashboard（公开统计） |
| user | H2S 凭据 | Dashboard + 个人匹配卡片 + Listings（filtered）+ Notifications（per-user）+ Map/Calendar（filtered） |
| admin | `__admin__` + WEB_PASSWORD | Dashboard + 全量 Listings + 全量 Notifications + Settings（管理设备） |

### 导航架构

```
iPhone (compact)                    iPad (regular)
──────────────                      ──────────────
TabView 4 tabs:                     TabView 6 tabs:
  🏠 Dashboard                        🏠 Dashboard
  🔍 Browse [L|M|C]                  📋 Listings
      ├─ ListingsView                🗺 Map
      ├─ MapView                     📅 Calendar
      └─ CalendarView                🔔 Notifications
  🔔 Notifications                   📐 Settings
  📐 Settings
```

iPhone 上 List/Map/Calendar 合并进 Browse tab + segmented picker；iPad 空间足够，6 个 tab 直接展开到底栏，无二级嵌套。

---

## API 后端

### 端点总览

所有端点在 `/api/v1/*` 下，返回统一壳形 `{"ok": bool, "data": ...}` / `{"ok": false, "error": {"code": "...", "message": "..."}}`。

#### Phase 1 — 鉴权

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/auth/login` | 无 | 用户名+密码 → Bearer token（TTL 默认 90 天） |
| POST | `/auth/logout` | Bearer | 撤销当前 token |
| GET  | `/auth/me` | Bearer | 当前身份（role/user/filter） |
| GET  | `/stats/public/summary` | 无 | 公开统计（total/new_24h/new_7d/changes_24h） |
| GET  | `/stats/public/charts` | 无 | 图表 key 列表 |
| GET  | `/stats/public/charts/<key>` | 无 | 图表时序数据 |

#### Phase 2 — 只读数据

| 方法 | 路径 | 鉴权 | 参数 | 说明 |
|------|------|------|------|------|
| GET | `/listings` | admin/user | `?city=&status=&q=&limit=&offset=` | 分页列表，user 按 `listing_filter` 过滤 |
| GET | `/listings/<id>` | admin/user | — | 单条详情，user 不可见时 404 |
| GET | `/notifications` | admin/user | `?limit=&offset=` | 分页通知，user 双层过滤（user_id + listing_filter） |
| POST | `/notifications/read` | admin/user | `{"ids": [...]}` 或 `{}` | 标记已读（全部或指定 ids） |
| GET | `/notifications/stream` | Bearer/query | `?token=&last_id=` | SSE 增量推送 |
| GET | `/map` | admin/user | — | 已缓存坐标的房源 |
| GET | `/calendar` | admin/user | — | 有入住日期的房源 |
| GET | `/me/summary` | admin/user | — | 当前用户统计（匹配数/可订数等） |
| GET | `/me/filter` | admin/user | — | 当前用户的 listing_filter |

#### Phase 3 — APNs / 设备

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/devices/register` | admin/user | 注册/刷新 APNs device token |
| GET | `/devices` | admin/user | 列出当前会话的设备（token 脱敏） |
| DELETE | `/devices/<id>` | admin/user | 删除指定设备 |
| POST | `/devices/test` | admin/user | 发送测试推送（验证 APNs 端到端链路） |

### 服务端文件结构

```
app/
├── api_auth.py              # Bearer Token 校验（装饰器 + TTL 缓存 + 异步刷盘）
├── api_errors.py            # 统一错误响应工厂
├── auth.py                  # Web 后台鉴权（session/cookie）
├── csrf.py                  # CSRF 保护
├── db.py                    # Storage 工厂
├── forms/
│   └── user_form.py         # 表单 → UserConfig 绑定（含 bcrypt 容错）
└── routes/
    ├── api_v1/
    │   ├── __init__.py      # Blueprint 注册
    │   ├── _helpers.py      # row→Listing / apply_user_filter / serialize
    │   ├── auth.py          # 登录/登出/me（含 H2S 凭据验证）
    │   ├── stats_public.py  # 公开统计
    │   ├── listings.py      # 房源列表/详情
    │   ├── notifications.py # 通知列表/已读/SSE
    │   ├── map.py           # 地图数据
    │   ├── calendar.py      # 日历数据
    │   ├── me.py            # 当前用户摘要/filter
    │   └── devices.py       # APNs 设备管理 + 测试推送
    ├── app_accounts.py      # Web 后台：App Token 管理
    └── users.py             # Web 后台：用户 CRUD

mcore/
├── push.py                  # APNs 推送调度（dispatch/aggregate/节流去重）

mstorage/
├── _tokens.py               # app_tokens 表 + CRUD
├── _devices.py              # device_tokens 表 + CRUD + disable
├── _notifications.py        # web_notifications 表（含 user_id 过滤）
└── ...                      # listings / charts / map_calendar / retry

notifier_channels/
├── apns.py                  # APNs HTTP/2 客户端（JWT ES256 签名 + httpx）
```

### 鉴权架构

```
iOS App                          Flask Server
───────                          ────────────
Authorization: Bearer <token>
        │
        ▼
    api_auth.bearer_required()
        │
        ├─ 提取 Bearer string
        ├─ SHA256 hash
        ├─ 查内存 TTL 缓存（5min）
        │   └─ miss → 查 SQLite app_tokens 表
        ├─ 校验 revoked=0 + expires_at
        ├─ 设置 g.api_role / g.api_user_id / g.api_token_id
        └─ 异步刷 last_used_at（30s 批量）

与 Web 后台的 cookie session 完全隔离。
```

### user 数据隔离策略

| 端点 | admin | user |
|------|-------|------|
| `/listings` | 全量 | Python 侧 `apply_user_filter()`，按 `listing_filter` 过滤 |
| `/listings/<id>` | 全量 | 不可见的房源返回 404（不泄漏存在性） |
| `/notifications` | 全量 | SQL `user_id=self OR ''` + Python `listing_filter` 二次过滤 + 类型白名单 |
| `/notifications/read` | 全局 | 仅标记 `user_id=self OR ''` 的通知 |
| `/map` `/calendar` | 全量 | 从 listings 表反查 + `apply_user_filter` |

---

## 构建与运行

### 后端

```bash
cp .env.example .env          # 编辑 WEB_PASSWORD 等
pip install -r requirements.txt
python web.py                 # 监听 :8088
```

### iOS

1. Xcode 打开 `ios/FlatRadar/FlatRadar.xcodeproj`
2. Scheme: FlatRadar，Destination: iPhone / iPad 模拟器 或 My Mac
3. Build & Run
4. Settings → Server URL 填入 `127.0.0.1:8088`（模拟器）或 VPS IP

### Docker

```bash
docker build -t flatradar .
docker run -d -p 8088:8088 -v $(pwd)/data:/app/data --env-file .env flatradar
```

---

## 版本历史

| 版本 | 内容 |
|------|------|
| v1.2.10 | 移动端 Web 适配 |
| v1.3.0 | monitor/storage 模块化重构 |
| v1.3.2 | Phase 1 鉴权 + API 框架 + iOS MVP |
| v1.3.3 | Phase 2 只读端点 + Phase 3 APNs 子系统 + Phase 4 iOS 适配 |
| v1.4.0 | Map / Calendar / SSE / Deep link / APNs 测试推送 |
| v1.4.1 | 错误展示打磨 / 多语言 en+zh-Hans / 深色模式 + Settings 切换 / iPad 6 tab 适配 + 键盘快捷键 |
