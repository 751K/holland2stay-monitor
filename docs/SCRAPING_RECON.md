# 候选平台侦察

**这份文档只管「还没接入的平台值不值得做」。** 已接入的三个平台的现状不在这里：
- Holland2Stay / Xior / OurDomain 的传输方式与反爬对抗 → [ARCHITECTURE.md §3](ARCHITECTURE.md)
- Xior 端点契约与自动预订 → [XIOR.md](XIOR.md)
- OurDomain 端点契约与自动预订 → [OURDOMAIN.md](OURDOMAIN.md)

结论来自真实 HTTP 探测。反爬现状会随上游变化——**下面每条评估都只代表探测当时**，
真要接入前必须重测一遍。已接入的三个平台里有两个的反爬在半年内变过（H2S 迁域名 +
上 Turnstile，Xior 端点上托管挑战），这不是小概率事件。

---

## 速览矩阵

> Xior 和 OurDomain 已接入，不在此表——见上面的文档指针。

| # | 平台 | 公开可读 | 反爬 | 数据形态 | 量级 | ToS 风险 | 结论 |
|---|---|---|---|---|---|---|---|
| 2 | **HousingAnywhere** | ✅ 完全 | 无 | 页面内嵌结构化 JSON（`__staticRouterHydrationData`） | 207 条 Amsterdam，23 条/页 | 低（`/api/*` 禁；分页 query 在灰区） | 🟢🟢🟢 **下一个做它** |
| 3 | **SSH (sshxl.nl)** | ✅ 但 SPA | 无 | Angular SPA + sitemap-offers.xml | 44 条全国 | 低 | 🟢 推荐（需挖 API） |
| 4 | **OurCampus (ourcampus.nl)** | ✅ 完全 | SecureRC CF → **curl_cffi 首个指纹即过** | 与 OurDomain 同栈（RENTCafe HTML） | **1 栋楼** | 低 | ❌ 不做（等待期 16–18 个月） |
| 5 | **Student Experience** | ✅ 完全 | 无（自研前端） | 自家预订组件，可用性由 JS 拉取 | **1 栋可订**（Minervahaven，2 个房型） | 低 | ❌ 暂不做（可订面太小） |
| 6 | Pararius | ❌ | **Cloudflare JS challenge** | — | — | — | 🟡 现可用 CloakBrowser（H2S 同方案） |
| 7 | DUWO/ROOM | ❌ | 无（但 **auth-wall + paid registration**） | API 仅登录后可见 | ? | **高**（登录后内容转发）| ❌ 不建议 |
| 8 | Kamernet | — | paid model | — | — | 高 | ❌ |

---

## §1 HousingAnywhere — **强烈推荐**

### Endpoint
```
GET https://housinganywhere.com/s/{City}--{Country}
e.g. https://housinganywhere.com/s/Amsterdam--Netherlands
```

### 关键发现（2026-08-03 复测）

- HTTP 200，507 KB HTML，**plain `Mozilla/5.0` UA 即可**，无 Cloudflare 挑战
- 房源是**页面内嵌的结构化 JSON**，不需要解析 HTML 卡片
- 每页 23 条，Amsterdam 共 207 条 → 约 9 页

### 数据形态

房源数组在：

```
window.__staticRouterHydrationData = JSON.parse("…")
  → .loaderData.<routeId>.listings          # 23 条
```

注意这是**双层编码**：外层是 JS 字符串字面量，内层才是 JSON，要解两次。
同一 blob 里还有 `undefined` 字面量（JS 不是 JSON），直接 `json.loads` 会炸，
需要先把 `:undefined` 替换成 `:null`。

单条 listing 的字段：

```
id, internalID, recordType, advertiserId, isPartner, depositPolicy,
price (分), priceEUR, currency, street, city, country, countryCode,
_geoloc {lat, lng},  ← 自带经纬度
isNew, propertyType, previewImage, photos, photoCount,
minimalRentalPeriod, maximumStay
```

`_geoloc` 是最大的额外收益：现有三个源都要走 geocode pipeline 才能上地图，
这个直接带坐标。

> **旧侦察记录的三条解析路径已经失效**，不要照着写代码：
> - JSON-LD（`<script type="application/ld+json">`）现在只剩聚合值
>   （`offerCount` / `lowPrice` / `highPrice`），**不含个体房源**
> - `window.__PRELOADED_STATE__` 现在只剩搜索元数据（价格分布等），**不含房源**
> - `data-test-locator="ListingCard/*"` 锚点仍在，但既然有干净 JSON 就没必要解 HTML
>
> 这正是页首那条判断的实例：**评估只代表探测当时**。

