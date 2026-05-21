# Future Plan / 未来规划

本文档记录后续版本可以继续推进的方向。

---

## 1. Android 客户端

### 目标

把 iOS FlatRadar 的功能复刻到 Android，覆盖另一半潜在用户群。国际学生 / 流动 young professional 群体里 Android 占比 ~40-50%，目前只有 iOS 客户端等于把这部分用户挡在门外。

### 技术栈

**Kotlin + Jetpack Compose 原生开发**，不引入 KMP / 跨平台框架。理由：

- iOS 端 SwiftUI 代码已稳定，没必要为了共享 60% 逻辑回去重构
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

### 后端改动（共用，非 Android 专属）

- FCM 推送通道：`notifier_channels/fcm.py`，与 `apns.py` 对称（HTTP v1 API + OAuth2 service account）
- 设备注册：`mstorage/_devices.py` 加 `platform` 字段区分 `ios` / `android`
- `/api/v1/devices/register` 扩展 `platform` 参数

### 阶段拆分

| 阶段 | 内容 | 预计 |
|---|---|---|
| **A0** | 项目骨架：Android Studio + Gradle (Kotlin DSL) + Compose + Hilt + 主题 / Navigation scaffold | 1 周 |
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

---

### 架构改造详解

#### 现状梳理

当前抓取链路的痛点：

- **`scraper.py`** 直接 import `holland2stay` 的 GraphQL `category_uid` / `available_to_book` 这些 H2S 专有字段；`models.Listing` 的 `feature_map` / `available_to_book_id` 也是为 H2S 量身定做
- **`config.py`** 的 `scrape_tasks()` 返回 `(city_name, city_id_str)`——`city_id` 是 H2S 的内部数字 ID，其他平台没有这个概念
- **`storage.py`** 的 `listings.id` 直接用 H2S 的 `sku` 当主键，OurDomain / DUWO 各家 ID 空间会冲突
- **`monitor.py`** 的 `run_once()` 假定只有一个 source，prewarm / booking 逻辑写死 H2S
- **`notifier.py`** 的消息模板写死 "Holland2Stay" 品牌词

要支持多平台，**必须先重构成 source-aware**——但要做到 **zero-regression**：只有 H2S 一家时行为完全不变。

#### 抽象层：`scrapers/` 包

```python
# scrapers/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from models import Listing

@dataclass(frozen=True)
class ScrapeTask:
    """一次抓取的最小单位。source-agnostic，由 Config.scrape_tasks() 产出。"""
    source: str           # "holland2stay" / "ourdomain" / "duwo" ...
    city_key: str         # 平台内部城市标识（H2S 是 city_id_str，OurDomain 可能是 slug）
    city_display: str     # 用户可见的城市名（"Amsterdam" / "Eindhoven"）

@dataclass
class ScrapeResult:
    """一个 ScrapeTask 的产出。complete=False 时不参与 stale 收敛。"""
    task: ScrapeTask
    listings: list[Listing]
    complete: bool        # 全部页都抓完 + 总数 sanity 检查通过 = True
    error: str | None = None

class AbstractScraper(ABC):
    """每个平台一个子类。监控进程通过 SCRAPER_REGISTRY[source] 取实例。"""
    source: str           # 类属性，子类必须覆盖

    @abstractmethod
    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        """同步函数，保留现有 sync 范式避免重构 monitor.py 的 run_in_executor。"""

    # 可选钩子，多数平台不需要：
    def prewarm_session(self) -> None: ...
    def try_book(self, listing: Listing) -> bool: ...
```

注册表：

```python
# scrapers/__init__.py
SCRAPER_REGISTRY: dict[str, type[AbstractScraper]] = {
    cls.source: cls for cls in [HollandStayScraper, OurDomainScraper, ...]
}
```

#### `models.Listing` 改造

现有 dataclass 加 `source` 字段 + 引入**前缀化 ID 策略**：

```python
@dataclass
class Listing:
    source: str          # 新增："holland2stay" / "ourdomain" / ...
    native_id: str       # 平台内部 ID（H2S 用 sku，OurDomain 用 slug 或 unitId）
    # 现有字段保持不变（name / city / price / status / feature_map ...）

    @property
    def id(self) -> str:
        """全局唯一 ID。格式 `{prefix}_{native_id}`，例如 `h2s_38492` / `od_a201-403`。
        UI / API / 通知 deep link 都用这个 id。"""
        return f"{_SOURCE_PREFIX[self.source]}_{self.native_id}"

_SOURCE_PREFIX = {
    "holland2stay": "h2s",
    "ourdomain":    "od",
    "duwo":         "duwo",
    "sshxl":        "ssh",
}
```

**关键决策**：保留 `id` 为字符串（H2S 现在也是字符串，没破坏性变更）。前缀法比复合主键查询友好，仍可单字段 lookup。

#### 数据库迁移

幂等 schema 演进（`mstorage/_schema.py`）：

```sql
-- listings 表加 source 列
ALTER TABLE listings ADD COLUMN source TEXT NOT NULL DEFAULT 'holland2stay';

-- 全量 backfill：原 sku 改为前缀化 id
UPDATE listings
   SET id = 'h2s_' || id
 WHERE source = 'holland2stay' AND id NOT LIKE 'h2s_%';

-- status_changes 同步重写 listing_id
UPDATE status_changes
   SET listing_id = 'h2s_' || listing_id
 WHERE listing_id NOT LIKE '%\_%' ESCAPE '\';

-- 新索引：跨 source 时按 source 过滤的查询会很多
CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_source_city ON listings(source, city);
```

`mstorage/_migrations.py` 加 `meta` flag `source_column_added_v1` 保证只跑一次；失败时整个事务回滚不破坏老数据。

