# Holland2Stay — 平台状态

本文记录 Holland2Stay（下称 H2S）的抓取现状：端点契约、Cloudflare 与站点自有的两层
校验、以及自 2026-08-17 起必须走的加密信道。

代码位于 [`scrapers/holland2stay.py`](../scrapers/holland2stay.py) 与
[`browser_fetcher.py`](../browser_fetcher.py)；后者为三个走浏览器的平台共用。

---

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.holland2stay.com`（Next.js） |
| 数据形态 | GraphQL JSON（Magento 后端） |
| 端点 | `https://www.holland2stay.com/api/__enc__`（加密信封，见 §4） |
| 传输方式 | CloakBrowser（patched Chromium）内 `page.evaluate(fetch)` |
| 覆盖城市 | 由 `CITIES` 配置，当前生产为 Amsterdam / Eindhoven |
| 在本项目中的地位 | 房源量最大的一个源，且是唯一开启自动预订的平台 |

---

## 2. 端点迁移史

该端点已迁移三次，**每次都是静默的**：旧路径不重定向、不返回结构化错误，直接消失。
后两次相隔仅六天。

| 时间 | 端点 | 旧路径的表现 |
|---|---|---|
| — | `api.holland2stay.com/graphql` | 被 Cloudflare 封锁，curl_cffi 直连不再可行 |
| 2026-06 | `www.holland2stay.com/api/graphql` | 同域，进入 Cloudflare 托管挑战之后 |
| 2026-08-11 19:34 | `www.holland2stay.com/api/service/residences` | 旧路径返回 **404 + Next.js 错误页**（HTML，非 JSON） |
| 2026-08-17 08:11 | `www.holland2stay.com/api/__enc__` | 同上 404；且**改为加密信封**，明文请求直接触发 Cloudflare 挑战 |

2026-08-18 08:11 又加了一道 **GraphQL operation 白名单**（端点未变）：不在名单里
的查询一律 `403 {"code":"operation_not_allowed"}`。见 §5。

三次迁移中，**GraphQL schema 与查询语句均逐字未变**——变的只有传输层。这不是推测：
在 `crypto.subtle.encrypt` 上打钩子截获站点加密**之前**的明文，可见它发的仍是
`GetCategories` / `products` / `category_uid:"Nw=="` 与同一套 `available_to_book` ID。
因此 `_GQL_QUERY` 与 `_to_listing` 历次迁移一行未改。

路径常量为 `browser_fetcher._H2S_GQL_PATH`。

**下次迁移时怎么找新端点**，按顺序：

1. 以空 body 打候选路径。端点正确时返回 GraphQL 语法错误而非 HTML：
   `{"errors":[{"message":"Syntax Error: Unexpected <EOF>"...}]}`。
   注意区分两种 404——JSON 的 `{"error":"Not found"}` 说明该命名空间存在，
   Next.js 的 HTML 错误页说明整条路径都不在。
2. 若候选路径全军覆没，**钩住 `crypto.subtle.encrypt` 截明文**（站点自己会加密，
   钩子能拿到它加密前的完整 payload），再配合 hook `window.fetch` 看它 POST 去哪儿。
   2026-08-17 就是这么一步定位到 `/api/__enc__` 的。
3. 不要只盯着 axios 拦截器改写后的 `/api/rest/*`——那是 GET 分支的形状，实际
   POST 目标是 `/api/__enc__`。当时在这里绕了几轮。

> 2026-08-11 的这次迁移导致抓取中断三天。404 当时落在通用的 `status >= 400` 分支，
> 报为「抓取网络失败 … 请检查代理/网络」，而代理自始至终正常，排查方向被完全带偏。
> 404 现已单独成支并指明「上游很可能改了 API 路径」。详见 CHANGELOG v1.16.3。

---

## 3. 两层校验

访问 API 需依次通过两道关卡，二者互相独立。

### 3.1 Cloudflare 托管挑战