### robots.txt

```
User-agent: *
Disallow: /api/*       ← 不要直接打他们的 API
Disallow: /my/*        ← 用户私人区
Disallow: /admin
...

sitemap: https://housinganywhere.com/sitemap.xml
```

`/api/*` 明确禁止——所以只能走页面内嵌 JSON，不能直接调他们的 API。这正好是
我们要的做法。

**但有一条灰区：`Disallow: /*/s/*?*` 禁的是带 query string 的搜索页，而分页正是
`?page=2`。** 这条模式要求 `/s/` 前面还有一段路径，我们用的 `/s/Amsterdam--Netherlands?page=2`
在根路径下，严格讲不匹配；但不同 robots 解析器对 `*` 能否匹配空串处理不一致。

保守做法：只抓第一页（23 条最新房源，对「新房源通知」已经够用），不翻页。
真要翻页，先确认他们的 ToS。

### 工程评估

| 项 | 评分 |
|---|---|
| 公开数据 | ⭐⭐⭐⭐⭐ 207 条 Amsterdam，覆盖荷兰全境 |
| 抓取难度 | ⭐⭐⭐⭐⭐ Plain UA + 内嵌 JSON，无任何技术阻碍 |
| 数据稳定性 | ⭐⭐⭐ 内嵌 blob 的路径两个月内已变过一次；`data-test-locator` 锚点可作兜底 |
| 合规风险 | ⭐⭐⭐⭐ listing 页可抓；分页 query 在 robots 灰区 |
| 用户重叠度 | ⭐⭐⭐⭐⭐ 国际学生 + young pro = FlatRadar 核心用户群 |

**推荐工程量：1.5–2 周**（HTML 解析 + city 列表配置 + 入库适配）。

---

## §2 SSH (sshxl.nl) — **可做，需挖 SPA bundle**

### Endpoint
```
GET https://www.sshxl.nl/en/rental-offer/{numeric_id}-
GET https://www.sshxl.nl/sitemap-offers.xml   ← listings 全量索引
```

### 关键发现

- **`sitemap-offers.xml` 直接列出 44 条当前活跃 offer URLs**
- 每条 URL 是 `/en/rental-offer/{numeric_id}-` 这样的稳定 ID
- 但**详情页是 SPA**（Angular 风格）—— title 只有 `<title>View</title>`，HTML 不含数据
- 真实数据需要从 `/api/...` 拿，但我没在初步探测里找到端点

### Backend 指纹

```
server: Kestrel               ← .NET 5+
set-cookie: .AspNetCore.Antiforgery  ← .NET ASP.NET Core CSRF token
robots.txt: Disallow /hangfire/   ← .NET 后台任务系统
```

### robots.txt

```
User-agent: *
Disallow: /hangfire/, /admin/, /my-ssh/, /mijn-ssh/, /styleguide/
Sitemap: https://www.sshxl.nl/sitemap-offers.xml ← 公开
```

**listings 不在 Disallow 列表，offer 抓取合规**。

### 工程评估

- 量小（44 条），但全国分布（Utrecht / Eindhoven / Amsterdam / Maastricht 等 9 城）
- **需要先反编译/解析他们的 SPA bundle 找出 listing API endpoint**——这是 P1 的真实工作量
- 备选方案：用 Playwright 抓 SPA 渲染后的 HTML（简单但慢 + 资源贵）
- 数据可能依赖 antiforgery cookie（一次 GET 主页拿 cookie，后续带 cookie 调 API）

**推荐工程量：2–3 周**（SPA bundle 分析 + API 端点测试 + 入库适配）。SPA bundle 分析有不确定性，可能踩坑。

---

## §3 Pararius — **可探测，基建已到位**

### 直接测试结果

```
GET https://www.pararius.com/apartments/amsterdam
→ HTTP 403 + cf-mitigated: challenge + "Just a moment..."
```

普通 `Mozilla/5.0` UA 触发 Cloudflare 5 秒挑战。`curl_cffi` 能否过尚未实测——OurDomain 的 SecureRC 同样被判成「CF hard block」但 curl_cffi 可过，所以值得先试轻方案。过不去就走浏览器传输层。

### robots.txt（允许浏览）

```
User-agent: *
Disallow: /contact/*, /report-*, /account/*, /checkout/*, /*/Kamer-te-huur/*
```

robots.txt 友好，但 Cloudflare WAF 不认 robots——它认请求指纹。

### 工程评估

浏览器传输层已经是现成基建（`BrowserFetcher` + 一个新的 `SiteProfile` 即可），所以「需要浏览器」不再是阻塞理由。真正的未知是 Pararius 的反爬强度——可能比 H2S 更严（DataDome 之类），必须实测。成本上要记住：多一个 Cloudflare 平台 = 多一个常驻浏览器（~200–400MB）+ 一条专属线程。

