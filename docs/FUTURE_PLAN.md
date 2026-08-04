# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。

---

## v2.0 方向（2026-08-03 决定）

v2.0 只做两件事。

### 方向一：可观测性 —— **已完成第一批**

排查工作完全依赖登录服务器：本系统中的每一个缺陷均是通过人工 grep 日志定位的，
而 2026-06-13 起的那次 7 周静默停摆，正是同一短板的极端表现。

已落地：`round_stats` 轮次遥测表、`mcore/health.py` 分 source 健康判定、
`mcore/watchdog.py` 退化告警（含恢复通知，节流持久化）、`/logs` 服务端过滤、
`/monitoring` 面板。方案与验收标准见
[OBSERVABILITY_PLAN.md](OBSERVABILITY_PLAN.md)，判据设计理由见 ARCHITECTURE §5.12。

后续可继续推进的方向：将遥测数据接入 `/api/v1` 供移动端使用；增加抓取耗时趋势图；
提供按城市而不仅按 source 的细分维度。

### 方向二：RENTCafe 自动预订 —— **侦察和编码都做完了，卡在验证**

本条最初的判断是「下一步是侦察，而非编码」。侦察已于 2026-08-03/04 完成，其结论
推翻了当时的顾虑：`oleapplication.aspx` 九步流程的第 4 步之后既无人工审核，也无
阻塞流程的文件上传（证件需要上传，但**不阻塞**表单保存）。`bookers/rentcafe.py`
随之完成，reCAPTCHA 对接 2Captcha，OurDomain 与 Xior 共用一份实现。详见
[XIOR.md](XIOR.md) §8.6 / §8.7 与 [OURDOMAIN.md](OURDOMAIN.md) §7。

当前真正余下的三项工作均不属于编码：

1. **Xior 的最后一步尚未确认。** 系统代为上传证件后申请表能否正常保存，仅差一次
   真实尝试。需注意 Xior 的草稿**不锁定房源**——它比 Holland2Stay 提前一步终止，
   下一页即需填写 IBAN/SWIFT。因此即使走通，其价值也远低于 Holland2Stay 一线。
2. **OurDomain 欠缺一个真实账号。** 登录之后的环节全部未验证，且该流程不含选房
   页，一旦脱离流程便没有重选入口。验证需要一个 OurDomain 的 RENTCafe 账号
   （含完整申请人资料、背景调查同意及已上传的证件）。
3. **账号粒度尚不明确。** 两栋 OurDomain 楼分属两个 securerc 主机，cookie 不跨
   主机。参照 Xior 的经验，很可能需要一栋楼一套账号；目前使用的是面板上单一的
   `ourdomain_email` / `ourdomain_password`，待验证暴露问题后再照 `xior_accounts`
   拆分。

在完成上述三项之前，`monitor._AUTO_BOOK_SOURCES` 保持仅含 `holland2stay`。

**OurCampus 不在该线之内**：其预订流程从未侦察，且至今未出现过任何房源，不存在
可预订的标的。

---

## 历史路线图（2026-06-13）

### 已完成：H2S 传输层迁移至 CloakBrowser
- H2S 将 API 迁至 `www.holland2stay.com/api/graphql` + Cloudflare Turnstile，旧 curl_cffi 路径封锁
- **scraper**：`scrapers/holland2stay.py` 重写，`browser_fetcher.py` 共享模块，CloakBrowser 绕过 Turnstile + 浏览器内调用 GraphQL
- **booker**：同步迁移，所有 GraphQL mutation 走 BrowserFetcher
- **新 API**：扁平字段替代 `custom_attributesV2`；attribute ID→label 通过 aggregations 接口映射
- 为 Pararius / Funda 等 CF 保护的平台提供了通用基建

### 第一期：Android Play Store 上架 —— **已放弃（2026-08-03）**
- Android 客户端 A0–A5 已完成（57 个文件，约 9.5k 行代码，47 项单元测试），功能上
  无遗留
- FCM 推送已完成端到端联调并通过真机验收
- CI 自动构建签名 APK（`build.yml` 的 android job）；AAB 相关步骤已移除
- **不再上架**：分发方式确定为自 Release 页直接下载 APK。原计划中的 Google Play
  Billing 内购、Data Safety、封闭测试与商店截图一并取消
