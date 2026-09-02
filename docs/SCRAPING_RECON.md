# 候选平台侦察

**本文档仅评估「尚未接入的平台是否值得接入」。** 已接入平台的现状不在此处：

- Holland2Stay / Xior / OurDomain 的传输方式与反爬对抗见 [ARCHITECTURE.md §3](ARCHITECTURE.md)
- Xior 的端点契约与自动预订见 [XIOR.md](XIOR.md)
- OurDomain 的端点契约与自动预订见 [OURDOMAIN.md](OURDOMAIN.md)

以下结论均来自真实的 HTTP 探测。反爬现状会随上游变化，**每条评估仅代表探测当时的
情况**，正式接入前必须重新测试。已接入的平台里，已有两个在半年内改过反爬
（Holland2Stay 迁移域名并启用 Turnstile，Xior 的端点启用托管挑战），这类变更今后
还会发生。

---

## 速览矩阵

> Xior 与 OurDomain 均已接入，不列入此表，相关文档见上文指引。

| # | 平台 | 公开可读 | 反爬 | 数据形态 | 量级 | ToS 风险 | 结论 |
|---|---|---|---|---|---|---|---|
| 1 | **Magis (magisrealestate.com)** | ✅ 完全 | **无** | Laravel + Livewire，服务端渲染 HTML | 5 城 17 栋，在架 12 套（可租 4） | 低（`robots.txt` 空 `Disallow:`） | ✅ **已接入**（2026-09-01），见 §0 |
| 2 | **HousingAnywhere** | ✅ 完全 | 无 | 页面内嵌结构化 JSON（`__staticRouterHydrationData`） | Amsterdam 207 条，每页 23 条 | 低（`/api/*` 被禁；分页 query 处于灰区） | 🟢🟢🟢 **建议作为下一个接入对象** |
| 3 | **SSH / SSHXL (sshxl.nl)** | ✅ 完全（API 已定位） | 无 | `POST /api/v1/offering/all`，JSON | 短租按院校配额，实测组合 0 条 | 低 | ❌ **不接**（无 Eindhoven；长租不在 API 内），见 §2 |
| 3b | **SSH& (sshn.nl)** | ❌ | 无，但 **Keycloak 登录墙 + 注册会员制** | Embrace 平台的 GraphQL 网关 | 未知 | **高**（同 DUWO/ROOM） | ❌ 不接，见 §2b |
| 4 | **OurCampus (ourcampus.nl)** | ✅ 完全 | SecureRC CF → curl_cffi 指纹轮换可过 | 与 OurDomain 同栈（RENTCafe HTML） | 1 栋楼 | 低 | ⚠️ **已接入但从未出过房源**，解析器未经真实数据验证，见 §4 |
| 5 | **Student Experience** | ✅ 完全 | **无** | 服务端渲染；学期档由 `/locations/getAcademicTerms/<id>` 出 JSON | 5 栋在营（Amsterdam 4 + Leiden），户型级，库存间歇 | 低 | ✅ **已接入**（2026-09-02，影子期），见 §5 |
| 5b | **Plaza (plaza.newnewnew.space)** | ✅ 完全 | **无** | `POST /portal/object/frontend/getallobjects/format/json`，空 body 出全站 JSON | 荷兰住宅 49 条，其中 31 条当场成交制 | 低（房源公开；ToS 无再分发限制） | ✅ **已接入**（2026-09-02，影子期），见 §5b |
| 5c | **Vesteda** | ✅ 完全 | **无** | `POST /api/units/search/facet`，空 body 出全站 JSON | 524 套 65 城，可租 113 | 低（`robots.txt` 无 `Disallow`） | ❌ **不接**：打分 + 同分抽签，推送无价值，见 §6b |
| 5d | **Gapph / Ad Hoc**（antikraak） | ✅ 完全 | 无 | Gapph 服务端渲染；Ad Hoc 走 WP REST `/wp-json/wp/v2/units` | Gapph 46 条、Ad Hoc 住宅 54 条，€150–€774 | 低 | 🟡 **候选**：价位对学生合适，但合同性质须先在 UI 区分，见 §6c |
| 6 | Pararius | ❌ | **Cloudflare JS challenge** | — | — | — | 🟡 现可采用 CloakBrowser（与 Holland2Stay 同一方案） |
| 7 | DUWO/ROOM | ❌ | 无，但存在 **auth-wall 与付费注册** | API 仅登录后可见 | 未知 | **高**（涉及转发登录后内容）| ❌ 不建议 |
| 8 | Kamernet | — | paid model | — | — | 高 | ❌ |

---

## §0 Magis Real Estate — **已接入（2026-09-01）**

### Endpoint

```
GET https://magisrealestate.com/for-rent?only_available=0
```

### 关键发现（2026-09-01 探测）

- 普通 `Mozilla/5.0` 直接 200，141 KB / 3.2s。**没有 Cloudflare、没有 JS 挑战、
  不需要浏览器、不需要代理**——是已接入的四个平台里最轻的一个
- `server: Apache`，Laravel + Livewire（Flux UI），房源在服务端渲染的 HTML 里
- `robots.txt` 是空 `Disallow:`，全站允许
- **query 参数能驱动 Livewire**：`only_available=0` 把结果从 4 条变成 12 条，
  连 `Not available` 的单元一起给

最后一条是接入的关键。本项目的状态变更通知需要「同一个单元从可租变成不可租」这个
事件，只抓可租的就只能看见「消失」——而消失是有歧义的（见 ARCHITECTURE §5.13）。

### 数据形态

一次请求返回**全部城市**的全部单元，不按城市分请求。因此每轮只发一次 HTTP，各
城市的 ScrapeTask 从同一份 HTML 里按城市切分（`scrapers/magis.py` 的
`batch_session`）。

卡片自带：状态、楼盘、户型、能耗标签、城市 + 街道、装修档位、面积、楼层、设施、
可入住日期、租金，以及**服务费的确切金额**。

### 价格：可以报到手价

```
€ 932,93 per month excl.
* service costs, furniture, utilities, internet & TV amount to € 121,51 per month
```

12 条实测全都带这一行。因此 Magis 与 Holland2Stay / Xior 同属「报到手价」一档，
不像 OurDomain / OurCampus 只能标注——它们拿不到单元的户型，而服务费按户型变。

### 覆盖面

| 城市 | 栋数 | 楼盘 |
|---|---:|---|
| Eindhoven | 9 | Aalsterweg 125-129、Aalsterweg 24-26、Boschdijk、Driek、The General、Kloosterdreef、Montgomerylaan、Woenselse Markt、Zernikestraat |
| Tilburg | 5 | The City、The Garden、Mr. X、The Rumour、The Vault |
| Rijswijk | 1 | Novum |
| 's-Hertogenbosch | 1 | De Wester |
| Amersfoort | 1 | The Wing |

价格区间 €190–1990，面积 2.3–110.9 m²。

### 风险与取舍

**规模是唯一的疑虑。** 在架 12 套、可租 4 套，远小于 Holland2Stay。接入赌的是这
17 栋楼的换手率，而一次探测判断不了。它比 §5 的 Student Experience（仅 1 栋可订，
已否）好得多，且 Eindhoven 占 9 栋——正是本项目的主力城市。

**解析必须按模式而不是按位置。** 卡片的可见文本顺序不稳定：有的多一行设施、有的
多一行租客徽标，后面的字段会整体顶掉一位。按行号取会把面积读成设施名，而那不会
抛异常。