---

## §4 OurCampus (ourcampus.nl) — **不做**

Greystar 的另一个学生住房品牌，与 OurDomain 同属一家。技术上几乎是白送，但产品上没价值。

### 技术面（2026-08-03 实测，全部通过）

| 项 | 值 |
|---|---|
| 栈 | Webflow 前台 + RENTCafe/SecureRC 后台，**与 OurDomain 完全同栈** |
| RENTCafe host | `new-ourcampus-amsterdam-diemen-rentcafewebsiteuk.securerc.co.uk` |
| slug / property_id | `new-ourcampus-amsterdam-diemen` / `186609` |
| 反爬 | plain curl → 403 + 挑战页；**curl_cffi `chrome136` 首个指纹即 200** |
| 已解析 | 3 个 floorplan：`1113259` Standard+ Studio 1P / `1112904` Furnished Student 1P / `1112905` Furnished Student 2P |

### 为什么不做

**1. 只有一栋楼。** `/en/apartments` 全站仅 Amsterdam Diemen，地址 Dalsteindreef 6002——
与 OurDomain South-East（Dalsteindreef 20-40）同街隔壁。接进来只多一栋楼。

**2. 等待期 16–18 个月（官网自述）。** 这是排队制不是先到先得，
「房源出现即秒推」的核心价值不成立。与 DUWO/ROOM 是同一类否决理由，
只不过那次是合规问题，这次是产品问题。

**3. 不能直接复用 `OurDomainScraper`。** 它的单元查询是
**POST + `floorPlans[]` 表单体**（页面 jQuery `.load(url, {floorPlans: names})`），
OurDomain 是 GET + query string。同栈不等于同接口。

### 一条未验证的事

实测时该楼**零可订单元**（floorplans.aspx 上 0 个 apply 按钮、6 个 Get Notified；
页面里唯一的 `applyButton` 字样在一行被注释掉的 JS 里）。所以
`contentclass=availableunits` 返回的是 floorplan 网格而不是单元表，
**它的单元表 HTML 结构未经验证**。

判定「真没房」而不是「解析失败」的依据是对照实验：同一份代码、同一个指纹打
OurDomain Diemen，`unitrow=2` / `1`，正常解析。没有这个对照就会掉进
「没拿到数据 ≠ 确认没有数据」的坑（见 [ARCHITECTURE.md §5.10](ARCHITECTURE.md)）。

真要接入，必须等它有房时重新验证一次单元表结构。

---

## §5 Student Experience (studentexperience.com) — **暂不做**

自营学生公寓运营商，形态上最接近 Xior / H2S（自有房源池、单元级、非排队制）。
但**线上可订的面太小**。

### 荷兰楼盘（2026-08-03 实测）

| 楼盘 | 线上可订 |
|---|---|
| Amsterdam Minervahaven | ✅ 唯一一个 |
| Amsterdam Amstel | ❌ |
| Amsterdam NDSM | ❌ |
| Amsterdam Zuidas | ❌（站内公告 2026 年关闭） |
| Leiden | ❌ |
| Amstelveen Uilenstede | 在建 |

「线上可订」的判据是它自家预订组件 `/studios` 的 `locationId` 下拉框——
里面 NL 只有 `2 = Amsterdam Minervahaven`（另外两个是西班牙的 Granada、
Madrid Pozuelo）。其余楼盘没有线上预订路径。

Minervahaven 的两个房型：`14` Core Studio（€1.550/月起）、
`11` Signature Studio（€1.799/月起）。

### 两条路都不通

**RENTCafe 路径**：后台确实是 SecureRC（`studentexperience.securerc.co.uk`），
但只有 `amsterdam-minervahaven0` 这个 slug 存在（property_id `186778`），
`amsterdam-amstel0` / `amsterdam-ndsm0` 都是 RentCafe 404。而且那个
`floorplans.aspx` 76KB 里 **0 个 floorplan tile**（`subPointerId` /
`myFloorPlanId` / `FloorPlanContainer` 全为 0）——它不走 online-leasing
的 floorplan 流程，`OurDomainScraper` 那套用不上。

**自家组件路径**：`/studios?los=shortstay&locationId=2&studioTypeId=14`
是服务端渲染的，参数集为 `los` / `locationId` / `studioTypeId` /
`academicTermId`。但**承载可用性的 `academicTermId` 下拉框始终是空的**，
选完楼盘和房型也不填充——由 JS 异步拉取，对应的 XHR 端点我没找到。
要拿到真实可用性得反 JS 或上浏览器。