#### `monitor.py` 改造

`run_once()` 不再直接 import `scraper.scrape_all`，改成查注册表 + 按 source 隔离故障：

```python
def run_once(cfg: Config, storage: Storage) -> None:
    tasks = cfg.scrape_tasks()                  # list[ScrapeTask]，跨 source
    grouped = group_by_source(tasks)            # {source: [task, ...]}

    for source, source_tasks in grouped.items():
        scraper_cls = SCRAPER_REGISTRY.get(source)
        if not scraper_cls:
            log.warning(f"unknown source: {source}, skip")
            continue
        scraper = scraper_cls()
        try:
            for task in source_tasks:
                result = scraper.scrape(task)
                storage.upsert_listings(
                    result.listings,
                    source=source,
                    city=task.city_display,
                    complete=result.complete,
                )
        except Exception as e:
            # 故障隔离：一个 source 挂了不能影响其他 source
            log.error(f"source={source} failed: {e}", exc_info=True)
            storage.record_source_failure(source, str(e))

    # stale 收敛按 source 分别做（每 source 独立窗口）
    storage.mark_stale_listings(per_source_thresholds={
        "holland2stay": {"book": 7, "lottery": 2},
        "ourdomain":    {"book": 7, "lottery": None},   # OurDomain 没 lottery
        "duwo":         {"book": 3, "lottery": None},   # 学生短期周期
    })
```

#### `config.py` 改造

`.env` 扩展按 source 配置 + 加 feature flag 控制开关：

```bash
# 现有
H2S_CITIES=Eindhoven=29,Amsterdam=11
H2S_AVAILABILITY=179,336

# 新增 — feature flag：开启哪些 source（未列出的即使配置了也跳过）
SOURCES=holland2stay,ourdomain

# 各 source 自己的配置块
OURDOMAIN_CITIES=Amsterdam=diemen-zuid,Rotterdam=blaak
OURDOMAIN_INTERVAL=300                          # 单位秒，默认与全局 INTERVAL 一致
DUWO_CITIES=Amsterdam,Delft,Leiden
DUWO_USERNAME=...                               # 部分平台需登录
DUWO_PASSWORD=...
DUWO_INTERVAL=1800                              # DUWO 学生平台节奏慢，30min 即可
```

`Config.scrape_tasks()` 把每个 source 的配置展开成 `ScrapeTask` 后合并返回。未在 `SOURCES` 里的 source 不会被加载，方便 staged rollout：先在 staging 开 ourdomain 验证，再灰度到 prod。

#### Filter 跨 source 的归一化

不同平台的字段语义不一致，统一在 scraper 端做归一化：

| 字段 | H2S | OurDomain | DUWO | 归一化策略 |
|---|---|---|---|---|
| 城市 | `city: Eindhoven` | `location.city: Eindhoven` | `properties.city: Eindhoven` | `lower().strip()` 后比对 |
| 状态 | `Available to book / Available in lottery / Rented` | `Available / Reserved` | `Available / Sold` | 抽 `StatusKind` enum：`book` / `lottery` / `reserved` / `other`；每个 scraper 自己映射 |
| 房型 | `Studio / 1-room / 2-room` | `Studio / Apartment / Loft` | `Single / Shared / Studio` | 抽 `TypeKind` enum + 保留 raw；UI 端宽松匹配 |
| 能效 | `A+ / A / B / ...` | （可能没这字段） | （多数 不暴露） | optional，UI 端 missing 时不显示 |
| 价格 | `basic_rent: 707.000` | `price: 1200` | `kale_huur: 450` | 统一 `priceValue: float`（已是 Listing 字段） |

`ListingFilter` 已有的 cities / types / status_kinds / energy 字段保留，但**比对前 source-side 已做归一化**——每个 scraper 输出已经是标准化后的 `Listing`，filter 不用感知 source 差异。

#### Notification 路由

`notifier.py` 的模板加 source 前缀：

```python
def format_new_listing(listing: Listing) -> str:
    badge = _SOURCE_DISPLAY[listing.source]   # "H2S" / "OurDomain" / "DUWO"
    return f"[{badge}] New: {listing.name} · €{listing.priceValue} · {listing.city}"
```

iMessage / Email / Telegram / APNs 4 个 channel 都要更新模板。**APNs payload** 的 `aps.alert.body` 跟着改；payload 里加 `source` 字段，iOS 端解析后 deep link 跳转能定位到正确的详情页（因为 listing id 已经前缀化）。

#### 用户侧改动

**iOS 端**：
- `Listing` model 加 `source: String`，新增 `SourceBadge` view（绿色 H2S / 橙色 OD / 蓝色 DUWO），插在 `ListingRow.titleLine` 之前
- `ListingFilter` 加 `sources: [String]?`，filter sheet 加"按平台筛选"区
- Notifications 按 source 分组（"今天 OurDomain 出了 3 套"聚合通知）
- Settings 加 source enable / disable toggle——即使后端开了某 source，user 也可以本地屏蔽不感兴趣的平台

**Web 端**：
- listings 表格加 Source 列，可点击表头按平台排序
- Stats 页加"按平台占比"饼图 + 平台对比柱状图
- Filter 表单加 source multi-select

---

### 阶段拆分

| 阶段 | 内容 | 预计 |
|---|---|---|
| **P0** | 架构重构（`scrapers/` 包 + `Listing.source` + DB 迁移 + monitor.py 改造），现有 H2S 迁过来跑通无回归 | 1.5 周 |
| **P1** | **OurDomain** —— 实现 `OurDomainScraper`，首个第三方源验证 pipeline；UI 加 source badge | 2 周 |
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
