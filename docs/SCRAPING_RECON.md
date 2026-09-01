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
| 5 | **Student Experience** | ✅ 完全 | 无（自研前端） | 自有预订组件，可用性由 JS 拉取 | **仅 1 栋可订**（Minervahaven，2 个房型） | 低 | ❌ 暂不接入（可订范围过小） |
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

## §5 Student Experience (studentexperience.com) — **暂不做**

自营学生公寓运营商，形态上最接近 Xior 与 Holland2Stay（自有房源池、单元级粒度、
非排队制）。但其**线上可订的范围过小**。

### 荷兰楼盘（2026-08-03 实测）

| 楼盘 | 线上可订 |
|---|---|
| Amsterdam Minervahaven | ✅ 唯一可订 |
| Amsterdam Amstel | ❌ |
| Amsterdam NDSM | ❌ |
| Amsterdam Zuidas | ❌（站内公告 2026 年关闭） |
| Leiden | ❌ |
| Amstelveen Uilenstede | 在建 |

「线上可订」的判据是其自有预订组件 `/studios` 的 `locationId` 下拉框：其中属于荷兰
的仅有 `2 = Amsterdam Minervahaven`，另外两项为西班牙的 Granada 与 Madrid Pozuelo。
其余楼盘不提供线上预订路径。

Minervahaven 的两个房型：`14` Core Studio（€1.550/月起）、
`11` Signature Studio（€1.799/月起）。

### 两条路径均不可行

**RENTCafe 路径**：其后台确为 SecureRC（`studentexperience.securerc.co.uk`），但
仅 `amsterdam-minervahaven0` 这一 slug 存在（property_id 为 `186778`），
`amsterdam-amstel0` 与 `amsterdam-ndsm0` 在 RentCafe 上均返回 404。且该
`floorplans.aspx` 的 76KB 内容中 **不含任何 floorplan tile**（`subPointerId`、
`myFloorPlanId`、`FloorPlanContainer` 的数量均为 0）——它并不走 online-leasing 的
floorplan 流程，`OurDomainScraper` 的实现无法套用。

**自有组件路径**：`/studios?los=shortstay&locationId=2&studioTypeId=14` 为服务端
渲染，参数集包括 `los`、`locationId`、`studioTypeId` 与 `academicTermId`。但
**承载可用性信息的 `academicTermId` 下拉框始终为空**，即便选定楼盘与房型后亦不
填充——其内容由 JS 异步拉取，对应的 XHR 端点未能定位。要取得真实可用性，须逆向
其 JS 或引入浏览器。

在 `los=longstay` 模式下，楼盘与房型的下拉框均不存在。

### 结论

为 1 栋楼、2 个房型而逆向其 JS 或增加一个常驻浏览器，投入产出不成比例。

**重新评估的触发条件**：该平台为 Leiden、NDSM 或 Amstel 开放线上预订（表现为
`locationId` 下拉框中出现新选项）。届时可订范围将扩大至 4–5 栋楼，值得重新评估。

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

---

## §7 综合建议

### 接入优先级（按投入产出比排序）

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
3. **Pararius / Funda —— 可以启动探测**
   - 此前判断为「需 Playwright，暂缓」，而浏览器传输层现已是现成基建
   - 但其反爬可能严于 Holland2Stay（例如 DataDome 一类），须先实测
4. **DUWO / Kamernet —— 放弃**（受限于合规与商业模式，而非技术问题）

> 接入新平台的实际成本远不止 scraper 本身：反爬机制会变化（见文首），且每增加一个
> 受 Cloudflare 保护的平台，就需多常驻一个浏览器（约 200–400MB）与一条专属线程。

### 关于「再找几个 Xior / H2S 那样的运营商」

在考察 OurCampus、Student Experience、Basecamp、Vesteda、Camelot、Yugo 与
The Social Hub 之后可以得出以下规律：**在荷兰的专业学生公寓领域，Xior 已是规模
最大者**（荷兰 30 栋，全欧 100 余栋）。其余自营运营商基本可归为三类：

| 类型 | 例子 | 不予接入的原因 |
|---|---|---|
| 规模过小 | OurCampus（1 栋）、Student Experience（1 栋可订） | 抓取成本固定，房源数量过少难以摊薄 |
| 排队制 | DUWO/ROOM、SSH&（sshn.nl）、SSHXL 长租、Basecamp 及社会住房整体 | 等待期以月至年计（SSHXL 实测 2–36 个月），即时推送没有意义 |
| 自研前端 | Vesteda、Camelot | 房源由客户端渲染，须逆向 API 或引入浏览器，成本接近接入一个新平台 |

因此继续沿「运营商」方向寻找的边际收益正在递减。**marketplace 方向
（HousingAnywhere）单城即有 207 条，是更划算的选择。**

尚未排除、各值得投入约半天进行评估的有：**Vesteda**（大型机构房东，自有门户，
房源由客户端渲染）与 **Camelot Europe**（Next.js 加 Storyblok，首页的
`__NEXT_DATA__` 仅含 CMS 内容，房源位于搜索页，尚未深入分析）。

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
