# 多平台抓取侦察报告

P1 候选平台技术可行性 + 合规性汇总。

> **2026-06-13 更新**：H2S 已将 GraphQL API 从 `api.holland2stay.com/graphql` 迁移至 `www.holland2stay.com/api/graphql`（与主站同域），并对旧子域名启用了 Cloudflare Turnstile 保护。curl_cffi TLS impersonation 已无法通过。已迁移至 **CloakBrowser**（patched Chromium，58 C++ 源码级反指纹 patch）——浏览器自动执行 Turnstile JS challenge，通过后通过 `page.evaluate(fetch)` 调用同域 GraphQL API。详见 scaper / booker 源码。

所有结论来自真实 HTTP 探测，除非单独标注更新日期。

---

## 速览矩阵

| # | 平台 | 公开可读 | 反爬 | 数据形态 | 量级 | ToS 风险 | 推荐 |
|---|---|---|---|---|---|---|---|
| 1 | **Xior** | ✅ 完全 | **Turnstile 不验证** | AJAX JSON（`admin-ajax.php?action=yardi_room_availability`） | 57 栋 NL+BE（100+ 全欧） | 低 | 🟢🟢🟢 **最推荐** |
| 2 | **HousingAnywhere** | ✅ 完全 | 无 | JSON-LD + `__PRELOADED_STATE__` + 全 data-test-locator 标签 | 196 条 Amsterdam | 低（robots 禁 `/api/*`，但 HTML OK） | 🟢🟢🟢 **强烈推荐** |
| 3 | **SSH (sshxl.nl)** | ✅ 但 SPA | 无 | Angular SPA + sitemap-offers.xml | 44 条全国 | 低 | 🟢 推荐（需挖 API） |
| 4 | Pararius | ❌ | **Cloudflare JS challenge** | — | — | — | 🟡 现可用 CloakBrowser（H2S 同方案） |
| 5 | **OurDomain (thisisourdomain)** | ✅ 完全 | SecureRC CF challenge → **curl_cffi 可过** | Unit 级 HTML table（server-rendered，data-selenium-id 锚点） | 8 单元（1 栋） | 低 | 🟢 已接入 |
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

## §3 Pararius — **需 Playwright，推迟**

### 直接测试结果

```
GET https://www.pararius.com/apartments/amsterdam
→ HTTP 403 + cf-mitigated: challenge + "Just a moment..."
```

普通 `Mozilla/5.0` UA 触发 Cloudflare 5 秒挑战。鉴于 OurDomain 的 SecureRC 也被判定为"CF hard block"但实测 `curl_cffi` 可过，Pararius 应重新用 `curl_cffi chrome131` 测试——有可能同样通过。**待重新探测后更新本条。**

### robots.txt（允许浏览）

```
User-agent: *
Disallow: /contact/*, /report-*, /account/*, /checkout/*, /*/Kamer-te-huur/*
```

robots.txt 友好，但 Cloudflare WAF 不认 robots——它认请求指纹。

### 工程评估（2026-06-13 更新）

现可用 **CloakBrowser**（H2S scraping/booker 已在用）：patched Chromium 自动执行 CF JS challenge，通过后拿 cf_clearance 即可调 API。之前评估"需 Playwright"，现在基建已到位，可行性大幅提升。但 Pararius 的反爬可能比 H2S 更严（DataDome 等），需实际探测验证。

---

## §4 OurDomain (thisisourdomain.nl) — **推荐 P1 接入**

> **2026-05-22 更新**：此前判断"低价值，推迟"，实测发现 `curl_cffi` 可直接通过 SecureRC 的 Cloudflare，无需 Playwright。详细设计见 [`OURDOMAIN.md`](OURDOMAIN.md)。

### Endpoint

两阶段抓取：

```
# 阶段 1：获取 FP ID 列表
GET .../floorplans.aspx

# 阶段 2：获取单元（unit-level，8 个物理单元）
GET .../rcLoadContent.ashx?contentclass=availableunits&floorPlans={fp_id}&MoveInDate={YYYY-MM-DD}&myolePropertyID=184283
```