**tenant 维度不登记。** 站点只在部分房源上打「Students only」徽标（12 条里 3 条），
筛选里另有一档「Starters」但没有任何房源带它——措辞与语义都还没见过。「没有徽标」
是「不限」还是「未标注」没有证据，而该维度 fail-closed，登记之后另外 9 条会被勾了
租客条件的用户整体过滤掉。徽标本身写进 features，通知里看得见，只是不参与筛选。

**不做自动预订。** 站点有 Account / Login，但下单流程未侦察，ToS 暴露面未评估。
与 OurCampus 一致。

---

## §1 HousingAnywhere — **强烈推荐**

### Endpoint
```
GET https://housinganywhere.com/s/{City}--{Country}
e.g. https://housinganywhere.com/s/Amsterdam--Netherlands
```

### 关键发现（2026-08-03 复测）

- 返回 HTTP 200 与 507 KB 的 HTML，**使用普通的 `Mozilla/5.0` UA 即可**，无
  Cloudflare 挑战
- 房源以**页面内嵌的结构化 JSON** 形式提供，无需解析 HTML 卡片
- 每页 23 条，Amsterdam 共 207 条，约合 9 页

### 数据形态

房源数组在：

```
window.__staticRouterHydrationData = JSON.parse("…")
  → .loaderData.<routeId>.listings          # 23 条
```

需注意该数据为**双层编码**：外层是 JS 字符串字面量，内层才是 JSON，须解码两次。
同一 blob 中还含有 `undefined` 字面量（JS 并非 JSON），直接调用 `json.loads` 会
抛出异常，须先将 `:undefined` 替换为 `:null`。

单条 listing 的字段：

```
id, internalID, recordType, advertiserId, isPartner, depositPolicy,
price (分), priceEUR, currency, street, city, country, countryCode,
_geoloc {lat, lng},  ← 自带经纬度
isNew, propertyType, previewImage, photos, photoCount,
minimalRentalPeriod, maximumStay
```

`_geoloc` 是最主要的额外收益：现有的三个 source 均须经由 geocode pipeline 才能
在地图上呈现，而该平台直接提供坐标。

> **旧侦察记录中的三条解析路径均已失效**，不应据此编写代码：
>
> - JSON-LD（`<script type="application/ld+json">`）现仅保留聚合值
>   （`offerCount` / `lowPrice` / `highPrice`），**不含单条房源**；
> - `window.__PRELOADED_STATE__` 现仅保留搜索元数据（价格分布等），**不含房源**；
> - `data-test-locator="ListingCard/*"` 锚点仍然存在，但既然已有结构清晰的 JSON，
>   便无需再解析 HTML。
>
> 这正是文首那条判断的实例：**评估仅代表探测当时的情况**。

### robots.txt

```
User-agent: *
Disallow: /api/*       ← 不要直接打他们的 API
Disallow: /my/*        ← 用户私人区
Disallow: /admin
...

sitemap: https://housinganywhere.com/sitemap.xml
```

`/api/*` 被明确禁止，因此只能采用页面内嵌 JSON，不得直接调用其 API。这与既定的
实现方式恰好一致。

**但存在一处灰区：`Disallow: /*/s/*?*` 所禁止的是带 query string 的搜索页，而分页
恰好使用 `?page=2`。** 该模式要求 `/s/` 之前另有一段路径，而所用的
`/s/Amsterdam--Netherlands?page=2` 位于根路径之下，严格而言并不匹配；但各 robots
解析器对于 `*` 能否匹配空串的处理并不一致。

保守做法是仅抓取第一页（23 条最新房源，对「新房源通知」的用途已经足够），不做
翻页。若确需翻页，应先确认其服务条款。

### 工程评估

| 项 | 评分 |
|---|---|
| 公开数据 | ⭐⭐⭐⭐⭐ Amsterdam 207 条，覆盖荷兰全境 |
| 抓取难度 | ⭐⭐⭐⭐⭐ 普通 UA 加内嵌 JSON，无任何技术障碍 |
| 数据稳定性 | ⭐⭐⭐ 内嵌 blob 的路径两个月内已变更过一次；`data-test-locator` 锚点可作备选 |
| 合规风险 | ⭐⭐⭐⭐ listing 页可抓取；分页 query 处于 robots 灰区 |
| 用户重叠度 | ⭐⭐⭐⭐⭐ 国际学生与年轻职场人群，与 FlatRadar 的核心用户高度重合 |

**预计工程量：1.5–2 周**（HTML 解析、城市列表配置与入库适配）。

---

## §2 SSH / SSHXL (sshxl.nl) — **不接（2026-09-01 复测后改判）**

> 2026-08-03 的结论是「🟢 推荐（需先定位其 API）」。API 已于 2026-09-01 定位到，
> 但同一次侦察推翻了当初推荐的依据——见「为什么改判」。

### Endpoint（已完整定位）

```
GET  https://www.sshxl.nl/api/v1/offering/filters
       ?ContingentId=<guid>&DemographicId=<guid>&MainPeriodId=<guid>
POST https://www.sshxl.nl/api/v1/offering/all
       content-type: application/json
       {"Filter":{"Contingent":"<guid>","Demographic":"<guid>","MainPeriod":"<guid>",
                  "FlexiblePeriodFilter":{"DateRange":{},"ExactDateRange":{}}}}
```

匿名可调，两个端点都返回 200 + JSON。路由是从 `js/portal.min.js`（6.2 MB）里挖出
来的，再在页面上 hook `fetch` 截真实请求体验证。`SearchAfter` 是游标（分页）。

`/api/v1/registration/contingents` 需要登录（匿名 403），但抓取用不到它——
contingent/demographic/period 三个 GUID 可以从公开的三步向导里取到。

### 反爬与合规

```
server: Kestrel（.NET）      无 Cloudflare，无挑战
robots.txt: Disallow /hangfire/ /admin/ /my-ssh/ /mijn-ssh/ /styleguide/
```

**`/api/` 不在 Disallow 内**，房源页与 sitemap 亦公开。ToS 风险低。

### 为什么改判

**一、覆盖城市与旧结论记的不一样。** 旧文写「分布于全国 9 个城市（Utrecht、
Eindhoven、Amsterdam、Maastricht 等）」——**这句是错的**。2026-09-01 从短租向导
的城市单选里读到的实际列表是：

```
Groningen · Rotterdam · Tilburg · Utrecht · Zwolle      （页脚另有 Amersfoort）
```

**没有 Eindhoven，也没有 Amsterdam。** 本项目的主力城市不在其中，这一条基本就
定了结论。

**二、长租根本不在 API 里。** bundle 里只有 `shortStayOfferingApi`，没有长租的
对应物。长租走注册排队——`/api/v1/cms/en/cities` 自己给出的等待时间是 2 到 36 个
月不等（规模从 ±30 到 ±13.000 间房）。也就是说学生住房的**主体部分不可监控**。

**三、短租按院校配额发放，不是一个列表。** 要选完四步才查得到：

```
城市（5）→ 教育机构（每城约 5 个）→ 学生类型（5）→ 租期（若干）
```

每个组合是独立的一次 POST，覆盖全部即数百次请求一轮；对比 Magis 是一轮一次。
而且短租只对特定院校的交换/硕士生开放，不是公开房源。