站点整体位于 Cloudflare 托管挑战之后，TLS 指纹伪装无法通过，因此必须使用真实
Chromium。`BrowserFetcher.ensure_initialized()` 导航至主站
（`_H2S_MAIN_PAGE = /residences`）完成挑战，取得 `cf_clearance`。

`cf_clearance` 与出口 IP 绑定，故 H2S 的 profile 使用**固定 sticky 代理**——与
OurDomain 相反，后者以更换 IP 作为唯一的恢复手段。

### 3.2 站点自有的 clearance

挑战通过之后，站点自身还有一道校验。未通过时 API 返回：

```
HTTP 403  {"error":"Browser verification required","code":"clearance_required"}
```

握手由站点前端自行完成，三个接口按序发生：

```
GET  /api/remote      → {"verified":true,"realClientIp":"…","country":"NL"}
POST /api/clearance   → {"token":"<Turnstile token>","provider":"turnstile"}
                        成功后下发 cookie h2s_clr
```

本项目不复刻该握手，而是**导航后等它自己完成**：`SiteProfile.clearance_probe` 以最小
查询轮询端点，`clearance_pending_markers` 含 `clearance_required`，直至通过为止。等不
到则重新导航——token 由导航签发，继续轮询换不出来，只会朝一个拿不到 clearance 的会话
打一串必然 403 的请求。

该机制历经 2026-08-11 与 08-17 两次改版**未作任何改动**即继续工作。

---

## 4. 加密信道（自 2026-08-17 起必经）

GraphQL 请求体须包成加密信封投递，明文请求会直接触发 Cloudflare 挑战。算法照抄站点
自己的 JS（`_next/static/chunks/common-*.js`，搜 `__enc__`）：

```
aesKey  = AES-GCM 256，每次请求新生成
k       = RSA-OAEP(SHA-256) 包裹 aesKey 的裸字节
iv      = 12 字节随机数
d       = AES-GCM(aesKey, iv, 明文)
信封     = {v:1, k, iv, d, ct}        全部 base64
```

`POST /api/__enc__`，带 `x-enc: 1`。响应带 `x-enc: 1` 时 body 为 `{iv, d, ct}`，用同一个
`aesKey` 解开即得明文 GraphQL 响应。

实现见 `BrowserFetcher._encrypted_fetch`，由 `SiteProfile.encrypted_envelope` 开关控制
（仅 H2S 打开）。它**只改传输层**：调用方照旧传明文 body、拿到明文 text，返回形状与
`_raw_fetch` 完全一致，因此 `fetch_gql` 以上（scraper / booker）一行未改。

响应没有 `x-enc` 头时原样返回明文——403 的 `{"code":"clearance_required"}` 正是这么回
的，§3.2 的 clearance 探测依赖这一点。

### 4.1 为什么在页面里加密

WebCrypto 就在手边，密钥材料不出浏览器；更重要的是这样能沿用同源 `fetch` 的全部凭据
（cookies、clearance、TLS 指纹），与既有传输层完全同构。

### 4.2 公钥不写死

公钥是 bundle 里的 SPKI base64 常量（实测 392 字符），由 `_ensure_enc_pubkey` 在**运行
时**从含 `__enc__` 的 chunk 中抓取，每个浏览器会话一次，随浏览器重建（2 小时）而失效。

写死会在轮换当天变成一次无从下手的解密失败；绑在浏览器生命周期上，则轮换最多废掉一
个会话。抓不到时抛出的异常会指明去哪个 chunk 找什么常量。

### 4.3 曾经走过的弯路

`/api/rest/*` 是 axios 拦截器 **GET 分支**改写后的形状（把 path+query 加密进 `x-enc-q`
头，URL 改写为 `/api/rest/__enc__`）。POST 分支**不改写 URL**，实际目标是
`/api/__enc__`。2026-08-17 定位时先盯着 `/api/rest/*` 试了几轮，均为 403 Cloudflare
挑战或 400 `Specified request cannot be processed`。

