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

## 推荐优先级

1. ~~移动端 Web 体验问题~~ ✅ v1.2.10 已完成。
2. ~~`monitor.py` 低风险拆分~~ ✅ v1.2.10 已完成（`mcore/` 包）。
3. ~~`storage.py` 按 repository 拆分~~ ✅ v1.3.0 已完成（`mstorage/` 包，6 个 Mixin）。
4. 设置页/用户表单等低频页面的移动端交互优化（分组折叠、步骤化）。
5. 等 Web API 稳定后，启动 `/api/v1` 契约整理和只读 token，为 iOS 客户端做准备。
6. iOS 原生客户端开发（SwiftUI），优先只读信息展示。
