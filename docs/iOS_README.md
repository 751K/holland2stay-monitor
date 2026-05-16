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

- **语言**：Swift 6
- **UI 框架**：SwiftUI（`@Observable @MainActor` 宏模式）
- **网络层**：`APIClient` + `URLSession`（async/await）+ `SSEClient`（实时推送）+ StoreKit 2
- **持久化**：Keychain（Bearer token）+ UserDefaults + `@AppStorage`
- **设计系统**：主色 #0A84FF，语义色（#34C759 success / #FF9500 warning / #FF3B30 error），tabular-nums

### 功能矩阵

| 功能 | 状态 | 说明 |
|------|------|------|
| 三档登录（admin/user/guest） | ✅ | admin=WEB_PASSWORD，user=bcrypt 或 H2S 凭据回退验证 |
| 用户自助注册 | ✅ | 注册即登录，自动签发 token；bcrypt 密码哈希 |
| 账号注销 | ✅ | 二次确认弹窗，删除 users.json 配置 + 撤销 token + 清除 SQLite |
| Bearer Token 持久化 | ✅ | Keychain + UserDefaults 降级 |
| Dashboard 实时统计 | ✅ | 问候语 + 用户胶囊 + Live badge + 统计卡片含 sparkline + Explore 2×2 内联迷你图表 |
| Dashboard 匹配预览 | ✅ | 3 张 mini 卡片展示匹配房源价格/城市，点击跳转详情 |
| Listings 列表 + 搜索 + 翻页 | ✅ | searchable + 无限滚动 + 多维度筛选（城市/状态/户型/合同/能源） |
| Listing 详情页 | ✅ | 全字段 + feature_map 网格 + 监控历史 + H2S 链接 + 官方核实提示 |
| Map 地图视图 | ✅ | MapKit + 自定义网格聚类 + 状态色 pin + cluster-tap zoom + 选中卡片 deep link |
| Calendar 日历视图 | ✅ | 月格 + 每日可入住数 + 选中日房源列表 + 灰底背景 |
| Notifications 通知列表 | ✅ | 卡片式 TODAY/YESTERDAY/EARLIER 三区 + 左滑已读 + Live indicator |
| SSE 实时推送 | ✅ | `/notifications/stream` + 指数退避重连 |
| APNs 设备注册 + Test Push | ✅ | Settings 一键自测，验证端到端链路 |
| Deep link | ✅ | `h2smonitor://listing/<id>` + APNs `listing_id` |
| 通知过滤器自助编辑 | ✅ | FilterEditView — 10 维度多选（租金/面积/楼层/城市/户型/合同/租客/能源/装修/优惠） |
| 深色模式 | ✅ | 全页面自适应（Login hero / Dashboard 卡片 / Settings），语义色跟随 |
| 多语言 en + zh-Hans | ✅ | 174 条本地化字符串，所有 UI 文本覆盖 |
| 错误展示打磨 | ✅ | APIError 分类 UI + 全局 401/403 自动登出 + errorDescription/failureReason 分离显示 |
| 首次启动 Terms 弹窗 | ✅ | 强制阅读 5 条关键条款 + "Agree & Continue"，interactiveDismissDisabled |
| Settings 法律文档 | ✅ | Terms of Use + Privacy Policy 完整内嵌 |
| iPad 适配 | ✅ | 底栏 6 tab（List/Map/Calendar 直接展开）+ ⌘1-⌘6 |
| 键盘快捷键 | ✅ | ⌘1-6 切换 tab（iPad 外接键盘） |
| Buy me a coffee | ✅ | StoreKit 2 IAP（Espresso/Latte/Flat White），3 档 consumable，捐赠不绑定功能 |
| App Store 准备 | ✅ | PrivacyInfo.xcprivacy + App 图标 + 签名配置 |

### 文件结构

