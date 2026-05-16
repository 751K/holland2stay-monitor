# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。

---

## 1. 移动端 Web 优化

✅ v1.2.10 已完成全面移动端适配（卡片视图、触摸目标、safe-area、iOS Safari 兼容）。
剩余低优先级项：PWA（manifest + service worker）、设置页/日志页移动端交互打磨。

## 2. monitor.py / storage.py 重构

✅ v1.3.0 已完成：`mcore/` 包（interval / prewarm / booking），`mstorage/` 包（6 个 Mixin）。
剩余低优先级项：`run_once()` 内部阶段抽取为独立函数、统一错误类型。

---

## 3. iOS 客户端（FlatRadar）

### 当前状态（v1.4.0）

✅ iOS 客户端 Phase 4 已完成。`ios/FlatRadar/` — SwiftUI + Swift 6.3，`@Observable @MainActor` + `actor APIClient`。

| 功能 | 状态 | 说明 |
|---|---|---|
| 三档登录（admin/user/guest） | ✅ | admin=WEB_PASSWORD，user=H2S 凭据回退验证 |
| Bearer Token 持久化 | ✅ | Keychain + UserDefaults 降级 |
| Dashboard 实时统计 | ✅ | 公开统计 + user 专属匹配卡片 |
| Settings（Server URL、角色、退出） | ✅ | 退出确认框已锚定按钮 |
| Listings 列表 + 搜索 + 翻页 | ✅ | searchable + 无限滚动 + 状态胶囊标签 |
| Listing 详情页 | ✅ | 全字段 + feature_map 网格 + H2S 链接 |
| Notifications 列表 + 翻页 | ✅ | 左滑标记已读 + 全部已读 + 类型图标 |
| Notifications 未读 badge | ✅ | Tab 角标实时更新 |
| Map 地图视图 | ✅ | 房源地图展示，支持深度链接和实时通知流 |
| Calendar 日历视图 | ✅ | 可入住房源按日分组展示，支持快速查询 |
| APNs 设备注册 + Send Test Push | ✅ | `POST /api/v1/devices/{register,test}`；Settings tab 一键自测 |
| Deep link | ✅ | `h2smonitor://listing/<id>` + APNs payload `listing_id`；`NavigationCoordinator` 统一路由 |
| SSE 实时推送 | ✅ | `/notifications/stream` 端点已就位，后端 Phase 2 实现 |
| 深色模式 + 手动切换 | ✅ | Settings 内 Light / Dark / System 三选一 Picker，跟随系统默认 |
| 多语言 en + zh-Hans | ✅ | String Catalog 153 条，覆盖所有视图/Store/APIError |
| 错误展示打磨 | ✅ | APIError 分类化 UI + 全局 401/403 自动登出 + Try Again + 刷新失败弹窗 |

### 待办

- **上架 App Store** — 用途声明、隐私说明、H2S 非官方关系声明
- **IPAD/Mac 适配** — ✅ 已完成：NavigationSplitView 双栏布局（sidebar + 内容区）、⌘1-4 键盘快捷键切 tab、Dashboard 响应式 4 列网格、`.horizontalSizeClass` 自动切换 iPhone TabView / iPad SplitView

---

## 4. iOS 后端规划（APNs + 数据端点）

### 已实现：Phase 1 — 鉴权 + API 框架

`app/routes/api_v1/auth.py` + `stats_public.py` + `app/api_auth.py` + `app/api_errors.py` + `mstorage/_tokens.py`。
Web 后台 `/settings/app-accounts` 可管理已签发的 token。

### ✅ 已完成：Phase 2 — 只读数据端点

| 端点 | 方法 | 文件 | 说明 |
|---|---|---|---|
| `/listings` | GET | `app/routes/api_v1/listings.py` | 分页列表，admin 全量 / user 按 `listing_filter` 过滤 |
| `/listings/<id>` | GET | 同上 | 单条详情，user 视角不可见的返回 404 |
| `/notifications` | GET | `app/routes/api_v1/notifications.py` | 分页通知，user 按 `user_id` + `listing_filter` 双层过滤 |
| `/notifications/read` | POST | 同上 | 标记已读，user 仅能标记自己的 |
| `/notifications/stream` | GET | 同上 | SSE 增量推送，支持 `?token=` query 参数鉴权 |
| `/map` | GET | `app/routes/api_v1/map.py` | 已缓存坐标的房源，user 视角过滤 |
| `/calendar` | GET | `app/routes/api_v1/calendar.py` | 有入住日期的房源，user 视角过滤 |
| `/me/summary` | GET | `app/routes/api_v1/me.py` | 当前用户概览（匹配数 / 24h 新增等） |
| `/me/filter` | GET | 同上 | 当前用户的 listing_filter（Phase 5 加 PUT） |

共享模块：
- `app/routes/api_v1/_helpers.py` — `row_to_listing` / `apply_user_filter` / `serialize_listing` / `storage_ctx`
- `mstorage/_notifications.py` — `NotificationOps`（`get_notifications` / `get_notifications_since` / `mark_notifications_read`，均支持 `user_id` 过滤）

### ✅ 已完成：Phase 3 — APNs 子系统

| 模块 | 文件 | 说明 |
|---|---|---|
| 推送调度 | `mcore/push.py` | `dispatch` / `dispatch_status_change` / `dispatch_aggregate` / `dispatch_error`；节流去重 + 聚合判定 |
| APNs 客户端 | `notifier_channels/apns.py` | HTTP/2 + JWT ES256 `.p8` 签名；`ApnsClient` / `ApnsConfig` / `ApnsResult`；403 自动重签 |
| 设备持久化 | `mstorage/_devices.py` | `DeviceOps`：`register_device` / `get_active_devices_for_user` / `disable_device` / `delete_device` |
| 设备端点 | `app/routes/api_v1/devices.py` | `POST /register` / `GET /` / `DELETE /<id>`，按 `app_token_id` 隔离 |