判断信封本身是否正确的信号：响应带 `x-enc: 1` 且能解密——哪怕内容是报错，也说明服务端
已成功解开信封，问题在 payload 而非密码学。

---

## 5. 端点契约

```
POST https://www.holland2stay.com/api/__enc__
Content-Type: application/json
x-enc: 1

<信封，见 §4>  ←  明文为 {"query": "<GraphQL>", "variables": {...}}
```

查询语句见 `scrapers.holland2stay._GQL_QUERY`。变量形如：

```json
{"pageSize": 100,
 "currentPage": 1,
 "filters": {"category_uid": {"eq": "Nw=="},
             "city": {"in": ["29"]},
             "available_to_book": {"in": ["179", "336", "6203"]}},
 "sort": {"available_startdate": "ASC"}}
```

`total_pages` 缺失时**必须标记为不完整**——把「没拿到数据」当成「确认没有数据」，正是
当年那次七周静默故障的判据类型，且这一组合恰好会让 stale 收敛清空整座城市。

**只请求 `_to_listing` 真正读取的字段。** 每轮两个城市、一天数百轮，响应体是按天计的
代理流量。2026-08-07 实测，`media_gallery`（平均 10.8 条图片 URL，从未被读取）一项即
占响应体 70%：

```
完整查询  2,096 B/条    92 MB/天
裁剪后      583 B/条    26 MB/天
```

由 `tests/test_h2s_query_fields.py` 守卫：新增字段而不在 `_to_listing` 中读取即失败。

### 5.1 operation 白名单（2026-08-18 起）

查询文本必须与站点自己发的**逐字段一致**。实测判据：

```
站点原文                            200
删掉 image_manager 块                403
加 tenant_profile_restrictions       403
加 available_startdate               403
只改空格                             200   ← 空白不敏感，字段集敏感
```

即白名单比对的是归一化后的**字段集合**，且 `operationName` 缺失同样 403。
`variables` 不受限制——城市、可用状态、分页、排序都可自由传。

因此查询存于 [`h2s_gql.py`](../h2s_gql.py)，是**照抄品**：

- 不要「优化」它。我们此前为省流量裁掉了 `media_gallery` 等字段，正是那份裁剪版
  在 2026-08-18 被全量拒绝，抓取中断。
- 上游改版后重新照抄：钩住 `crypto.subtle.encrypt` 截获站点加密前的明文即可。
- 由 `tests/test_h2s_query_fields.py` 守卫，字段增删即失败。

### 5.2 照抄的代价：三个字段拿不到

白名单那条查询不含以下字段，我们原先在用：

| 字段 | 用途 | 现状 |
|---|---|---|
| `tenant_profile_restrictions` | 学生 / 上班族标签 | 仍可作**筛选条件**（实测 Eindhoven 不限状态 78 条），且主查询自带的 aggregations 会报出有无受限房源。2026-08-18 当天可订的 48 套里一套都没有，故暂未接入；`tenant` 维度已从 H2S 的能力表摘除，见下 |
| `building_name` | 展示用 | 同样可经 aggregations 取回，需要时再补 |
| `available_startdate` | `Listing.available_from` | **拿不到**。不能用 `next_contract_startdate` 顶替——可订房源里大量是 `2050-01-01` 哨兵值。真实来源是 SSR 页面：房源卡片上写着 `Available per Aug 20, 2026`，实测 10/10 可解析，但一页仅 10 条 |

`tenant` 已从 `config._SOURCE_FILTER_DIMS["holland2stay"]` 中摘除。**必须摘**：该维度
fail-closed，缺值即拒绝，留着会让勾「仅学生」的用户一条 H2S 房源都收不到——比没有
这个筛选更糟。OurDomain / OurCampus / Xior 不受影响。

### 5.3 分层抓取：白名单之后唯一的省流量手段