`los=longstay` 模式下连楼盘和房型的下拉框都没有。

### 结论

为 1 栋楼 × 2 个房型反 JS 或加一个常驻浏览器，不划算。

**重新评估的触发条件**：它把 Leiden / NDSM / Amstel 开放线上预订
（`locationId` 下拉框里出现新选项）。那时可订面变成 4–5 栋，值得再看。

---

## §6 DUWO/ROOM (room.nl) — **不建议**

### 端点
- `GET /api/v1/PreferredCities` → 200 ✅（9 城市 + UUID）
- `GET /api/v1/product-search?pageIndex=0&pageSize=20&...` → **404 anonymous**

### 业务模型
- ROOM.nl 是 DUWO + 其他学生住房组织的统一搜索平台
- **用户必须先注册 + 付 ~€30/年 waiting list 会员费** 才能看 listings
- API 设计本身就是 `credentials: "include"`——必须登录态 cookie
- 这是 ROOM 商业模式核心：卖 waiting list 服务

### 工程上能做 vs 合规上该做

- 技术上能做：拿一个真账号，scraper 登录维持 session，调 `product-search`
- 合规上不该做：DUWO ToS 明确禁止"将通过本服务获得的信息再分发"。FlatRadar 把 DUWO 数据推送给非账户持有者 = 高风险违 ToS
- 单点故障：账号被锁 = 所有用户的 DUWO 监控停摆
- 学生身份验证：DUWO 注册要 student ID + paid status，不是随便能搞

**推荐：不做**。学生想监控 DUWO 应该自己注册 + 用 ROOM 自带的 mail alert 功能。

---

## §7 综合建议

### 下一个做谁（按投入产出比排序）

1. **HousingAnywhere — 最大覆盖**
   - 工程量低、合规清楚、用户群匹配、量大（196 条仅 Amsterdam 一城）
   - 无 Cloudflare，plain UA 即可；直接复用现有 `scrapers/` 包架构
2. **SSH — 填空城市**
   - 全国 44 条覆盖 9 城，正好补 H2S 没覆盖的 Utrecht / Maastricht / Groningen
   - 工程量略大，需要先挖 Angular SPA bundle 找 API
3. **Pararius / Funda — 可以启动探测**
   - 之前判断「需 Playwright，推迟」，现在浏览器传输层已经是现成基建
   - 但它们的反爬可能比 H2S 更严（DataDome 之类），要先实测
4. **DUWO / Kamernet — 放弃**（合规 / 商业模式不允许，不是技术问题）

> 接入新平台的实际成本远不止 scraper 本身：反爬会变（见页首），每加一个
> Cloudflare 保护的平台就多常驻一个浏览器（~200–400MB）和一条专属线程。

### 关于「再找几个 Xior / H2S 那样的运营商」

扫过 OurCampus、Student Experience、Basecamp、Vesteda、Camelot、Yugo、
The Social Hub 之后的规律：**荷兰专业学生公寓这块，Xior 已经是最大的那个**
（NL 30 栋 / 全欧 100+）。剩下的自营运营商基本落进三类：

| 类型 | 例子 | 为什么不做 |
|---|---|---|
| 规模太小 | OurCampus（1 栋）、Student Experience（1 栋可订） | 抓取成本固定，房源太少摊不平 |
| 排队制 | DUWO/ROOM、Basecamp、社会住房那一整类 | 等待期以年计，「秒推」没有意义 |
| 自研前端 | Vesteda、Camelot | 房源客户端渲染，要挖 API 或上浏览器，成本≈一个新平台 |

所以继续往「运营商」方向找的边际收益在递减。**marketplace 那条线
（HousingAnywhere）单城 207 条，是更划算的方向。**

尚未排除、值得各花半天的：**Vesteda**（大型机构房东，自有门户，
房源客户端渲染）和 **Camelot Europe**（Next.js + Storyblok，首页
`__NEXT_DATA__` 只有 CMS 内容，房源在搜索页，没深挖）。

### 替代发现：可考虑加入候选

侦察过程中发现的其他可能值得做的平台：
- **`hoppinger.com`** 是 ROOM.nl / 多个 Dutch 房产平台的承包商，他们的其他客户（非 DUWO 链路）可能用同一个 Drupal + .NET stack，开放程度更高。可探索。
- **OurCampus.nl**（thisisourdomain 链接里出现）—— 可能是 Greystar 的另一个学生住房品牌

---

## 附录：完整探测命令记录

复现这些结论的命令都在文档生成过程中真实运行过，存档于 git commit 历史。关键命令：

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