**四、没有真实数据可核对。** 实测组合（Utrecht / Master / 2026-2027）返回 `[]`，
`filters` 里 `AccomodationTypes` 与 `Complexes` 也都是空的。这与 OurCampus 接入
时的处境相同，而那次的代价是状态映射错了三周无人发现（见 CHANGELOG v1.26.0）。

### 唯一有利的一点

短租页面明写 **"First-come-first-serve"**，形态与本项目相符——这与长租的排队制
不同。**若 SSHXL 日后进入 Eindhoven，值得重新评估**：接口契约已在上面记全，
不必再挖一次 bundle。

---

## §2b SSH& (sshn.nl / mijn.sshn.nl) — **不接**

与 §2 的 SSHXL **不是同一家**，只是都在名字里带「SSH」（荷兰的学生住房基金会
历史上普遍叫 *Stichting Studenten Huisvesting*，因而撞名）。两者的区别有据可查：

| | SSHXL (sshxl.nl) | SSH& (sshn.nl) |
|---|---|---|
| 自我描述 | SSH Student Housing | 「Wij verhuren studentenkamers in **Nijmegen en Arnhem**」 |
| 城市 | Groningen / Rotterdam / Tilburg / Utrecht / Zwolle / Amersfoort | Nijmegen / Arnhem |
| 互相引用 | 无 | 无 |

首页文本里 Nijmegen 出现 15 次、Arnhem 9 次，而 Utrecht / Rotterdam / Tilburg /
Groningen / Zwolle **各 0 次**；SSHXL 那边正好相反。城市集合完全不重叠。

### 技术形态

`mijn.sshn.nl` 是租户门户（React SPA），跑在 **Embrace**（`embracecloud.nl`）
这个白标住房门户平台上，不是自研。

```
GraphQL 网关   https://mesh-router.embracecloud.nl/graphql
认证           Keycloak OIDC · auth.embracecloud.nl/auth/realms/sshn
证据           页面加载即请求 silent-sso.html?error=login_required
```

匿名 POST `{ __typename }` 网关会回 `{"data":{"__typename":"Query"}}`，但有数据的
字段必然要 token。

### 为什么不接

1. **登录墙 + 注册会员制。** 站点自述「you can only react if you have a valid
   proof of study」。这与 §6 的 DUWO/ROOM 是同一类，本文档对那一类的结论是
   「高风险（涉及转发登录后内容）❌ 不建议」。
2. **不是先到先得。** 分配模型是 Register → Respond → Priority → **Lottery**，
   页面另有「Results allocated rooms」。秒级通知在这里没有价值。
3. **无 Eindhoven。**

**侦察到此为止，未去枚举 GraphQL schema。** 该系统整站为注册会员制，继续深入即是
在寻找绕过认证墙的路径，与本文档对 DUWO/ROOM 的既有判断相悖。

> Embrace 是平台供应商而非 SSH& 自研，荷兰可能另有住房法人跑在同一套上
> （`matomoembracehousing` 这一命名暗示它是其住房产品线）。理论上一份「Embrace
> 形态」的适配可覆盖多家，但它们共用同一堵认证墙，不改变结论。真正值得找的是
> **跑在 Embrace 上、却把房源公开**的那种法人。

---

## §3 Pararius — **可探测，基建已到位**

### 直接测试结果

```
GET https://www.pararius.com/apartments/amsterdam
→ HTTP 403 + cf-mitigated: challenge + "Just a moment..."
```

普通 `Mozilla/5.0` UA 会触发 Cloudflare 的 5 秒挑战。`curl_cffi` 能否通过尚未实测——OurDomain 的 SecureRC 同样曾被判定为「CF hard block」，而 curl_cffi 可以通过，因此值得先尝试轻量方案；若无法通过，再改走浏览器传输层。

### robots.txt（允许浏览）

```
User-agent: *
Disallow: /contact/*, /report-*, /account/*, /checkout/*, /*/Kamer-te-huur/*
```

robots.txt 本身较为宽松，但 Cloudflare WAF 并不依据 robots 判定，而是依据请求指纹。

### 工程评估

浏览器传输层已是现成基建（只需 `BrowserFetcher` 加一个新的 `SiteProfile`），因此
「需要浏览器」不再构成阻塞理由。真正的未知在于 Pararius 的反爬强度——其可能严于
Holland2Stay（例如采用 DataDome 一类方案），必须实测确认。成本方面需注意：每增加
一个 Cloudflare 平台，即意味着增加一个常驻浏览器（约 200–400MB）与一条专属线程。

---

## §4 OurCampus (ourcampus.nl) — **已接入**

Greystar 旗下的另一个学生住房品牌，与 OurDomain 同属一家公司。

> **本节记录的评估结论为「投入产出比不划算」，接入是在明知这一点之后作出的产品
> 决策。** 以下分析保留原貌：它说明了为何应当降低对该 source 的预期，以及运维时
> 需要注意的事项。实现见 [`scrapers/ourcampus.py`](../scrapers/ourcampus.py)。

### 技术面（2026-08-03 实测，全部通过）

| 项 | 值 |
|---|---|
| 栈 | Webflow 前台 + RENTCafe/SecureRC 后台，**与 OurDomain 完全同栈** |
| RENTCafe host | `new-ourcampus-amsterdam-diemen-rentcafewebsiteuk.securerc.co.uk` |
| slug / property_id | `new-ourcampus-amsterdam-diemen` / `186609` |
| 反爬 | 普通 curl 返回 403 与挑战页；**curl_cffi 使用 `chrome136`，首个指纹即返回 200** |
| 已解析 | 3 个 floorplan：`1113259` Standard+ Studio 1P / `1112904` Furnished Student 1P / `1112905` Furnished Student 2P |

### 为什么它的期望值应该放低

**其一，仅有一栋楼。** `/en/apartments` 全站仅列出 Amsterdam Diemen，地址为
Dalsteindreef 6002，与 OurDomain South-East（Dalsteindreef 20-40）位于同一街道且
相邻。接入后仅增加一栋楼。

**其二，等待期为 16–18 个月（官网自述）。** 该平台采用排队制而非先到先得，
「房源出现即刻推送」这一核心价值并不成立。其否决理由与 DUWO/ROOM 属同一类型，
区别在于前者是合规问题，此处则是产品问题。

**其三，同栈并不等同于同接口。** 其单元查询采用 **POST 与 `floorPlans[]` 表单体**
（页面 jQuery 为 `.load(url, {floorPlans: names})`），而 OurDomain 采用 GET 与
query string。

实现上的处理方式：`OurCampusScraper` 继承 `OurDomainScraper`，仅覆盖
`_fetch_units_html()` 一个方法，其余部分（指纹池与冷却、每次尝试更换 IP、同一
session 内 403 重试、单元行解析、状态映射、Occupancy 反推、Listing 映射）全部复用。

> 需注意接入时**无法依据响应判断 GET 与 POST 孰为正确**——该楼零可订，两种请求
> 形态均只能取得空面板。选择 POST 的依据是「与其自身前端保持一致」，这是唯一有
> 证据支持的形态。基类的 GET 对 OurDomain 实测有效，未作改动。

### 一条未验证的事

实测时该楼**没有任何可订单元**（floorplans.aspx 上 apply 按钮数量为 0，Get
Notified 为 6 个；页面中唯一出现的 `applyButton` 字样位于一行被注释掉的 JS 中）。
因此 `contentclass=availableunits` 返回的是 floorplan 网格而非单元表，**其单元表的
HTML 结构未经验证**。