```
ios/FlatRadar/FlatRadar/
├── FlatRadarApp.swift              # @main 入口，环境注入，深色模式/SSE 管理，StoreKit 监听
├── PrivacyInfo.xcprivacy           # App Store 隐私清单（Required Reasons API + 数据收集声明）
├── Localizable.xcstrings           # 174 条中英文本地化
├── Models/
│   ├── APIResponse.swift           # 通用信封 + 分页/Device/Me/Map/Calendar + ServerTime 时间工具
│   ├── Listing.swift               # 房源模型（priceValue/featureMap/areaText/energyText）
│   ├── ListingFilter.swift         # 用户过滤条件模型（10 维度 + summary）
│   ├── MapListing.swift            # 地图房源（坐标 + 状态）
│   ├── CalendarListing.swift       # 日历房源（入住日期 + 分组）
│   ├── NotificationItem.swift      # 通知模型（markedRead + dayBucket）
│   ├── AuthModels.swift            # 登录/注册请求响应体
│   ├── UserInfo.swift              # 用户信息（来自 /auth/me）
│   ├── MonitorStatus.swift         # 公开统计（Dashboard）
│   ├── ChartData.swift             # 图表数据（动态 key 解码）
│   ├── AdminModels.swift           # 管理面板模型
│   ├── LegalText.swift             # Terms of Use / Privacy Policy 全文
│   └── APIResponse+Helpers.swift   # FilterOptions / 设备响应 / 账户删除响应
├── Networking/
│   ├── APIClient.swift             # HTTP 客户端（22 个 API 方法，@MainActor，auth 失败通知，30s 超时）
│   ├── APIError.swift              # 统一错误类型（LocalizedError + SF Symbol + recovery suggestion）
│   ├── SSEClient.swift             # SSE 流客户端（AsyncThrowingStream，text/event-stream 解析）
│   └── KeychainManager.swift       # Keychain 读写封装（kSecClassGenericPassword）
├── Push/
│   └── PushDelegate.swift          # UIApplicationDelegate 桥接（APNs token + 通知点击 + pending flush）
├── Navigation/
│   └── NavigationCoordinator.swift # @Observable 导航状态（tab/browse mode/listing path/deep link 协调）
├── Stores/
│   ├── AuthStore.swift             # 登录/注册/注销/删号/restore/token 持久化/全局 auth 失败监听
│   ├── DashboardStore.swift        # Dashboard 公开统计 + Me 摘要 + 图表 keys
│   ├── ListingsStore.swift         # 房源分页/搜索/多维过滤（city/status/type/contract/energy）
│   ├── MapStore.swift              # 地图房源缓存
│   ├── CalendarStore.swift         # 日历房源 + 按日分组 dict
│   ├── NotificationsStore.swift    # 通知分页/标记已读/SSE 连接管理/badge 同步
│   ├── PushStore.swift             # APNs 设备注册/解绑/测试推送 + sandbox/production 自动切换
│   ├── MeFilterStore.swift         # 当前用户 filter 的保存与同步
│   ├── AdminStore.swift            # 管理面板（用户列表/启停/监控进程控制）
│   └── CoffeeStore.swift           # StoreKit 2 捐赠管理（加载产品/购买/交易监听）
└── Views/
    ├── ContentView.swift           # 根视图（登录 vs 主页切换 + 首次 Terms 弹窗）
    ├── MainTabView.swift           # 响应式 TabView（iPhone 4 tab / iPad 6 tab + 键盘快捷键）
    ├── Auth/
    │   ├── LoginView.swift         # Hero 山脉动画 + 展开式角色卡片 + 注册 sheet + Terms/Privacy links
    │   └── LoginModePicker.swift   # Tenant/Guest/Staff 三选一选择器（含图标 + MatchedGeometryEffect）
    ├── Browse/
    │   └── BrowseView.swift        # iPhone Browse tab：NavigationStack + segmented picker
    ├── Dashboard/
    │   ├── DashboardView.swift     # 问候语 + 用户胶囊 + Live badge + 统合统计卡 + Explore 内联图表 + 匹配预览
    │   └── ChartDetailView.swift   # 通用图表详情（Swift Charts 时序图 + BreakdownRow 明细表格）
    ├── Listings/
    │   ├── ListingsView.swift      # 房源列表（搜索/多维筛选 sheet/排序菜单）+ 错误状态
    │   ├── ListingRow.swift        # 紧凑型行（NEW 徽章 + monospacedDigit 价格 + 状态胶囊）
    │   └── ListingDetailView.swift # 房源详情（全字段 + feature 网格 + 监控历史 + 官方核实提醒）
    ├── Map/
    │   ├── MapView.swift           # MapKit 地图（状态色 pin + 集群气泡 + 选中卡片 + 计数 badge）
    │   └── MapClustering.swift     # 自定义网格聚类算法
    ├── Calendar/
    │   └── CalendarView.swift      # 月历（月格+入住计数+选中日房源列表）+ 灰底背景
    ├── Notifications/
    │   ├── NotificationsView.swift # 卡片式通知列表（TODAY/YESTERDAY/EARLIER + 未读 stripe + Live indicator）
    │   └── NotificationRow.swift   # 单行通知（类型图标 + 颜色 + 时间戳）
    ├── Settings/
    │   ├── SettingsView.swift      # Push Filter / Appearance / Push Notifications / Account / Admin / Legal / Coffee / About
    │   └── FilterEditView.swift   # 10 维度过滤条件编辑表单
    └── Admin/
        ├── AdminUsersView.swift    # 用户列表（启停/删除 + 状态 chips）
        └── AdminMonitorView.swift  # 监控进程控制（状态/启停/重载）
```

