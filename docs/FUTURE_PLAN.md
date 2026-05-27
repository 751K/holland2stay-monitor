# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。

---

## 当前路线图（2026-05-27）

接下来三个阶段的重点工作：

### 第一期：Android 继续开发
- 在现有 Compose + Hilt 骨架基础上持续推进功能开发
- 目标：功能 parity 追平 iOS 客户端
- 详见下方 [§1 Android 客户端](#1-android-客户端)

### 第二期：iOS 性能优化
- 对现有 iOS 客户端做性能专项优化
- 重点方向：列表滚动帧率、图片加载、网络层缓存、内存占用、启动时间
- SwiftUI 视图 diff 优化，减少不必要的 body 重算
- Instruments profiling（Time Profiler / Allocations / SwiftUI View Body）

### 第三期：Xior 自动预订研究
- 在已有 Xior 抓取基础上，推进 RENTCafe 自动预订流程
- 基于 `docs/XIOR.md` §11 的可行性分析继续深挖
- 重点攻克：登录流程、多步表单自动填写、reCAPTCHA 解决方案
- 目标：实现 Xior 房源出现后自动提交预订申请

---

## 1. Android 客户端

> 状态更新（2026-05-25）：iOS 客户端已完成当前产品范围内的大功能开发，进入维护阶段。Android 客户端已经启动并持续推进，本章节保留为 Android parity 和上架收口的方向说明。

### 目标

把 FlatRadar 的核心租客体验稳定带到 Android，覆盖另一半潜在用户群。国际学生 / 流动 young professional 群体里 Android 占比 ~40-50%，Android parity 和 Play Store 上架是当前客户端侧主要推进方向。

### 技术栈

**Kotlin + Jetpack Compose 原生开发**，不引入 KMP / 跨平台框架。理由：

- iOS 端 SwiftUI 代码已稳定并进入维护阶段，没必要为了共享 60% 逻辑回去重构
- Compose 与 SwiftUI 声明式范式接近，视图层迁移心智成本低
- Material 3 组件体系成熟，设计系统可对等映射
- 原生推送（FCM）、地图（Google Maps / OSM）、图表库支持最完整
- 两套代码并行维护的代价远低于跨平台框架的集成 / 调试 / 平台适配成本

### 架构对齐

与 iOS 端保持分层对称，降低跨端理解成本：

| 层 | iOS (SwiftUI) | Android (Compose) | 说明 |
|---|---|---|---|
| View | SwiftUI Views | `@Composable` + Navigation | 声明式 UI，组件级对应 |
| State | `@Observable` / `@StateObject` | `ViewModel` + `StateFlow` | MVVM，响应式数据流 |
| Network | `URLSession` + async/await | OkHttp / Ktor + coroutines | REST + SSE（OkHttp EventSource） |
| Storage | `UserDefaults` / Keychain | `DataStore` / `EncryptedSharedPreferences` | Token / 偏好持久化 |
| DI | `@Environment` / 单例 | Hilt (Dagger) | 依赖注入 |

### 后端改动（已完成）
- FCM 推送通道：`notifier_channels/fcm.py` ✅，与 `apns.py` 对称（HTTP v1 API + OAuth2 service account）
- 设备注册：`mstorage/_devices.py` 已加 `platform` 字段区分 `ios` / `android` ✅
- `/api/v1/devices/register` 已扩展 `platform` 参数 ✅

### 阶段拆分

| 阶段 | 内容 | 预计 |
|---|---|---|
| **A0** | 项目骨架：Android Studio + Gradle (Kotlin DSL) + Compose + Hilt + 主题 / Navigation scaffold | ✅ 已完成 |
| **A1** | 鉴权 + Dashboard + Listings：Bearer Token 管理（EncryptedSharedPreferences）、三档登录、实时统计、房源列表 + 筛选 | 2 周 |
| **A2** | Map + Calendar：Google Maps Compose 或 osmdroid + 日历视图 + 房源标记联动 | 2 周 |
| **A3** | SSE + 通知列表：OkHttp EventSource 实时推送、通知 tab、已读 / 未读状态 | 1 周 |
| **A4** | FCM 集成：Firebase 初始化、token 注册 / 刷新、后端推送通道适配、静默推送 + 深链跳转 | 1.5 周 |
| **A5** | Settings + 多语言 + 深色模式 + 错误处理统一 | 1.5 周 |
| **A6** | 打磨 + Play Store 上架：Material 3 视觉对齐、Data Safety 表格、隐私声明、截图、内部测试 → 正式发布 | 2 周 |

### 风险

- **Material vs HIG 设计差异**：Dashboard / List 卡片样式要重新对齐 Material 3 token（spacing、elevation、shape），不能照搬 iOS HIG 数值
- **Google Play 审核**：比 App Store 宽松，但 Data Safety / Permissions 声明表格仍需准确填写，非官方关系声明与 iOS 保持一致
- **FCM token 失效回收**：服务端做 `NotRegistered` 清理，与 APNs `unregistered` 处理路径共用逻辑
- **地图组件选型**：Google Maps Compose 需 API key + Play Services；若考虑无 GMS 设备（华为等），需 osmdroid 备选方案

---

## 2. 更多租房平台支持

> 状态更新（2026-05-25）：本章节是早期多平台规划记录。当前主线已经完成多源抓取架构，并接入 Holland2Stay、OurDomain 和 Xior；后续平台扩展仍可参考下方调研和架构原则。

### 目标

FlatRadar 已从 Holland2Stay 单源演进为多平台监控。荷兰国际学生 / young professional 群体租房的主要平台仍有十几家，继续扩展更多平台后能进一步接近**一站式房源雷达**，对用户价值继续提升。

### 平台调研（按优先级）

| # | 平台 | 域名 | 定位 | 抓取难度 |
|---|---|---|---|---|
| 1 | **OurDomain** | `ourdomain.nl` | ✅ 已接入。Amsterdam Diemen Zuid / Rotterdam，RENTCafe 后端 | ✅ 已完成 |
| 2 | **DUWO** | `duwo.nl` / `room.nl` | 荷兰最大学生住房供应商（Amsterdam / Delft / Leiden / Den Haag / Wageningen / Hoofddorp），ROOM.nl 是 DUWO 联合多家组织的统一平台 | 中（账号绑定，部分房源需注册） |
| 3 | **SSH Student Housing** | `sshxl.nl` | 全国性大型学生住房（Utrecht / Amsterdam / Eindhoven / Maastricht / Groningen / Rotterdam / Zwolle / Tilburg / Den Haag） | 中（账号绑定，short-stay 渠道独立） |
| 4 | **Pararius** | `pararius.nl` | 综合租房 marketplace，国际学生使用率最高的非学生专属站，english-first | 高（大量房源 + 中介模式，可能要应对 anti-bot） |
| 5 | **Kamernet** | `kamernet.nl` | 单间合租 marketplace，学生 / 年轻人占比高，paid model（房客付费看联系方式） | 高（付费墙 + 中介关系，scrape 要谨慎合规） |
| 6 | **HousingAnywhere** | `housinganywhere.com` | 国际学生 marketplace，覆盖欧洲；荷兰段量大 | 中（有公开 API 但条款限制） |
| 7 | **De Key** | `dekey.nl` | Amsterdam 城市住房协会，年轻人 / 学生定向（Stadgenoot Light） | 中（部分房源走 WoningNet） |
| 8 | **Lieven de Key — Studentenwoningweb** | `studentenwoningweb.nl` | DUWO + Lieven de Key + Stadgenoot 等 Amsterdam 学生住房联合平台 | 中（账号 + 排队等待制） |
| 9 | **Funda Huur** | `funda.nl/huur/` | 综合租房（量大但中介房源占比高） | 高（强 anti-bot，可能要等他们开放 API） |
| 10 | **Camelot Europe** | `camelot-europe.com` | 长 / 短租 + 看护型住宅（anti-squat），Amsterdam / Rotterdam 有量 | 中 |

---

### 架构现状（已完成）

多源抓取架构已实现。`scrapers/` 包：`base.py`（`AbstractScraper` + `ScrapeTask` + `ScrapeResult`）、`holland2stay.py`、`ourdomain.py`、`xior.py`。核心设计：
- `Listing.source` + 前缀化 ID（`h2s_` / `od_` / `xr_`），全局唯一
- DB 已迁移：`source` 列 + 前缀化 backfill + 索引
- `monitor.py` 按 source 隔离故障，每 source 独立 stale 阈值
- 通知模板已加 source badge（iMessage / Email / Telegram / APNs / FCM）
- iOS 端 `SourceBadge` view 已上线，Web 端 Source 列 + 筛选已上线

#### Filter 跨 source 归一化参考

| 字段 | H2S | OurDomain | DUWO | 归一化策略 |
|---|---|---|---|---|
| 城市 | `city: Eindhoven` | `location.city: Eindhoven` | `properties.city: Eindhoven` | `lower().strip()` 后比对 |
| 状态 | `Available to book / Available in lottery / Rented` | `Available / Reserved` | `Available / Sold` | 抽 `StatusKind` enum：`book` / `lottery` / `reserved` / `other`；每个 scraper 自己映射 |
| 房型 | `Studio / 1-room / 2-room` | `Studio / Apartment / Loft` | `Single / Shared / Studio` | 抽 `TypeKind` enum + 保留 raw；UI 端宽松匹配 |
| 能效 | `A+ / A / B / ...` | （可能没这字段） | （多数 不暴露） | optional，UI 端 missing 时不显示 |
| 价格 | `basic_rent: 707.000` | `price: 1200` | `kale_huur: 450` | 统一 `priceValue: float`（已是 Listing 字段） |

### 阶段拆分（更新）

| 阶段 | 内容 | 预计 |
|---|---|---|
| **P0** | 架构重构（`scrapers/` 包 + `Listing.source` + DB 迁移 + monitor.py 改造） | ✅ 已完成 |
| **P1** | **OurDomain** + **Xior** —— 实现 scraper，验证多源 pipeline；UI 加 source badge | ✅ 已完成 |
| **P1.5** | **Xior 自动预订** —— RENTCafe 多步表单自动填写 + reCAPTCHA 解决，实现房源出现后自动提交预订申请（详见 `docs/XIOR.md` §11） | 🔜 当前 |
| **P2** | **DUWO / ROOM.nl** + **SSH Student Housing** —— 覆盖 Amsterdam / Delft / Leiden / Utrecht 高校城市；需处理登录态 cookie | 3 周 |
| **P3** | **HousingAnywhere**（公开 API 优先）+ **Studentenwoningweb** | 2 周 |
| **P4** | **Pararius** / **Kamernet** —— 难度高，量大；Pararius 可能需 Playwright | 3 周 |
| **P5** | 跨平台 stats / dashboard 扩展（饼图 / 平台对比 / 平台独立 stale 阈值 / Web admin 系统页 source 健康看板） | 1 周 |

---

### 风险与合规

#### 法律 / 合规

- **`robots.txt` + ToS 逐家审查**：每个平台抓取前明确读条款，记录在 `docs/scraping_compliance.md`。HousingAnywhere 等明确有公开 API 的优先用 API
- **个人信息合规（AVG / GDPR）**：只抓房源本身字段，**绝对不**抓上传者 / 中介个人电话邮箱姓名；如果某些平台房源描述里夹带这些，scraper 层做正则脱敏后入库
- **不绕过付费墙**：Kamernet 等付费看联系方式的平台，只抓 free tier 公开列表，不模拟登录拿付费数据
- **明确"非官方第三方"声明**：每个 source badge 旁加 tooltip "FlatRadar is not affiliated with {Platform}"；登录页 / 关于页同步说明
- **数据保留期**：保留下架房源用于历史统计 OK；但若某平台 ToS 要求删除则在 `mark_stale` 时整条 listing 删掉而非仅标记 Occupied

#### 技术风险

- **反爬升级**：Pararius / Funda 有 Cloudflare + behavioral 检测，`curl_cffi` 的 chrome110 impersonate 可能不够。备用方案：`playwright` headless（运行时成本 10–50× 提升）——只在 ROI 高的平台上
- **登录态平台**（DUWO / SSH / Studentenwoningweb）：账号密码存 `.env`，cookie 定期刷新；账号被锁就 fall-back 到游客可见的子集 + 推送 admin 告警
- **每平台轮询节奏分开**：高频平台（H2S）保 5min；低频学生平台（DUWO / SSH）放宽到 30min。每个 source 自己的 `INTERVAL` env 变量，monitor 循环里独立调度
- **后端流量放大**：从 1 source 到 10 source，出口流量 × N。监控 Docker / VPS 带宽配额；nginx 加 limit_req 兜底
- **数据质量参差**：不同平台字段完整度差异大，UI 层做 graceful degradation——缺 energy label 就不显示那一行，而不是显示 "—"

#### 运维风险

- **每平台 schema 变更可能性高**：第三方网站 redesign 一次，scraper 就崩。建议：
  - 每个 scraper 在 CI 跑 daily smoke test（拉 1 个城市，断言至少 1 条结果）
  - smoke test 连续 3 天失败时自动告警（推送 admin APNs + 邮件）
  - `mstorage/_meta` 记录每个 source 最后成功时间 + 最近一次错误，Web admin 系统页可视化 "source health"
- **故障隔离**：一个 source 挂了不能影响其他 source。`run_once()` 用 try/except 隔离每个 source 的 scrape 阶段（见前面 `monitor.py` 改造示例）
- **回滚预案**：DB 迁移用 idempotent ALTER + meta flag；如果 source 列的引入暴露了未预期的查询性能问题，可临时把 `SOURCES=holland2stay` 退化到单 source 行为

---

## 3. iOS 客户端 — 剩余低优项

### Larger Text / Dynamic Type 完整支持（accessibility nutrition label 第 7 项）

- 代码内 `.font(.system(size: N))` 固定字号全部替换为 `.body` / `.subheadline` / `.caption` 等语义字号
- mono caps 标签加 `.dynamicTypeSize(...DynamicTypeSize.accessibility1)` 上限避免撑爆卡片
- 跑 AX5 字号回归，调整 ListingRow / NotificationRow / DashboardView 在最大字号下的截断 / 换行行为
- ASC nutrition label 补勾 "Larger Text"

### Swift Charts 无障碍

- DashboardView 的 sparkline + KPI charts 加 `.chartDescriptor` / audio graph 支持
- VoiceOver 用户能听到趋势走向、最大值、最小值

### iPad 多窗口（Stage Manager）

- 支持 iPad 多窗口同时打开两个不同的 listing 详情
- `NSUserActivity` 状态恢复

---

## 4. 后端 — 低优 / 持续改进

### Phase 5（admin 写操作）剩余项

`PUT /me/filter` ✅ v1.5.0；`DELETE /me` ✅ v1.5.0；`POST /auth/register` ✅ v1.5.0；`POST /auth/password` ✅ v1.6.0；`POST /diagnostics/crash` ✅ v1.6.0。

待补：
- `POST /api/v1/admin/users` —— admin 端 user CRUD API（目前只有 Web 后台，没暴露 API）
- `POST /api/v1/admin/monitor/{start,stop,reload}` —— admin 远控监控进程的 API（iOS AdminMonitorView 当前调的是 Web 端点）

### 多平台后的统计 / 图表扩展

- Dashboard "按平台占比"饼图
- Stats 页"哪个平台房源更新最快"对比
- 每个 source 独立的 stale 阈值（H2S 7 天 / OurDomain 待调研 / DUWO 学生短期周期可能 3 天）

---

## 已完成里程碑

| 里程碑 | 版本 |
|---|---|
| 移动端 Web 体验适配 | v1.2.10 |
| monitor / storage 重构 | v1.3.0 |
| Phase 1 — 鉴权 + API 框架 | v1.3.2 |
| iOS 客户端 v1 MVP | v1.3.2 |
| Phase 2 — 只读数据端点 | v1.3.3 |
| Phase 3 — APNs 子系统 | v1.3.3 |
| Phase 4 — iOS 客户端 Phase 2 适配 | v1.3.3 |
| APNs 设备注册 + Deep link + SSE | v1.4.0 |
| Map / Calendar iOS UI | v1.4.0 |
| 错误展示打磨 / 多语言 / 深色模式 | v1.4.1 |
| iPad / Mac 适配（NavigationSplitView） | v1.4.x |
| 用户配置 SQLite 化 + 自助注册 + 改密 | v1.5.0 / v1.6.0 |
| Crash diagnostics 上报 + Web admin 查看 | v1.6.0 |
| StoreKit "Buy me a coffee" 内购 | v1.6.0 |
| **App Store 上架** | **v1.6.0** |
| ASC Accessibility Nutrition Label 覆盖 6 / 9 | v1.6.1 |