- 详见下方 [§1 Android 客户端](#1-android-客户端)

### 第二期：iOS 性能优化
- 对现有 iOS 客户端做性能专项优化
- 已完成：DateFormatter 静态化、featureMap 键预归一化、URLCache 条件 GET、通知首屏非阻塞、地图聚类后台化（v1.7.10）
- 继续：列表滚动帧率、图片加载、内存占用、启动时间
- SwiftUI 视图 diff 优化，减少不必要的 body 重算
- Instruments profiling（Time Profiler / Allocations / SwiftUI View Body）

### 第三期：Xior 自动预订研究 —— **研究部分已完成（2026-08-04）**
- 三个攻坚项均已落地：登录流程（两段式，四处陷阱逐一实测）、多步表单自动填写
  （字段由页面驱动，15 个字段名经实测校正）、reCAPTCHA（对接 2Captcha，
  `captcha/rentcafe_pages.py` 逐页记录所用版本为 v2 或 v3）
- 实现位于 `bookers/rentcafe.py`，OurDomain / OurCampus 共用同一份
- 分析原文见 [XIOR.md](XIOR.md) §8（此处早先所写的 §11 系笔误，XIOR.md 无该节）
- 余下的三项工作见上方[方向二](#方向二rentcafe-自动预订--侦察和编码都做完了卡在验证)

---

## 1. Android 客户端

> 状态更新（2026-05-30）：iOS 客户端进入维护阶段。Android 客户端 A0–A5 已完成，FCM 推送端到端拉通，CI 自动构建签名 APK。Play Store 上架已于 2026-08-03 放弃，改为 Release 页直接下载。详见 `docs/ANDROID_PLAN.md` 进度复盘。

### 目标

把 FlatRadar 的核心租客体验稳定带到 Android，覆盖另一半潜在用户群。国际学生 / 流动 young professional 群体里 Android 占比 ~40-50%，Android parity 已完成；Play Store 上架已于 2026-08-03 放弃，改为 Release 页直接下载签名 APK。

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

### 后端改动（全部已完成）
- FCM 推送通道：`notifier_channels/fcm.py` ✅（HTTP v1 API + OAuth2 service account），与 `apns.py` 对称
- 推送平台分流：`mcore/push.py` ✅ 所有 dispatch 函数双发 APNs + FCM
- 设备注册：`mstorage/_devices.py` ✅ `platform` 字段区分 `ios` / `android`
- `/api/v1/devices/register` ✅ 白名单 `ios` / `android`，按 platform 分流
- `/api/v1/devices/test` ✅ 按 platform 分流（iOS → APNs，Android → FCM data-only payload）
- 服务端已部署：`FCM_ENABLED=true` + service account JSON `/secrets/` ✅
- 条件缓存中间件：`app/routes/api_v1/__init__.py` ✅ ETag + Cache-Control + 304 对所有 GET 200 JSON 响应
- 每小时存活采样：`mstorage/_base.py` ✅ `record_uptime_sample()` / `uptime_percent_7d()` 替代旧的 `monitor_started_at`

### 阶段拆分

| 阶段 | 内容 | 状态 |
|---|---|---|
| **A0** | 项目骨架：Android Studio + Gradle (Kotlin DSL) + Compose + Hilt + 主题 / Navigation scaffold | ✅ 已完成 |
| **A1** | 鉴权 + Dashboard + Listings：Bearer Token 管理、三档登录、BiometricPrompt、实时统计、房源列表 + 筛选 + 详情 | ✅ 已完成 |
| **A2** | Map + Calendar：Google Maps Compose + clustering + 日历月格视图 | ✅ 已完成 |
| **A3** | SSE + 通知列表：OkHttp 实时推送、TODAY/YESTERDAY/EARLIER 分组、滑动已读、导航 unread 角标 | ✅ 已完成 |
| **A4** | FCM 集成：Firebase 初始化、token 注册/刷新、后端推送通道适配、深链跳转 | ✅ 已完成 |
| **A5** | Settings + 多语言 + 深色模式 + 错误处理：DataStore、System/Light/Dark、~170 中英字符串、CrashReporter | ✅ 已完成 |
| **A6** | 打磨 + Play Store 上架 | ❌ 已放弃（2026-08-03）。上架相关全部取消；只剩 Material 3 视觉打磨可独立进行 |

### 风险

- **Material vs HIG 设计差异**：Dashboard / List 卡片样式要重新对齐 Material 3 token（spacing、elevation、shape），不能照搬 iOS HIG 数值
- **FCM token 失效回收**：服务端做 `NotRegistered` 清理，与 APNs `unregistered` 处理路径共用逻辑
- **地图组件选型**：Google Maps Compose 需 API key + Play Services；若考虑无 GMS 设备（华为等），需 osmdroid 备选方案

---

## 2. 更多租房平台支持

> 状态更新（2026-05-25）：本章节是早期多平台规划记录。当前主线已经完成多源抓取架构，并接入 Holland2Stay、OurDomain 和 Xior；后续平台扩展仍可参考下方调研和架构原则。

### 目标

FlatRadar 已由 Holland2Stay 单源演进为多平台监控。面向荷兰国际学生与年轻职场人群的
主要租房平台仍有十余家，继续扩展平台覆盖可进一步接近**一站式房源雷达**的目标，
从而持续提升对用户的价值。

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

多源抓取架构已实现。`scrapers/` 包包含 `base.py`（`AbstractScraper`、`ScrapeTask`、
`ScrapeResult`）、`holland2stay.py`、`ourdomain.py`、`ourcampus.py` 与 `xior.py`。
核心设计如下：

- `Listing.source` 配合前缀化 ID（`h2s_` / `od_` / `oc_` / `xr_`），保证全局唯一
- 数据库已完成迁移：新增 `source` 列、前缀化 backfill 与索引
- `monitor.py` 按 source 隔离故障
- 通知模板已加入 source badge（iMessage / Email / Telegram / APNs / FCM）
- iOS 端 `SourceBadge` view 已上线，Web 端的 Source 列与筛选亦已上线

> 其中「每 source 独立 stale 阈值」一项已于 v1.13.0 撤销：四个平台的终态信号一致，
> 现统一为一套两段式收敛，见 [ARCHITECTURE.md §5.13](ARCHITECTURE.md#513-从-feed-里消失是唯一的下架信号)。

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
| **P1.5** | **RENTCafe 自动预订** —— 多步表单自动填写与 reCAPTCHA 求解（详见 [XIOR.md](XIOR.md) §8） | ⚠️ 代码已完成，待端到端验证 |
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

- **反爬升级**：Pararius 与 Funda 采用 Cloudflare 加行为检测，`curl_cffi` 的 chrome110 impersonate 可能不足以应对。备用方案为 headless Playwright（运行时成本提升 10–50 倍），仅在投入产出比高的平台上采用
- **登录态平台**（DUWO / SSH / Studentenwoningweb）：账号密码存 `.env`，cookie 定期刷新；账号被锁就 fall-back 到游客可见的子集 + 推送 admin 告警
- **每平台轮询节奏分开**：高频平台（H2S）保 5min；低频学生平台（DUWO / SSH）放宽到 30min。每个 source 自己的 `INTERVAL` env 变量，monitor 循环里独立调度
- **后端流量放大**：从 1 source 到 10 source，出口流量 × N。监控 Docker / VPS 带宽配额；nginx 加 limit_req 兜底
- **数据质量参差**：不同平台字段完整度差异大，UI 层做 graceful degradation——缺 energy label 就不显示那一行，而不是显示 "—"

#### 运维风险

- **各平台的 schema 变更概率较高**：第三方网站每次改版都可能导致 scraper 失效。建议：
  - 每个 scraper 在 CI 跑 daily smoke test（拉 1 个城市，断言至少 1 条结果）
  - smoke test 连续 3 天失败时自动告警（推送 admin APNs + 邮件）
  - `mstorage/_meta` 记录每个 source 最后成功时间 + 最近一次错误，Web admin 系统页可视化 "source health"
- **故障隔离**：单个 source 发生故障不得影响其余 source。`run_once()` 以 try/except 隔离每个 source 的 scrape 阶段（见前文 `monitor.py` 的改造示例）
- **回滚预案**：DB 迁移用 idempotent ALTER + meta flag；如果 source 列的引入暴露了未预期的查询性能问题，可临时把 `SOURCES=holland2stay` 退化到单 source 行为

---

## 3. iOS 客户端 — 剩余低优项

> 性能优化专项（v1.7.10）已完成：DateFormatter 静态化、featureMap 键预归一化、URLCache 条件 GET（配合后端 ETag/304 中间件）、通知首屏非阻塞、地图聚类后台化。详见 `docs/CHANGELOG.md`。

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

待补充的项目：

- `POST /api/v1/admin/users`：admin 端的用户 CRUD API（目前仅有 Web 后台，尚未
  暴露为 API）
- `POST /api/v1/admin/monitor/{start,stop,reload,restart}` ✅ 已全部暴露为 API（v1.7.x）

### 多平台之后的统计与图表扩展

- Dashboard 增加「按平台占比」饼图
- Stats 页增加「各平台房源更新速度」对比
- ~~每个 source 独立的 stale 阈值~~ —— **该方向已放弃**。四个平台的终态信号一致，
  分设阈值描述的是一个并不存在的差异，v1.13.0 已统一为一套两段式收敛，见
  [ARCHITECTURE.md §5.13](ARCHITECTURE.md#513-从-feed-里消失是唯一的下架信号)。

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
| 全平台性能优化 / 代码质量加固 | v1.7.8 |
| 用户优先级排序 + 安全加固 | v1.7.9 |
| Android MVP 完成（A0–A5）+ CI 自动构建 | v1.7.9 |
| Android FCM 端到端拉通 | v1.7.8 |
| iOS 性能专项（DateFormatter / featureMap / URLCache / SSE / 地图聚类） | v1.7.10 |
| Dashboard 运行时间修复（每小时存活采样） | v1.7.11 |
| Android 版本号动态化 + CI AAB 自动发布 | v1.7.11 |
| 后端条件缓存中间件（ETag / 304） | v1.7.10 |
| SQLite 连接池化 + 图表查询下推 | v1.7.10 |