字段集被锁死，响应体没得裁，能动的只有「查什么」。2026-08-18 实测两城合计、线上
真实字节（加密响应不可压缩，无 gzip 收益）：

```
只查 可订 + 抽签 + 即将上线      2.2 KB/轮
再加上 Reserved               292.2 KB/轮
```

贵的不是「有没有新房」，是那批已被预订的房源——每轮完整拉一遍，而 Reserved 状态
几乎不动。故拆成两层（`scrapers/holland2stay.py` 的 `_FRESH_STATUSES` /
`_ARCHIVE_STATUSES`）：

- **每轮**只查可订类。新房源必然先出现在这里，通知不延迟。
- **每 `_FULL_SCAN_INTERVAL`（默认 1800 秒）**做一次含 Reserved 的全量。

两条必须守住的性质，均有测试（`tests/test_h2s_tiered_scan.py`）：

1. **高频轮一律 `complete=False`。** 它看不见 Reserved，若标成完整扫描，stale
   收敛会把那批房源判成「已下架」清掉。
2. **层级按批次决定，不按城市。** 实现时踩过：在 `_plan_scan` 里直接推进计时器，
   导致一轮里第一个城市消耗掉「该全量」的标记，其余城市统统被降级。

### 5.4 attribute label 映射

枚举字段返回的是 attribute option ID，须映射为可读 label。以前单独发一条
`GetAggregations` 查询，2026-08-18 起被白名单挡掉——好在白名单这条 `GetCategories`
的 ProductsFragment 本就带 `aggregations`，同一响应里即可取（`_labels_from_aggregations`），
反而省掉一次请求。

`available_to_book` 不在其中，其 ID→label 由 `_STATUS_MAP` 硬编码。

映射需**跨轮累积**：aggregations 是按当前 filters 统计的，高频轮里 Reserved 房源的
label 根本不出现，覆盖式赋值会让上一轮攒到的映射丢失，features 里就会冒出裸 ID。

上游返回的 label 荷兰语与英语混杂（生产库中 `Finishing: Furnished` 与
`Finishing: Gemeubileerd` 并存）。归一由 `models.FEATURE_SYNONYMS` 与
`canonical_feature()` 在过滤层完成，**不在抓取层做**——上游文案随时可能新增写法，
抓取层归一会把未收录的值直接丢掉。

## 6. 风险

| 风险 | 现状 |
|---|---|
| 端点再次迁移 | **已发生三次**，后两次相隔六天，均为静默 404。定位步骤见 §2；404 已单独成支并指向「上游改了路径」 |
| 加密公钥轮换 | 运行时抓取，绑浏览器生命周期，轮换最多废掉一个会话。见 §4.2 |
| 信封格式变更（`v` 从 1 起跳） | 目前无版本协商。届时解密会失败，需重读 chunk 里的 `__enc__` 实现 |
| Turnstile 加码 | 当前由站点前端自动完成，本项目只等待。若改为需要交互，则须接入打码服务 |
| 出口 IP 被 Cloudflare 盯上 | 挑战连续失败即熔断退避；重建浏览器会更换出口 IP |
| operation 白名单收紧 | 已发生一次。查询是照抄品，字段增删即 403。定位与重抄步骤见 §2 / §5.1 |
| 白名单查询里的字段被上游删掉 | 我们跟着丢字段，且无从补救（只能另找 SSR 等来源）。§5.2 已有三个先例 |
| GraphQL schema 变更 | `total_pages` 缺失时标记不完整；字段集由 `tests/test_h2s_query_fields.py` 守卫 |
| label 出现新的语言写法 | 过滤层归一，未收录的值原样保留而非丢弃；补 `FEATURE_SYNONYMS` 即可 |

---

## 7. 自动预订

H2S 是当前唯一开启自动预订的平台（`monitor._AUTO_BOOK_SOURCES`）。所有 GraphQL
mutation 同样经由 `BrowserFetcher`，与抓取共用会话与 clearance。流程止于支付页，不代
填任何金融凭据。