详细设计见 [`OURDOMAIN.md`](OURDOMAIN.md)。

### 关键发现

- **`curl_cffi chrome131` → HTTP 200**，全 GET 请求（POST 触发 CF 403）
- **零新依赖**：与 H2S 共享完全相同的 HTTP 策略（curl_cffi + impersonate）
- **单元级监控**：两个阶段提取—floorplan 发现 FP ID → `availableunits` 获取 8 个物理单元（#6045, #6171 等）
- 每个单元含：单元号、面积（单值 m²）、月租（单值 €）、押金、楼层、朝向、可入住日期
- 单元在所有 8 个 FP 类型下共享（同一物理房间可租不同合同类型），需按 `unit_id` 去重
- Diemen 和 South-East 是同一个 RENTCafe property（184283），数据一致
- 单元级租金是**单值**（`€1.587`），不是 FP 级的范围（`€1,797–€1,900`）——过滤直接可用

### 数据形态（单元级）

```html
<tr id="unitrow_307195">
  <th data-selenium-id="Apt1"  id="307195">#6045</th>           ← 单元号 + 数字 ID
  <td data-selenium-id="SqFt1">22</td>                            ← m² 单值
  <td data-selenium-id="Rent1">€ 1.587</td>                      ← 月租单值
  <td data-selenium-id="Deposit1">€ 2.622</td>                   ← 押金
  <td data-selenium-id="Amenity1">                                ← 详情浮层
    <label>Ground Floor</label>
    <label>Courtyard View</label>
  </td>
  <td data-selenium-id="AvailDate1">
    <span class="text-success">Available</span>                   ← 可预订
    <span class="text-warning">Wait List</span>                   ← 等位
  </td>
  <td data-selenium-id="Action1">
    <input value="Book now" onclick="ApplyNowClick('307195',...)" />
  </td>
</tr>
```

### 当前数据（2026-05-22）

8 个物理单元（22–28 m², €1,563–€1,662），当前全部 "Available"。Diemen 和 South-East 是同一个 RENTCafe property。详见 [`OURDOMAIN.md`](OURDOMAIN.md) §3。

### 与 H2S 的关键差异

| 差异 | 影响 |
|---|---|
| HTML table ×2（FP 列表 + 单元列表）而非 GraphQL JSON | 两阶段抓取，9 req/轮，按 unit_id 去重 |
| 状态翻转模型（Available ↔ Wait List） | 只产生 `status_changes`，首次 8 个 `new_listings` |
| 租金是单值 `"€ 1.587"` | `parse_float` 直接可用，比 FP 级范围更精确 |
| 单元含楼层/朝向 | 可过滤 Ground Floor vs Floor 1-4，Courtyard View |
| 单元在所有 FP 下共享 | 去重后 8 个 listing，非 64 个 |
| ID 需 `od_` 前缀 | P0 推迟的 ID 前缀化在 P1 统一做 |

### 工程评估

| 项 | 评分 |
|---|---|
| 公开数据 | ⭐⭐⭐ 8 个物理单元，覆盖 Amsterdam Diemen |
| 抓取难度 | ⭐⭐⭐⭐ curl_cffi + HTML 正则，两阶段抓取 + 去重，零新依赖 |
| 数据稳定性 | ⭐⭐⭐⭐ Yardi 是全国性 PMS，`data-selenium-id` 是测试锚点 |
| 合规风险 | ⭐⭐⭐⭐⭐ 公开页面，无 robots.txt 禁爬条款 |
| 用户重叠度 | ⭐⭐⭐ 学生 + young pro，与 H2S 用户群互补（H2S 不覆盖 Diemen） |
| 过滤可用性 | ⭐⭐⭐⭐ 单值租金 + 面积 + 楼层 + 朝向——比 FP 级更适合过滤 |

**推荐工程量：8–10 小时**（两阶段 scraper + 去重 + 楼层解析 + 接线）。详细设计见 [`OURDOMAIN.md`](OURDOMAIN.md)。