判定为「确无房源」而非「解析失败」的依据是对照实验：以同一份代码、同一个指纹请求
OurDomain Diemen，`unitrow` 分别为 2 与 1，解析正常。缺少该对照即会落入
「未取得数据不等于确认不存在数据」这一类判据错误（见
[ARCHITECTURE.md §5.10](ARCHITECTURE.md)）。

**接入后仍然如此：第一次真实有房时必须人工核对一次日志。**

**截至 2026-08-04 该情形尚未出现**：接入以来 `unitrow=yes` 一次都未曾出现，
`data/ourcampus_capture.txt` 中始终为 `panel=yes unitrow=no parsed=0`。也就是说
该 source 的解析器、状态映射、阈值，以及「feed 仅列出可订单元」这一前提，至今
**未经任何真实数据验证**。（本地实例的情况可查阅同一文件。）

> **2026-08-27 更新。** 上述情形已经出现，留档样本也已用于核对解析器。核对结果
> 是本节的两条前提均不成立：feed 并不只列出可订单元，且置灰的日期单元格表示
> 「自该日起可订」而非「已出租」，按后者理解会使整批可订单元被判为 Occupied 且
> 不发通知。判据已改为依据该行是否带有可用的下单按钮，详见 CHANGELOG v1.26.0。
> 本节以上内容保留为当时的侦察记录，不再代表当前实现。

为此增设了两道守卫，二者均位于基类中，OurDomain 一并受益：

**① 结构判据。** 解析出零单元时，检查响应中是否存在 `Apartment Search Result`
面板标题。存在则说明这是一张结构完整的搜索结果页，可判定为确无可订单元；不存在
则说明取到的并非单元面板，此时标记 `complete=False` 而非上报「零可用」。两种情形
的 HTTP 状态均为 200，缺少该守卫将导致 stale 收敛清空存量 listing。

**② 连续确认**（v1.13.0）。结构判据无法识别「确实是目标表格，但本次渲染异常」
——URL 相同、面板标题相同，仅单元行未渲染，两种响应在特征上无从区分。因此要求
**同一栋楼连续 3 轮解析出 0 个单元，才允许其参与 stale 收敛**
（`OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM`）。未达计数时房源照常入库，仅本轮不参与。

两道守卫均无法覆盖的情形：**单元行结构发生永久性变更，而页面仍是合法面板**
——此时会稳定返回 0 个单元，连续 3 轮同样会达成计数。该情形只能依靠人工核对，
capture 文件正是为此保留。

---

## §5 Student Experience (studentexperience.com) — **已接入（2026-09-02）**

自营学生公寓运营商，形态上最接近 Xior 与 Holland2Stay（自有房源池、非排队制）。
荷兰五处在营，另有一处在建。

> **2026-09-02 改判。** 本节此前的结论是「暂不做」，理由是「线上可订的范围过小
> ——为 1 栋楼、2 个房型而逆向其 JS 或增加一个常驻浏览器，投入产出不成比例」。
> **那个判据是错的**，下面逐条列出错在哪里。错误的性质值得记下来：不是数据过期，
> 是**把过滤后的视图当成了全集**。

### 三处需要更正的原始结论

**一、「荷兰只有 Minervahaven 可订」——错。**

原文的判据是「其自有预订组件 `/studios` 的 `locationId` 下拉框：其中属于荷兰的
仅有 `2 = Amsterdam Minervahaven`」。这个观察本身没错，错在**当时打开的是
`?los=shortstay`**——`los` 会反过来过滤地点下拉框。不带 `los` 参数时：

| locationId | 楼盘 | 城市 |
|---|---|---|
| 2 | Amsterdam Minervahaven | Amsterdam |
| 3 | Amsterdam Zuidas | Amsterdam |
| 4 | Amsterdam NDSM | Amsterdam |
| 5 | Amsterdam Amstel | Amsterdam |
| 36 | Leiden | Leiden |
| — | Amstelveen Uilenstede | 站点标注 "Under development"，无 id |

西班牙另有 `8 = Granada`、`7 = Madrid Pozuelo`，不属本项目范围。

原文另记「Amsterdam Zuidas ❌（站内公告 2026 年关闭）」——2026-09-02 复测时它仍在
导航、FAQ 与 `locationId` 下拉框中，且出现在长租页的按楼盘计数块里。

**二、「`academicTermId` 下拉框始终为空，对应的 XHR 端点未能定位」——已定位。**

```
GET /locations/getAcademicTerms/<locationId>
→ {"terms":[{"yardiAcademicTermIdValue":"1678",
             "academicTermName":"12 Months (01-Oct-2026 - 30-Sep-2027)",
             "isShortStay":"1"}], "hasTerms":true}
```

端点写在 `/themes/default/js/location-term-filter.min.js`（4KB，未混淆到读不了）
里，页面内联脚本给出它的配置对象。下拉框「始终为空」是因为它由该请求异步填充，
而请求只在选定 `locationId` 之后才发。

**三、「`los=longstay` 模式下楼盘与房型的下拉框均不存在」——不完整。**

下拉框确实不在，但那一页有别的东西：一个**按楼盘的可订计数块**，有货没货都渲染。

```
City     Amsterdam 0    Leiden 0
Complex  Amsterdam Amstel 0   Amsterdam NDSM 0   Amsterdam Zuidas 0   Leiden 0
```

这个块后来成了 scraper 的完整性探针，见下。

### 为什么现在接

**先到先得。** FAQ 原文：「For short-stay studios at Amsterdam Minervahaven we
work on a **first-come, first-served basis**. This means that if you're the first
person responding to a studio advertisement and you meet the requirements, the
studio is yours.」

这一条是接入的全部理由，也是它与 Vesteda 的分水岭——后者是打分 + 同分抽签，早
三十秒收到通知不会提高中签概率，推送没有意义（见 §6b）。

**纯学生盘。** 「All Student Experience studios are exclusively available for
students」，签约前须通过服务门户上传在读证明，入住时须为在校注册状态。合格身份
含 study programme / internship / **PhD research**——**与 OurCampus 相反**，那边
的 criteria 明确排除 PhD 与博后。两处都登记为 `student only`，但含义不同，真出现
按学位分档的需求时不能合并处理。

**库存是间歇性的。** 站点自己提供「有房时邮件通知我」的订阅，等于承认这一点。
这正是监控类产品的形状：平时安静，放盘时才响。

### 技术形态

纯 HTTP、服务端渲染、无 Cloudflare、无 JS 挑战、不需要浏览器。与 Magis 同级。

库存切成两条互不相交的线，各有入口但**共用同一套卡片 DOM**：

```
短租  /studios?los=shortstay&locationId=<id>&academicTermId=<term>
长租  /studios?los=longstay
```

⚠️ `locationId` 在长租路径上**被忽略**：传 `locationId=8`（Granada）返回的仍是
荷兰四栋楼的计数。所以长租每轮只发一次请求，不按楼盘循环。

每轮请求预算：长租 1 次 + 学期档 5 次 + 有档期的楼盘各 1 次 ≈ 7 次，整批共用。

### 卡片形态

`<a href="/studio-types/<id>" class="studio is-overview …">`，两种变体：主卡片
（`has-popularity-header`）与「Or explore our other studios」滑块里的紧凑卡片
（`studio-compact`）。**两者都是当前可订的户型**——0 库存的楼盘连滑块都不渲染。