### 登录流程

```
用户选择角色 → Tenant/Staff 展开卡片输入凭据
    │
    ├─ Staff (Admin) → __admin__ + WEB_PASSWORD → hmac.compare_digest → role=admin
    │
    ├─ Tenant (User) → 用户名 + 密码
    │       ├─ 优先 bcrypt app_password_hash 匹配 → role=user
    │       └─ 未匹配 → H2S GraphQL generateCustomerToken 验证 → role=user
    │
    ├─ Guest → 本地标记，无需请求后端
    │
    └─ 注册 → Tenant 卡片底部 "Register" → 注册 sheet → POST /auth/register
            → 创建 UserConfig + bcrypt 设密 + 自动签发 token
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
| guest | 无需登录 | Dashboard（公开统计）、Browse（全量 Listings/Map/Calendar） |
| user | 用户名+密码 或 H2S 凭据 | Dashboard + 匹配预览 + Listings（filtered）+ Notifications（per-user）+ Map/Calendar（filtered）+ Settings（filter 编辑 + 删号） |
| admin | `__admin__` + WEB_PASSWORD | Dashboard + 全量 Listings + 全量 Notifications + Settings（管理设备 + Test push）+ Admin 面板 |

### 导航架构

```
iPhone (compact)                    iPad (regular)
──────────────                      ──────────────
TabView 4 tabs:                     TabView 6 tabs:
  🏠 Dashboard                        🏠 Dashboard
  🔍 Browse [L|M|C]                  📋 Listings
      ├─ ListingsView                🗺 Map
      ├─ MapView                     📅 Calendar
      └─ CalendarView                🔔 Alerts
  🔔 Alerts                          📐 Settings
  📐 Settings
