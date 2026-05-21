# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。

---

## 1. Android 客户端

### 目标

把 iOS FlatRadar 的功能复刻到 Android，覆盖另一半潜在用户群。**国际学生 / 流动 young professional 群体里 Android 占比 ~40-50%**，目前只有 iOS 客户端等于把这部分用户挡在门外。

### 技术选型候选

| 路线 | 优点 | 缺点 |
|---|---|---|
| **Kotlin + Jetpack Compose** | 原生体验、Material 3 自带、Compose 与 SwiftUI 范式接近、迁移视图层成本低 | iOS / Android 两套代码并行维护 |
| **Kotlin Multiplatform (KMP) + Compose Multiplatform** | 共享 Stores / Networking / Models（占代码量 60%+），iOS 这边保留 SwiftUI 仅做胶水 | KMP 工具链仍在演进，CI / 调试复杂度增加 |
| **React Native** | 一次代码两端跑、生态成熟 | 与现有 Swift / Kotlin 原生栈不一致；APNs / FCM 配置仍要双端处理；性能在 Map / 图表场景一般 |
| **Flutter** | 一次代码两端跑、UI 一致性高 | 跟服务端约定的 SSE / APNs 集成都得重新趟一遍；Apple 生态融合度较低 |

**初步倾向**：先做 **Kotlin + Compose 原生**（路线 1），不引入 KMP——iOS 这边 SwiftUI 代码已经稳定，没必要为了 60% 共享回去重构；等 Android 也稳定后再评估是否抽 KMP 公共层。

### 推送通道

- **FCM (Firebase Cloud Messaging)** 替代 APNs
- 后端 `notifier_channels/` 加 `fcm.py`，跟 `apns.py` 对称（HTTP v1 API + OAuth2 service account）
- `mstorage/_devices.py` 已有 `env` 字段（sandbox/production）—— Android 加 `platform` 区分 ios/android
- `/api/v1/devices/register` 端点扩展 `platform` 字段

### 阶段拆分

| 阶段 | 内容 |
|---|---|
| **A0** | Android Studio 项目骨架 + Compose Hello World + 公共 Bearer Token 鉴权流（Keychain → EncryptedSharedPreferences） |
| **A1** | 三档登录 + Dashboard 实时统计 + Listings 列表（参考 iOS Phase 1-2） |
| **A2** | Map / Calendar / Notifications（含 SSE 客户端，Android 用 OkHttp EventSource） |
| **A3** | FCM 集成 + 后端推送通道适配 + Test Push |
| **A4** | Settings / 多语言 / 深色模式 / 错误处理统一 |
| **A5** | Play Store 上架（同样的非官方关系声明 / 隐私 / 截图） |

### 风险

- **Google Play 审核**比 App Store 更宽松但 Data Safety / Permissions 表格仍要认真填
- **后台 FCM 配额**：免费层 Cloud Messaging 不限量，但服务端要做 token 失效回收（同 APNs `unregistered` 处理）
- **Material vs HIG 设计差异**：Dashboard / List 卡片样式要重新对齐 Material 3 token，不能照搬 iOS 间距

---

## 2. 更多租房平台支持

### 目标

目前 FlatRadar 仅抓取 Holland2Stay 一家。荷兰国际学生 / young professional 群体租房的主要平台还有十几家，多平台聚合后能成为**一站式房源雷达**，对用户价值提升一个数量级。

### 平台调研（按优先级）