粒度是**户型级**而非单元级：一个户型对应多间，面积因此是区间。稀缺徽标
`Only N studios available` 只在余量少时出现，不能当作余量字段——没有徽标不代表
没货，只代表站点不觉得需要催。

### 库存变化发生在**学期档**这一层

这是接入后第一天就观察到的，值得单独记——它同时纠正了本节自己的一处草率结论。

2026-09-01 22:07 的快照（Minervahaven，唯一学期档 `1678` = 01-Oct-2026 –
30-Sep-2027）：

| studio-type | 户型 | 起价 | 面积 | 余量徽标 |
|---|---|---|---|---|
| 11 | Signature studio | €1.799/月 | 20,5–26 m² | Only 2 studios available |
| 10 | Essential studio | €1.750/月 | 20–22 m² | 无 |

2026-09-02 09:48（约十四小时后）多出一档 `1654` = **02-Sep-2026 – 01-Sep-2027**，
并随之带出第三个户型：

| 14 | Core Studio | €1.550/月 | 20–22 m² | Only 1 studio available |

**由此更正本节的一处结论。** 依据第一份快照，本节曾写「原文记的 `14 Core Studio`
现在不在售」——那是错的，或者说下得太早：它当时不在档期 `1678` 里，而档期 `1654`
那时还不存在。**同一个户型在一个学期档下看不见、在另一个档下就在售**，拿单一档期
的快照断言「某户型不在售」是没有依据的。

这也是接入前一直缺的那个证据：**放盘的粒度是学期档**，不是户型。一个新档期开出来
会一次带出若干户型。scraper 遍历 `getAcademicTerms` 返回的全部档期，因此这类变化
抓得到；只盯某一个 `academicTermId` 的实现会整段漏掉。

（€1.550 这个数在楼盘页上一直挂着，但 2026-09-01 时它并不是 `/studios` 选择器里
的可订项——楼盘页写的是价格带，不是当下可订清单，两者不能互相印证。）

### 完整性探针：为什么必须用长租页的计数块

这是接入本平台唯一不显然的工程决定。

短租路径上「0 张卡片」有三种成因，**从 HTML 上分不出来**：真的没货、没选学期档、
或者站点改版换了卡片类名。三者里只有第三种该判 incomplete，可是页面在前两种情况下
也一样干干净净——连 "we don't have available studios" 那句提示都不出（实测
`?los=shortstay&locationId=3`）。

判错的代价不是少推几条，是**整批存量被 stale 收敛判成 Occupied 并发一批假的下架
通知**。

长租页的计数块有货没货都渲染，因此可以当结构探针：读不到就是改版了，整轮判
incomplete；读得到则说明 DOM 还是我们认识的那个，此时「0 张卡片」可以放心地当成
「真的没货」。这条探针还顺带给出一个交叉校验——计数之和大于零却一张长租卡片都没
解析出来，同样判 incomplete，那是「计数块还在、卡片结构变了」的情形。

### 维度登记

`type`（全站皆 studio，四个档位名是价位分层不是房型）与 `tenant`（来自
`SOURCE_ASSUMED_FEATURES`）。

`finishing` **不登记**：规格行（"Private & fully furnished"）只出现在主卡片上，
紧凑卡片没有。该维度 fail-closed，登记之后紧凑卡片那几条会被勾了装修档位的用户
整体过滤掉——与 Magis 对 `tenant` 的取舍同一个道理。值仍写进 features，通知里
看得见，只是不参与筛选。

`floor` / `energy` 站点不给。楼层只在设施行的散文里出现过（"located on floor
5-8"），那是户型的整体描述而非某一间的楼层。

### 短租的额外资格条件（无对应筛选维度）

Minervahaven 短租线要求：临时居留（≤1 年）、荷兰境外常住地址、外国国籍，**不接受
用荷兰地址提交的申请**。这几条不适合塞进 `tenant`（那个维度的取值表是身份类别，
不是居留状态），因此只在文档与 scraper 注释里记录，用户仍需自行核对 FAQ。

### 粒度：曾经是单元级，现在不是

archive.org 存有 **505 个 2023 年的单元页**，URL 形如 `/studios/2512`，id 范围
369–3122——站点当年是**按单元**挂牌的。若这一层还在，scraper 就该改到单元级：
身份与 churn 信号都会准得多，稀缺徽标那个粗略的余量字段也不再需要。

**2026-09-02 复验：已经不在。** 在 369–3122 区间取样四个 id（369 / 2512 / 2513 /
3122），全部返回 `404`，`canonical` 指向 `/404`。站点确已迁到户型粒度，现有
scraper 的层级是对的。

（首次尝试复验时站点正好在故障，07:17–07:38 UTC 返回 HTTP 500，`/studios/2512`
与 `/studio-types/11` 一并不可达；上述结论取自恢复之后的复测。）

### 不做自动预订

「first-come, first-served」意味着下单窗口很短，但下单流程未做侦察，ToS 暴露面
亦未评估。与 OurCampus / Magis 一致：只通知，不预订。

---

## §5b Plaza (plaza.newnewnew.space) — **已接入（2026-09-02）**

运营方 Plaza Resident Services，跑在 **Zig / Hexia** 平台上（`sdk.zig365.nl`、
`zds-cdn.zig365.nl`）。站点自我描述是「Woonruimte voor **studenten**, starters en
expats」。覆盖荷兰多城，另有德国 Bochum 与波兰 Poznan——本项目只取荷兰。

### 接口：一个 POST 拿全站

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{}' \
  https://plaza.newnewnew.space/portal/object/frontend/getallobjects/format/json