---

## §5 Xior (xiorstudenthousing.eu) — **最推荐**

> 2026-05-22 侦察。Xior 使用 WordPress + Yardi 后端，房间数据通过 AJAX JSON 返回，**Turnstile 不验证服务端**——纯 HTTP、零反爬。详细设计见 [`XIOR.md`](XIOR.md)。

### Endpoint

```
POST https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php
  action=yardi_room_availability
  property_page_id=1126
  room_type_id=33944
  semester_id=3281
```

### 关键发现

- **纯 JSON 响应**：不需要 HTML 解析，不需要 GraphQL，返回 `{"success":true, "data":{"units":[...], "selected_room":{...}}}`。
- **Turnstile 假防线**：不带 token / 空 token / dummy token 均返回相同完整数据。服务端不验证。
- **单元级数据**：`apartmentId`、`apartmentName`（房号）、`sqm`（精确面积）、`minimumRent`/`maximumRent`、`deposit`、`availableDate`、`unitStatus`、`applyOnlineURL`（直达预订链接）。
- **覆盖欧洲 100+ 栋楼**：NL 25 + BE 32 + DE/DK/ES/PT/PL/SE。
- **建筑发现**：城市页 HTML 含所有建筑链接；每栋建筑页 `window.xior` 含 `wp_building_id`、`building_code`、`booking_engine: "yardi"`。
- **三阶段流程**：发现建筑 → 提取房型 ID（Yardi modal 的 `data-room-id`）→ POST AJAX 获取单元。

### 数据形态

```json
{
  "apartmentId": 402419,
  "apartmentName": "M1.30.53",
  "sqm": 19,
  "minimumRent": 417, "maximumRent": 580,
  "deposit": 0,
  "availableDate": "01/07/2026",
  "unitStatus": "Notice Unrented",
  "applyOnlineURL": "https://brouwersweg-xiorstudenthousing.securerc.co.uk/..."
}
```

### 工程评估

| 项 | 评分 |
|---|---|
| 公开数据 | ⭐⭐⭐⭐⭐ NL 25 栋 + BE 32 栋，全欧 100+ |
| 抓取难度 | ⭐⭐⭐⭐⭐ 纯 JSON，无 Cloudflare，无 reCAPTCHA |
| 数据稳定性 | ⭐⭐⭐⭐ AJAX 端点依赖 WordPress + Yardi 插件，变更频率低 |
| 合规风险 | ⭐⭐⭐⭐⭐ 公开 AJAX，无 robots 限制 |
| 用户重叠度 | ⭐⭐⭐⭐ 学生住房（与 H2S 用户群互补） |

**推荐工程量：4–5 小时**（纯 JSON scraper + 楼数据录入）。三个平台里最快集成。

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

### P1 推荐路径（按投入产出比排序）

1. **Xior — 最快赢单（4–5 小时）**
   - 纯 JSON AJAX，零反爬，零 Cloudflare
   - NL+BE 57 栋楼，全欧覆盖。三个平台里最容易接
   - 详细设计：[`XIOR.md`](XIOR.md)
2. **OurDomain — 已接入（✅ 完成）**
   - curl_cffi + HTML 表解析，8 个物理单元
   - 已完成 `Listing.id` 前缀化迁移
   - 详细设计：[`OURDOMAIN.md`](OURDOMAIN.md)
3. **HousingAnywhere — 最大覆盖（1.5–2 周）**
   - 工程量低、合规清楚、用户群匹配、量大（196 仅 Amsterdam）
   - 用现有 `scrapers/` 包架构 + HTML 解析
4. **SSH — 填空城市（2–3 周）**
   - 全国 44 条覆盖 9 城，填 H2S 没覆盖的 Utrecht / Maastricht / Groningen
   - 工程量略大（需 SPA bundle 分析）
5. **Pararius — 现在有 CloakBrowser 基建**，H2S 已验证可行。可以考虑启动探测（Funda 同理）
6. **DUWO / Kamernet — 放弃**（合规 / 商业模式不允许）

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