节流策略：同 (user, listing, kind) 5min 去重；每用户每分钟 ≤10 条；≥3 套聚合为 round 推送。
APNs 未启用（`APNS_ENABLED!=true`）时所有调用 no-op，不影响现有 4 渠道。

### ✅ 已完成：Phase 4 — iOS 客户端 Phase 2 适配

**客户端新增/修改（11 文件）：**

| 文件 | 变更 |
|---|---|
| `Models/Listing.swift` | 新增 `priceValue`/`featureMap`/`firstSeen`/`lastSeen` |
| `Models/APIResponse.swift` | 新增 `ListingsResponse`/`NotificationsResponse`/`MeSummary`/`MeFilterResponse` |
| `Models/NotificationItem.swift` | 新增 `markedRead()` |
| `Networking/APIClient.swift` | 新增 6 个 API 方法 + `buildURL` 修复 query string 编码 |
| `Stores/ListingsStore.swift` | 分页、搜索过滤、loadMore、refresh |
| `Stores/NotificationsStore.swift` | 分页、标记已读/全部已读（optimistic update） |
| `Stores/DashboardStore.swift` | 新增 `fetchMeSummary()` |
| `Views/Listings/ListingsView.swift` | searchable + 无限滚动 + 状态胶囊标签 + loading/empty/error |
| `Views/Listings/ListingDetailView.swift` | **新建**：全字段 + feature_map 网格 + H2S 链接 |
| `Views/Notifications/NotificationsView.swift` | 左滑已读 + 全部已读 + 类型图标 + 无限滚动 |
| `Views/Notifications/NotificationRow.swift` | SF Symbol 图标 + 颜色按 type 区分 |
| `Views/Dashboard/DashboardView.swift` | user 角色显示匹配/可订卡片 + 退出确认框 |
| `Views/Settings/SettingsView.swift` | 退出确认框锚定按钮（修复 popover 位置） |
| `Views/MainTabView.swift` | Notifications tab 未读 badge |

**服务端配套修改：**

| 文件 | 变更 |
|---|---|
| `app/routes/api_v1/auth.py` | user 登录回退到 H2S GraphQL `generateCustomerToken` 验证；`_dummy_bcrypt_verify` 处理 bcrypt 未安装 |
| `users.py` | `_bcrypt_hash` 处理 bcrypt 未安装（抛 RuntimeError 而非崩溃） |
| `app/forms/user_form.py` | 捕获 bcrypt 未安装异常 → ValueError |
| `app/routes/users.py` | `user_new`/`user_edit` 捕获 ValueError 并 flash 提示 |
| SQLite `user_configs` | name → H2S 邮箱，`app_login_enabled: true` |

**已修复的 bug：**
- `URL.appendingPathComponent` 把 `?` 编码成 `%3F` → 新增 `buildURL` 拆分 path + query
- refresh control 非空闲替换警告 → `isLoading` 条件加 `&& items.isEmpty`
- logout API double-wrapping decode 错误 → 返回类型改为 `RevokePayload`
- 不存在的用户名登录 500 → `_dummy_bcrypt_verify` 处理 ImportError
- Web 界面设置 App 密码崩溃 → `_bcrypt_hash` + 路由层捕获
- iPad/Mac 退出确认框位置错误 → confirmationDialog 锚定按钮

### 待实现：Phase 5 — 写操作（1-2 周，低优）

- `PUT /api/v1/me/filter`：user 自助改过滤条件
- `/api/v1/admin/*`：用户 CRUD、启停监控
- iOS Settings 按 role 显示不同功能

### 风险

| 风险 | 缓解 |
|---|---|
| Apple Developer 审批延迟 | APNs 已全面就绪并验证通过，密钥配置完成 |
| .p8 密钥泄漏 | Docker secret + `.gitignore` + 定期轮换 |
| device_token 轮换 | 每次 App 启动重新 register；UNIQUE 去重 |
| 推送刷屏 | 节流策略 + `apns-collapse-id` |
| 老库 schema 演进 | `ALTER TABLE ADD COLUMN` + try/except 幂等 |

---

## 当前优先级

| # | 项目 | 状态 |
|---|---|---|
| 1 | 移动端 Web 体验 | ✅ v1.2.10 |
| 2 | monitor / storage 重构 | ✅ v1.3.0 |
| 3 | Phase 1 — 鉴权 + API 框架 | ✅ v1.3.2 |
| 4 | iOS 客户端 v1 MVP | ✅ v1.3.2 |
| 5 | Phase 2 — 只读数据端点 | ✅ v1.3.3 |
| 6 | Phase 3 — APNs 子系统 | ✅ v1.3.3 |
| 7 | Phase 4 — iOS 客户端适配 | ✅ v1.3.3 |
| 8 | APNs 设备注册 + Deep link + SSE | ✅ v1.4.0 |
| 9 | Map / Calendar iOS UI | ✅ v1.4.0 |
| 10 | 错误展示打磨 | ✅ v1.4.1 |
| 11 | 多语言 en + zh-Hans | ✅ v1.4.1 |
| 12 | 深色模式 + Settings 切换 | ✅ v1.4.1 |
| 13 | Phase 5 — 写操作（自助改 filter 等） | 低优 |
| 14 | App Store 上架 | 待条件满足 |