| # | 平台 | 域名 | 定位 | 抓取难度 |
|---|---|---|---|---|
| 1 | **OurDomain** | `ourdomain.nl` | 与 H2S 最相似——internationals + fully furnished + 大楼整栋经营，Amsterdam Diemen Zuid / Rotterdam / Delft 等 | 中（疑似 Magento / 类似 PWA 架构，可能 GraphQL） |
| 2 | **DUWO** | `duwo.nl` / `room.nl` | 荷兰最大学生住房供应商（Amsterdam / Delft / Leiden / Den Haag / Wageningen / Hoofddorp），ROOM.nl 是 DUWO 联合多家组织的统一平台 | 中（账号绑定，部分房源需注册） |
| 3 | **SSH Student Housing** | `sshxl.nl` | 全国性大型学生住房（Utrecht / Amsterdam / Eindhoven / Maastricht / Groningen / Rotterdam / Zwolle / Tilburg / Den Haag） | 中（账号绑定，short-stay 渠道独立） |
| 4 | **Pararius** | `pararius.nl` | 综合租房 marketplace，国际学生使用率最高的非学生专属站，english-first | 高（大量房源 + 中介模式，可能要应对 anti-bot） |
| 5 | **Kamernet** | `kamernet.nl` | 单间合租 marketplace，学生 / 年轻人占比高，paid model（房客付费看联系方式） | 高（付费墙 + 中介关系，scrape 要谨慎合规） |
| 6 | **HousingAnywhere** | `housinganywhere.com` | 国际学生 marketplace，覆盖欧洲；荷兰段量大 | 中（有公开 API 但条款限制） |
| 7 | **De Key** | `dekey.nl` | Amsterdam 城市住房协会，年轻人 / 学生定向（Stadgenoot Light） | 中（部分房源走 WoningNet） |
| 8 | **Lieven de Key — Studentenwoningweb** | `studentenwoningweb.nl` | DUWO + Lieven de Key + Stadgenoot 等 Amsterdam 学生住房联合平台 | 中（账号 + 排队等待制） |
| 9 | **Funda Huur** | `funda.nl/huur/` | 综合租房（量大但中介房源占比高） | 高（强 anti-bot，可能要等他们开放 API） |
| 10 | **Camelot Europe** | `camelot-europe.com` | 长 / 短租 + 看护型住宅（anti-squat），Amsterdam / Rotterdam 有量 | 中 |

### 架构改造（必要前置）

当前 `scraper.py` 是 H2S GraphQL 直拉，强耦合 Holland2Stay 的 schema。多平台支持前需要先抽象：

```
scrapers/
  base.py               # AbstractScraper：scrape(city) -> list[Listing]
  holland2stay.py       # 现有 H2S 实现迁过来，继承 AbstractScraper
  ourdomain.py          # Phase 1 新增
  duwo.py               # Phase 2
  sshxl.py              # Phase 2
  ...
```

**关键统一字段**：每个 scraper 输出统一的 `Listing` dataclass（已经存在），但需要新增 `source: str` 字段（`"holland2stay"` / `"ourdomain"` / ...），数据库表 + 通知模板都要带上"来源"。

### 数据库迁移

- `listings` 表加 `source TEXT NOT NULL DEFAULT 'holland2stay'`
- `id` 字段改成 `(source, native_id)` 复合主键（或前缀化 `"h2s_38492"` / `"od_12345"`），避免不同平台 ID 冲突
- `status_changes` 跟随 listing id 变更迁移
- 老数据一次性 backfill `source='holland2stay'`

### 阶段拆分

| 阶段 | 内容 |
|---|---|
| **P0** | `scrapers/` 包抽象 + 现有 H2S 迁过来，跑通无新平台时行为不变（zero-regression） |
| **P1** | **OurDomain** —— 与 H2S 架构最像，作为首个第三方源验证多平台 pipeline |
| **P2** | **DUWO / ROOM.nl** + **SSH** —— 两个大学生住房供应商，覆盖 Amsterdam / Delft / Leiden / Utrecht 等核心高校城市 |
| **P3** | **HousingAnywhere**（如果他们的公开 API 条款允许）+ **Studentenwoningweb**（Amsterdam 学生联合平台） |
| **P4** | **Pararius** / **Kamernet**（marketplace 类）—— 难度高，量大，最后做 |

### 用户侧改动

- **Listing card / 详情页** 显示 `source` badge（"H2S" / "OurDomain" / "DUWO" 彩色小标签）
- **Filter 增加 source 维度**，user 可勾选只看某几个平台的房源
- **Notification template** 标明来源："[OurDomain] New listing in Amsterdam ..."

### 风险与合规

- **`robots.txt` / ToS**：每个平台抓取前先确认条款；HousingAnywhere 等明确有 API 的优先用 API
- **反爬**：Pararius / Funda 有 Cloudflare + behavioral 检测，`curl_cffi` 不一定够，可能需要 Playwright 这种 headless browser，runtime cost 增加
- **个人信息合规**：避免抓取上传者 / 中介个人电话邮箱字段，只抓房源本身
- **频率与礼貌**：每个平台 polling 间隔分开配置，默认 ≥5min，避免被 ban

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
