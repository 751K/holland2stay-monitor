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
| 2 | **HousingAnywhere** | ✅ 完全 | 无 | JSON-LD + `__PRELOADED_STATE__` + 全 data-test-locator 标签 | 196 条 Amsterdam | 低（robots 禁 `/api/*`，但 HTML OK） | 🟢🟢🟢 **强烈推荐** |
| 3 | **SSH (sshxl.nl)** | ✅ 但 SPA | 无 | Angular SPA + sitemap-offers.xml | 44 条全国 | 低 | 🟢 推荐（需挖 API） |
| 4 | Pararius | ❌ | **Cloudflare JS challenge** | — | — | — | 🟡 现可用 CloakBrowser（H2S 同方案） |
| 6 | DUWO/ROOM | ❌ | 无（但 **auth-wall + paid registration**） | API 仅登录后可见 | ? | **高**（登录后内容转发）| ❌ 不建议 |
| 7 | Kamernet | — | paid model | — | — | 高 | ❌ |

---

## §1 HousingAnywhere — **强烈推荐**

### Endpoint
```
GET https://housinganywhere.com/s/{City}--{Country}
e.g. https://housinganywhere.com/s/Amsterdam--Netherlands
```

### 关键发现

- **HTTP 200 with 660 KB HTML，plain `Mozilla/5.0` UA 即可**
- 无 Cloudflare challenge / WAF 阻拦
- 设置 `ha_anonymous_id` cookie（匿名用户标识，跟踪 + 不阻断）

### 数据形态（3 种冗余结构，任选其一解析）

#### A. JSON-LD schema.org（最稳）

页面 `<script type="application/ld+json">` 里有两块：

```json
{
  "@context": "http://schema.org",
  "@type": ["Apartment", "Product"],
  "name": "Accommodation for rent in Amsterdam, Netherlands",
  "offers": {
    "@type": "AggregateOffer",
    "priceCurrency": "EUR",
    "offerCount": 196,
    "highPrice": 5928,
    "lowPrice": 600
  },
  "hasMap": "http://www.google.com/maps/place/52.37403,4.88969"
}
```

只给汇总（offerCount / highPrice / lowPrice），不给个体——但这是个信号：网站愿意公开聚合数据。

#### B. `window.__PRELOADED_STATE__`（最全）

页面里塞了 86 KB 的 Redux/Apollo state，含完整 listing 数据。Top key `hermes` 是他们的内部状态命名空间。Parse 出来后能拿到每条 listing 的所有字段。

#### C. HTML data-test-locator（最直接）

每条 listing 用 React 测试钩子打了标签，**直接当 scrape 锚点用**：

```
ListingCard/Title         → 标题
ListingCard/Price         → 价格
ListingCard/Availability  → 入住时间
ListingCard/AttributesSize       → 面积
ListingCard/AttributesFacilities → 设施列表
ListingCard/AttributesPlaces     → 容纳人数
ListingCard/Highlight/NoDeposit  → 标签：无押金
ListingCard/Highlight/FlexibleCancellation
ListingCard/Highlight/Confirmed
ListingCard/BadgeMultiplePlaces  → 多套房标识
ListingCard/Anchor       → 详情页链接
```

外加 `<meta itemProp="price" content="1720">` 这种 schema.org microdata。

### robots.txt

```
User-agent: *
Disallow: /api/*       ← 不要直接打他们的 API
Disallow: /my/*        ← 用户私人区
Disallow: /admin
...

sitemap: https://housinganywhere.com/sitemap.xml
```

**Allow 浏览 listing 列表页 + 详情页**。只禁 `/api/*` 和用户私人区。**完全合规**。

### 工程评估

| 项 | 评分 |
|---|---|
| 公开数据 | ⭐⭐⭐⭐⭐ 196 条 Amsterdam，覆盖荷兰全境 |
| 抓取难度 | ⭐⭐⭐⭐⭐ Plain UA + HTML 解析，无任何技术阻碍 |
| 数据稳定性 | ⭐⭐⭐⭐ JSON-LD + data-test-locator 双冗余，schema 变化风险低 |
| 合规风险 | ⭐⭐⭐⭐⭐ Allow listing 页，仅禁 API + 私人区 |
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

## §4 DUWO/ROOM (room.nl) — **不建议**

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

## §5 综合建议

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