```

匿名 200，无 cookie、无 referer，2026-09-02 实测 167 KB / 55 条。GET 也返回同样
内容。字段是结构化 JSON 而非散文：`totalRent` / `netRent` / `areaDwelling` /
`floor` / `latitude` / `longitude` / `postalcode` / `doelgroepen` /
`constructionYear` / `closingDate` / `publicationDate` / `urlKey`。

端点是从 `/aanbod/wonen` 页面的内联配置顺着 `wzp-angular-bundle.min.js`（367 KB，
未混淆到读不了）找到的，同一份 bundle 里列着 115 个 `/portal/**/format/json`
端点。另有一个更现代的 `POST /api/v1/actueel-aanbod`，但在这台主机上 404——bundle
是所有 Zig 门户共用的，那条路由 Plaza 没开。

`robots.txt` 只禁 `/portal/uploads/*/floorplans/*` 与 `/portal/uploads/*.pdf`，
本端点不在禁用范围内。**因此 `floorplans` 里的文件不抓**，只用 `pictures`。

### 分配机制：这一节是接入判断的全部依据

⚠️ **本节的第一版是错的。** 当时从荷兰语字段名
`model.advertentieSluitenNaEersteReactie` 直译，写成「首个回应之后广告即关闭」，
并把另一类说成「有截止时间，到点后再分配」。**两半都不准**，下面是站点自己的文案
（取自 `POST /portal/core/frontend/gettranslations/format/json`，3595 条）。

**`dth`（31 条，`advertentieSluitenNaEersteReactie` 为真）**

站点内部叫 DTH，标题 `ModelTitleDTH` = "Eerste reactie"：

> `VolgordeBepalingDescriptionDTH`：「De eerste die reageert en voldoet aan de
> voorwaarden die genoemd worden in de advertentie, krijgt de woning aangeboden」

应征时会弹确认框 `bevestigAdvertentieSluitenNaEersteReactie`：

> 「Wil je deze kamer **definitief boeken**? Dat betekent dat je deze kamer
> accepteert en **geen andere aanbieding meer krijgt**。」

也就是说：**点下去当场成交**，不是「排队等挑」。这是本文档考察过的所有平台里最强
的形态。但代价也最重——**接受即放弃其它全部 offer**，第一版完全漏掉了这一条，而
催用户「快点」却不说清代价是不负责的。因此这句话原样写进 `Listing.features`。

这 31 条全部是 Utrecht 的学生 studio（16 m²，€712.75–€867.75）。

**`reactiedatum`（荷兰 18 条 / 含德国共 22 条）**

站点给用户看的标签是 `reactiedatumfilteroptionlabel` = **「Snelle reageerder」**
（快速响应者）：

> `ModelCategorieExplanationReactiedatum`：「Wij werken **niet met een
> wachtlijst**。Dus deel zo snel mogelijk alle benodigde gegevens met ons. Voldoe
> jij daarmee aan alle criteria dan maak je **meteen** kans op de woning」
>
> 搜索档说明：「Snelle reageerder — **Wees er snel bij!** Zo maak jij de grootste
> kans om deel te nemen aan een bezichtiging」

有 `closingDate` 兜底，但**不是**「到点统一开奖」。第一版把它说成截止分配，等于把
这一类的价值说轻了——实际上它同样奖励速度。

**平台支持、但 Plaza 当前一条都没用的模型**

| code | 站点说明 | 推送价值 |
|---|---|---|
| `loting` | 「Na het sluiten van de reactietermijn wordt er door de computer **geloot**」 | ❌ 抽签，早知道不提高中签率 |
| `inschrijfduur` | 「We bepalen de volgorde aan de hand van **inschrijfduur**」 | ❌ 按注册时长排队 |
| `hospiteren` | 「De bewoners nodigen kandidaten uit… en kiezen een nieuwe huisgenoot」 | ❌ 合租面试 |
| `woningruil` | 换房 | ❌ |

这四类正是否决 Vesteda（§6b）与 DUWO/ROOM（§6）的那一类理由。当前 55 条一条都没
有，但**将来可能出现**——因此 scraper 逐条记录模型而不是整站断言，
`ALLOCATION_LABELS` 里六种文案都给全了，抽签与排队那两条明写 `speed does not
help`，真出现时通知里看得见。

### 与 DUWO/ROOM 的区别——这一条决定能不能接

两者都要花钱注册：ROOM 约 €30/年，Plaza 是 €27.50/年
（`/inschrijven/registreren`：「Registreren op NewNewNew kost €27,50. Daarmee kun
je één jaar lang reageren op woningen」）。DUWO 因此被否（§6），但**否决理由不是
收费本身**：

| | DUWO / ROOM | Plaza |
|---|---|---|
| 匿名能否看到房源 | ❌ `product-search` 对匿名请求返回 404 | ✅ 无 cookie 无 referer 直接 200（两次独立验证） |
| ToS 是否禁止再分发 | ✅ 明确禁止「将通过本服务获得的信息再行分发」 | ❌ disclaimer 只有标准免责声明 |
| 我们要不要持账号 | 必须——否则连数据都拿不到 | 不必——只读监控完全不需要账号 |

那 €27.50 买的是「能应征」，不是「能看见」，与 Xior / H2S 需要账号才能下单是同一
类，不影响只读监控。

**但这笔钱必须让用户知道**：全部 55 条的 `inschrijvingVereistVoorReageren` 都是
true，收到通知却没注册就动不了。因此每条 listing 都写一条
`Registration: required to respond (paid account)`。

### 数据形态与过滤

同一个端点返回全部对象类型与全部国家，三道过滤都必须做：

```
55 条响应
 ├─ 2 条 dwellingType.categorie == "voorVoertuig"   停车位 —— 不筛会推「€50 的房源」
 ├─ 4 条 land.id != "524"                            德国 Bochum
 └─ 49 条荷兰住宅                                     ← 实际入库
```

国别判据用 `land.id` 这个主键，**不用 `regio.name` 的前缀**——后者是展示文案
（"Nederland - Utrecht"），改一次文案就会让按前缀判断的实现静默漏掉全部荷兰房源。
两者 2026-09-02 一致，可互为交叉校验。

城市分布（2026-09-02）：Utrecht 32、Geldrop 6、Amsterdam 3、Enschede 2，
Delft / Maastricht / Eindhoven / Deventer / Duivendrecht / Groot-Ammers 各 1。

租客维度**逐条读 `doelgroepen`**，不是整站断言：同一批里 student 39 / regulier 16
并存。只有恰好等于 `{student}` 的才写 `student only`，混合标记的不写、让该维度对
它们 fail-open——替站点断言「只有学生能租」是错的，而这正是 Xior 2026-08-21 在
finishing 上栽过的那种错。

### 城市清单一定会漂

`KNOWN_PLAZA_CITIES` 是站点导航自述的八城与当时实际在架的十城取并集。**两份本来
就对不上**：在架的 Geldrop（6 条，第二多）、Groot-Ammers、Deventer、Duivendrecht
都不在导航里。

站点上架新城市时不会有人来改这份表。未登记城市的房源不会被静默丢掉——scraper 按
WARNING 记下城市名，日志里看得见，加进表即可。宁可漏推几条也不猜城市：猜错会把
房源分派给错误的 ScrapeTask，用户按城市订阅就会收到不该收的。

### 完整性探针

响应里的 `sAngularServiceData` 带着门户配置，**有房没房都返回**，因此拿它当结构
探针：读得到就说明这是一份真的接口响应，此时 `result` 为空可以放心当成「当前没有
在架房源」（先到先得那 31 条在首个回应后即关闭，短时间清空是可能的）；读不到则说
明拿到的是别的东西（改版、网关、登录墙），不能把空结果当成「没房源」——那会让存量
被整体收敛成 Occupied 并发一批假的下架通知。

另外，路由不认识时站点返回的是**一整页 HTML** 而不是 JSON（实测 `/api/v1/*` 的
404 是 158 KB HTML），因此 JSON 解析失败要上抛 `ScrapeNetworkError` 而不是当成空
结果。

### 不做自动预订

应征需要付费账号，流程未侦察，ToS 暴露面未评估。与 OurCampus / Magis /
Student Experience 一致：只通知，不预订。

---

## §6 DUWO/ROOM (room.nl) — **不建议**

### 端点
- `GET /api/v1/PreferredCities` → 200 ✅（9 城市 + UUID）
- `GET /api/v1/product-search?pageIndex=0&pageSize=20&...` → **404 anonymous**

### 业务模型
- ROOM.nl 是 DUWO 与其它学生住房组织的统一搜索平台
- **用户必须先完成注册并支付约 €30/年的 waiting list 会员费**，方可查看 listings
- 其 API 设计本身即为 `credentials: "include"`，必须携带登录态 cookie
- 这正是 ROOM 商业模式的核心：出售 waiting list 服务

### 技术可行性与合规适当性

- 技术上可行：使用一个真实账号，由 scraper 登录并维持 session，调用 `product-search`
- 合规上不宜：DUWO 的服务条款明确禁止「将通过本服务获得的信息再行分发」。FlatRadar
  若将 DUWO 数据推送给非账户持有者，属高风险的条款违反行为
- 存在单点故障：账号一旦被锁定，全部用户的 DUWO 监控随即停摆
- 存在身份验证门槛：DUWO 注册需要 student ID 与已付费状态，难以批量获取

**结论：不予接入**。有意监控 DUWO 的用户应自行注册，并使用 ROOM 自带的邮件提醒
功能。

> ⚠️ **别把「要付费注册」本身当成否决理由。** Plaza（§5b）同样收费（€27.50/年），
> 却接了——区别在于 Plaza 的房源**匿名可见**、ToS 也没有再分发限制，那笔钱买的是
> 「能应征」而不是「能看见」，只读监控完全不需要账号。DUWO 的两条否决理由是
> **数据在登录墙后**与**ToS 明确禁止再分发**，缺了任一条结论都会不同。

---

## §6b Vesteda — **不接（技术上最省事的一个，业务模型否决）**

2026-09-01 侦察。本节值得单独写，因为它是**技术判据与业务判据给出相反结论**的
典型：接口开放到几乎不用写解析器，但推送在这个平台上没有价值。

> 本文档此前把 Vesteda 归在 §7 的「自研前端」一类，注为「房源由客户端渲染，须逆向
> API 或引入浏览器，成本接近接入一个新平台」，并列为「尚未排除、值得投入约半天
> 评估」。**技术判断是错的**——实际不到十分钟就拿到了全量数据。

### 接口：一个 POST，空 body 出全站

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{}' \
  https://www.vesteda.com/api/units/search/facet
```

825 KB JSON，**524 套，65 个城市**，无认证、无反爬。`robots.txt` 只有一行
`Sitemap:`，没有任何 `Disallow`。

端点来自 `/nl/woning-zoeken` 页面里的 `vesteda.apiUrl='/api/units'`，实际调用在
`/static/vue/dist/js/app.js`（88 KB，未混淆到读不了）：`getUnits(e){return
Vt.post("/search/facet", e)}`。

字段齐到不需要二次抓详情页：`priceUnformatted` / `price` / `size` /
`numberOfBedRooms` / `latitude` / `longitude` / `postalCode` / `city` /
`complex` / `imageSmall` / `status` / `onlyMiddleRent` /
`prioritizeKeyProfessions` / `suitedForHomeSharers` / `onlySixtyFivePlus`。

`status` 枚举同样写在 `app.js` 里：`1=nieuw`（可租）、`2=verhuurd`、
`3=verhuurd onder voorbehoud`、`4=gereserveerd`、`5=nieuw`。按 `status==1` 过滤：

| | 总量 | 可租 |
|---|---|---|
| Amsterdam | 95 | 27 |
| Rotterdam | 70 | 24 |
| Utrecht | 40 | 9 |
| Maastricht | 18 | 6 |
| Groningen | 27 | 6 |
| **Eindhoven** | 14 | **2** |
| 全国 | 524 | **113** |

可租的 113 套散在 30 个城市，€898–€3000（中位 €1655），45–160 m²。

### 否决理由：打分 + 同分抽签

`/toewijzing` 页面的原文：候选人按四条标准打分（收入是否达标、能否在可用时立即
入住、资料与文件是否齐全、家庭结构是否匹配，另有最低年龄 / keyworker 优先一类的
附加条件），满足全部得满分。

> 「Zijn er meerdere kandidaten met dezelfde score? Dan wordt door middel van
> **loting** bepaald wie de woning mag bezichtigen.」

同分者**抽签**决定谁能去看房。另注明「Vesteda werkt niet met wachtlijsten」——
没有等候名单，这一点比 DUWO/ROOM 好，但不改变结论：

**FlatRadar 的价值是「比别人早知道」，而抽签制下早三十秒不提高中签概率。**
这与否决 DUWO/ROOM 是同一个理由，只是机制不同——那边是排队，这边是抽签。

次要理由：每套房挂着「Kan ik deze woning huren?」的最低毛收入要求，中位 €1655
的房子按荷兰惯例要 3–3.5 倍毛收入，对学生基本是关死的门。

### 它是什么

自称 `woningbelegger en -verhuurder`——住宅投资机构兼房东，管养老金基金与保险公司
的钱，约 28,000 套自持，定位「middensegment」。是直接出租的房东（与 Xior / H2S
同一层，不是中介聚合），但不是学生公寓运营商。

**重新评估的触发条件**：分配规则从抽签改为先到先得。这不太可能——「eerlijk en
transparant」是他们主动宣传的卖点。

---

## §6c Leegstandbeheer / antikraak — **候选，未接**

2026-09-01 侦察。这一整类此前不在文档里。特点是**便宜**（€90–€774，中位约 €300）
且**先到先得**，学生付得起；代价是合同性质与常规租约不同，见下。

### Gapph（gapph.nl）——这一类里最大的

Villex 与 Interveste 已并入 Gapph，两家首页现在都指向 `gapph.nl/woonruimte`。

两条业务线：`antikraak`（空置看护）与 `tijdelijk-huren`（Leegstandwet 临时出租）。
服务端渲染，URL 形如 `/woonruimte/tijdelijk-huren/eindhoven/1869`。**无分页**
（`?page=2` / `?page=3` 返回同一批，已验）。

`robots.txt` 禁 `/beheer/` `/regiobeheer/` `/captcha/` `/cookies/` `/reageer/`
`/blog/` `/mijngapph/`，**房源页允许**。

2026-09-01 在架 46 条：`tijdelijk-huren` 35、`antikraak` 11。城市分布对本项目
有利——Eindhoven 各 1，另有 Veldhoven、Geldrop、Valkenswaard、Helmond，都在
Eindhoven 都市圈；Amsterdam 5、den-bosch 4、Dordrecht 3、Nijmegen / Tilburg /
Delft 各 1。站点把 **Studentenhuisvesting** 列为业务线之一。

**工程量中等偏上**：详情页的价格与面积写在散文里（「€750 voor een
2-kamerwoning, €800 voor een 3-kamerwoning en €850 voor een 4-kamerwoning」），
而且一条 listing 对应**整栋楼的多个户型**——接入前要先想清楚一条 listing 映射成
几条房源。这比 Magis 的模式化抽取难。

### Ad Hoc Beheer（adhocbeheer.nl）——接口最干净，地理不对

标准 WordPress REST，自定义 post type `units`：

```bash
curl -s 'https://adhocbeheer.nl/wp-json/wp/v2/units?per_page=100'
```

100 条（`x-wp-total` 用 `per_page=1` 复核过，就是 100，不是被 per_page 截断），
其中住宅 54 条（`Woonruimte` 48 / `Woning` 5 / `Antikraak wonen` 1），其余是
`Werkruimte` / `Kantoorruimte` / `Atelierruimte`。`acf` 字段为空，价格与面积同样
在正文散文里。

月费 €150–€774，中位 **€300**——价位对学生完全合适。

**问题是位置**：54 套散在 41 个城市，大学城只命中 4 套（Amsterdam / Rotterdam /
Maastricht / Den Haag 各 1），其余是 Vaals 4、Winterswijk 3、Hoogezand 3、
Emmen 2、Assen 2 这类地方。Eindhoven 0（Waalre、Maarheeze 勉强算周边）。

**单独接意义不大**，若做应与 Gapph 打包成「antikraak 类」一个来源。

### 接之前必须解决的一件事：合同性质

`antikraak` 签的是 **bruikleenovereenkomst（借用合同）**，不是租约：没有租客
保护，通常 28 天通知即须搬离，能否在 BRP 登记地址要逐条确认。对学生这是实质风险。

若接入，**必须在 UI 上把这类房源与正常租约区分标注**，不能混在同一个列表里。
`tijdelijk-huren`（Leegstandwet）那条线有租约，情况好一些，但仍是定期合同。

这是个产品决定而不是工程决定，未做即不应接入。

### 其余同类

- **Alvast**（alvast.nl）——WordPress，但 `projecten` 只有 5 条，是项目介绍不是
  房源；真实房源入口未定位。
- **Camelot Europe** ——已改名，`cameloteurope.com` 301 到 `mosaicworld.eu`，
  旧路径 404。Next.js + Storyblok，入口需重新定位。

---

## §7 综合建议

### 接入优先级（按投入产出比排序）

0. ~~**Plaza**~~ —— **已接入**（2026-09-02，影子期），见 §5b
   - 荷兰住宅 49 条，其中 **31 条是当场成交制**——本文档考察过的所有平台里最强的
     形态。另 18 条是「Snelle reageerder」，同样奖励速度
   - 一个匿名 POST 拿全站，字段结构化，工程量与 Magis 同级
   - ⚠️ 用户须付费注册（€27.50/年）才能应征。这不影响只读监控（房源匿名可见），
     但每条 listing 都要标注出来
1. **HousingAnywhere —— 覆盖范围最大**
   - 工程量低、合规边界清晰、用户群匹配，且数量可观（仅 Amsterdam 一城即 196 条）
   - 无 Cloudflare，普通 UA 即可；可直接复用现有 `scrapers/` 包的架构
2. ~~**SSH —— 补足城市覆盖**~~ —— **已否决**（2026-09-01）
   - 本条原写「全国 44 条覆盖 9 个城市，恰好补足 Holland2Stay 未覆盖的 Utrecht、
     Maastricht、Groningen」。**城市列表是错的**：SSHXL 实际覆盖 Groningen、
     Rotterdam、Tilburg、Utrecht、Zwolle（另有 Amersfoort），既无 Eindhoven
     也无 Amsterdam，更无 Maastricht。
   - API 已定位（见 §2），工程量不再是障碍；否决理由变成了覆盖面、长租不可见、
     短租按院校配额、以及没有真实数据可核对。
3. **Gapph / Ad Hoc（antikraak）—— 唯一在 Eindhoven 都市圈有货的候选**
   - 价位对学生合适（€150–€774，中位约 €300），先到先得，无反爬
   - 挡着的不是工程：`bruikleenovereenkomst` 不是租约，须先决定 UI 上怎么区分
     标注。这是产品决定，见 §6c
4. **Pararius —— 可以启动探测**
   - 此前判断为「需 Playwright，暂缓」，而浏览器传输层现已是现成基建
   - 2026-09-01 复测：`403` + `cf-mitigated: challenge`，与 Holland2Stay 同一套
     Cloudflare 挑战，CloakBrowser 可试
5. **Funda —— 不建议**
   - 2026-09-01 复测：`200` 但正文是 Akamai 的 captcha 页，比 Pararius 硬
6. **Vesteda / DUWO / Kamernet —— 放弃**（受限于分配机制与商业模式，而非技术问题）
   - Vesteda 是这三个里最值得说明的：技术上是全部候选中最省事的一个（一个 POST
     出全站），但**打分 + 同分抽签**让推送失去价值。见 §6b

> 接入新平台的实际成本远不止 scraper 本身：反爬机制会变化（见文首），且每增加一个
> 受 Cloudflare 保护的平台，就需多常驻一个浏览器（约 200–400MB）与一条专属线程。

### 关于「再找几个 Xior / H2S 那样的运营商」

在考察 OurCampus、Student Experience、Basecamp、Vesteda、Camelot、Yugo 与
The Social Hub 之后可以得出以下规律：**在荷兰的专业学生公寓领域，Xior 已是规模
最大者**（荷兰 30 栋，全欧 100 余栋）。其余自营运营商基本可归为三类：

| 类型 | 例子 | 不予接入的原因 |
|---|---|---|
| 规模过小 | OurCampus（1 栋） | 抓取成本固定，房源数量过少难以摊薄 |
| 排队制 | DUWO/ROOM、SSH&（sshn.nl）、SSHXL 长租、Basecamp 及社会住房整体 | 等待期以月至年计（SSHXL 实测 2–36 个月），即时推送没有意义 |
| 抽签制 | Vesteda | 同分抽签，早知道不提高中签概率——与排队制殊途同归，见 §6b |
| 客户端渲染 | The Social Hub、Camelot | 须逆向 API 或引入浏览器，成本接近接入一个新平台 |

**但这条规律只对「自营运营商」成立。** Plaza（§5b）不是运营商而是**平台**——多家
机构把房源挂在同一套 Zig/Hexia 系统上，因此单个接口就覆盖十个城市。往这个方向找
（找平台而不是找楼盘）比继续数运营商划算得多：同类的还有 Woningnet、Thuisvester
一系，尚未侦察。

> **2026-09-02 这张表改过两行，两处原因不同，都值得记下来。**
>
> **Student Experience 从「规模过小」整行删除。** 原文写「1 栋可订」，那是把
> `?los=shortstay` 过滤后的下拉框当成了全集，实际是 5 栋。真实情况是「5 栋但常态
> 0 库存」——这与「只有 1 栋」对「该不该接」的含义正好相反：前者是规模问题，接了
> 也没用；后者是频率问题，平时安静、放盘时才响，而那正是监控的意义。见 §5。
>
> **Vesteda 从「自研前端」移到新增的「抽签制」一行。** 原分类的技术判断是错的
> （实测是一个不需要认证的 POST），但结论恰好没变——换了一个完全不同、而且更硬的
> 理由。见 §6b。

因此继续沿「运营商」方向寻找的边际收益正在递减。**marketplace 方向
（HousingAnywhere）单城即有 207 条，是更划算的选择。**

尚未排除、值得投入约半天进行评估的有：**The Social Hub**——真学生盘，Eindhoven /
Delft / Maastricht / Groningen / Rotterdam 都有店，是唯一「真学生盘 + 有 Eindhoven
+ 规模够」的组合。但 2026-09-01 实测整站 Optimizely + React 客户端渲染，
`/eindhoven/student-stay/` 抓下来只有 19 KB 空壳，订房走 `liverates.hotelrez.co.uk`
这个酒店引擎，可用性接口未定位。

### 替代发现：可考虑加入候选

侦察过程中发现的其它可能值得评估的平台：

- **`hoppinger.com`** 是 ROOM.nl 及多个荷兰房产平台的承包商，其其余客户（不经
  DUWO 链路）可能采用同一套 Drupal 加 .NET 技术栈，开放程度或更高，值得进一步
  探索。
- **OurCampus.nl**（自 thisisourdomain 的链接中发现）——推测为 Greystar 旗下的另一个
  学生住房品牌。（该平台其后已接入，见 §4。）

---

## 附录：完整探测命令记录

用于复现上述结论的命令均在文档撰写过程中实际执行过，并存档于 git 提交历史中。
关键命令如下：

```bash
# Tech stack
curl -sL -A "..." https://platform/ -o /tmp/home.html

# Robots / sitemap
curl -sL https://platform/robots.txt
curl -sL https://platform/sitemap.xml

# API discovery（HousingAnywhere 用的 SPA bundle 解析）
grep -oE '"/api/[a-zA-Z0-9/_-]+"' bundle.js | sort -u
python3 -c "import re, json; ..."   # __PRELOADED_STATE__ extraction
```