```

iPhone 上 List/Map/Calendar 合并进 Browse tab + segmented picker；iPad 空间足够，6 个 tab 直接展开到底栏。

---

## API 后端

### 端点总览

所有端点在 `/api/v1/*` 下，返回统一壳形 `{"ok": bool, "data": ...}` / `{"ok": false, "error": {"code": "...", "message": "..."}}`。

#### 鉴权

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/auth/login` | 无 | 用户名+密码 → Bearer token（TTL 默认 90 天，上限 90 天） |
| POST | `/auth/register` | 无 | 自助注册（≥2 字符用户名，≥4 字符密码，bcrypt 哈希），注册即登录，同 IP 限 3 次/小时 |
| POST | `/auth/logout` | Bearer | 撤销当前 token |
| GET  | `/auth/me` | Bearer | 当前身份（role/user/filter） |
| GET  | `/stats/public/summary` | 无 | 公开统计（total/new_24h/new_7d/changes_24h/last_scrape） |
| GET  | `/stats/public/charts` | 无 | 图表 key 列表 |
| GET  | `/stats/public/charts/<key>` | 无 | 图表时序数据（?days=30） |

#### 只读数据

| 方法 | 路径 | 鉴权 | 参数 | 说明 |
|------|------|------|------|------|
| GET | `/listings` | 可选 | `?city=&cities=&status=&q=&types=&contract=&energy=&limit=&offset=` | 分页列表，user 按 `listing_filter` 过滤 |
| GET | `/listings/<id>` | 可选 | — | 单条详情，user 不可见时 404 |
| GET | `/notifications` | admin/user | `?limit=&offset=` | 分页通知，user 双层过滤（user_id + listing_filter） |
| POST | `/notifications/read` | admin/user | `{"ids": [...]}` 或 `{}` | 标记已读（全部或指定 ids） |
| GET | `/notifications/stream` | Bearer/query | `?token=&last_id=` | SSE 增量推送 |
| GET | `/map` | admin/user | — | 已缓存坐标的房源 |
| GET | `/calendar` | admin/user | — | 有入住日期的房源 |
| GET | `/me/summary` | admin/user | — | 当前用户统计（匹配数/可订数等） |
| GET | `/me/filter` | admin/user | — | 当前用户的 listing_filter |
| PUT | `/me/filter` | user | JSON body | user 自助修改过滤条件，白名单校验 |
| DELETE | `/me` | user | — | 注销账号：删除 users.json 配置 + 撤销 token + 清除 SQLite |
| GET | `/filter/options` | 可选 | — | 过滤维度候选值（cities/types/contract/energy...） |

#### APNs / 设备

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/devices/register` | admin/user | 注册/刷新 APNs device token |
| GET | `/devices` | admin/user | 列出当前会话的设备（token 脱敏） |
| DELETE | `/devices/<id>` | admin/user | 删除指定设备 |
| POST | `/devices/test` | admin/user | 发送测试推送（验证 APNs 端到端链路） |

#### Admin

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/admin/users` | admin | 用户列表 + App 会话统计 |
| POST | `/admin/users/<id>/toggle` | admin | 启用/禁用用户 |
| DELETE | `/admin/users/<id>` | admin | 删除用户 + 撤销 token |
| GET | `/admin/monitor/status` | admin | 监控进程状态（PID/last scrape/count） |
| POST | `/admin/monitor/start\|stop\|reload` | admin | 监控进程控制 |

### 服务端文件结构

```
app/
├── api_auth.py              # Bearer Token 校验（装饰器 + TTL 缓存 + 异步刷盘）
├── api_errors.py            # 统一错误响应工厂（unauthorized/forbidden/validation/conflict/rate_limited/server_error）
├── auth.py                  # Web 后台鉴权（session/cookie）+ 爆破防护 + 注册限流
├── csrf.py                  # CSRF 保护
├── db.py                    # Storage 工厂
└── routes/
    ├── api_v1/
    │   ├── __init__.py      # Blueprint 注册
    │   ├── _helpers.py      # row→Listing / apply_user_filter / serialize
    │   ├── auth.py          # 登录/注册/登出/me（含 bcrypt + H2S 凭据验证 + 时序对齐）
    │   ├── stats_public.py  # 公开统计 + 图表
    │   ├── listings.py      # 房源列表/详情（含新多维过滤参数）
    │   ├── notifications.py # 通知列表/已读/SSE
    │   ├── map.py           # 地图数据
    │   ├── calendar.py      # 日历数据
    │   ├── me.py            # 当前用户摘要/filter CRUD/账号注销
    │   ├── devices.py       # APNs 设备管理 + 测试推送
    │   └── admin.py         # 管理面板（用户 CRUD + 监控进程控制）
    ├── __init__.py
    └── ...

mcore/
├── push.py                  # APNs 推送调度（dispatch/aggregate/节流去重）

mstorage/
├── _tokens.py               # app_tokens 表 + CRUD + revoke_user_tokens
├── _app_users.py            # app_users 表（SQLite 账号镜像，用于双写同步）
├── _devices.py              # device_tokens 表 + CRUD + disable
├── _notifications.py        # web_notifications 表（含 user_id 过滤）
└── ...                      # listings / charts / map_calendar / retry

notifier_channels/
├── apns.py                  # APNs HTTP/2 客户端（JWT ES256 签名 + httpx）

users.py                     # UserConfig dataclass + load/save/update_users + bcrypt + 加密
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

### 安全要点

- **密码存储**：bcrypt 单向哈希（cost=12），不存明文
- **时序对齐**：用户不存在分支跑 dummy bcrypt，防止用户名枚举
- **原子写入**：`users.json` 先写 `.tmp` 再 `os.replace`，持有文件锁
- **双写清理**：注册/删号同时维护 `users.json` + SQLite `app_users`，失败时 rollback
- **输入校验**：用户名 ≤64 字符，密码 ≥4 字符，TTL 上限 90 天
- **注册限流**：同 IP 每小时 3 个 + 复用登录爆破防护
- **admin 端点**：全部 `bearer_required(("admin",))` 守卫

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
4. 默认连接 `flatradar.app`，本地调试时在 Settings → Server URL 填入 `127.0.0.1:8088`（模拟器）或 VPS IP

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
| v1.5.0 | 登录页 Hero 山脉设计 / 展开式角色卡片 / 用户自助注册 / 账号注销 / Dashboard V1 重设计（问候语 + sparkline + Explore 内联图表 + 匹配预览）/ Listings 多维过滤 / 通知卡片式 inbox / Filter 自助编辑 / Settings 法律文档 / 首次 Terms 弹窗 / Buy me a coffee StoreKit 2 / PrivacyInfo.xcprivacy / 设计系统主色 #0A84FF + tabular-nums / 174 条中英文本地化 / 后端 app_users 双写 + register 限流 + 注册冲突检测 + bcrypt 登录 |
