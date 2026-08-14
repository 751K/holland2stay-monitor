# Holland2Stay — 平台状态

本文记录 Holland2Stay（下称 H2S）的抓取现状：端点契约、Cloudflare 与站点自有的两层
校验、以及 2026-08-11 那次改版引入的加密信道。

代码位于 [`scrapers/holland2stay.py`](../scrapers/holland2stay.py) 与
[`browser_fetcher.py`](../browser_fetcher.py)；后者为三个走浏览器的平台共用。

---

## 1. 平台概况

| 项 | 值 |
|---|---|
| 官网 | `https://www.holland2stay.com`（Next.js） |
| 数据形态 | GraphQL JSON（Magento 后端） |
| 端点 | `https://www.holland2stay.com/api/service/residences` |
| 传输方式 | CloakBrowser（patched Chromium）内 `page.evaluate(fetch)` |
| 覆盖城市 | 由 `CITIES` 配置，当前生产为 Amsterdam / Eindhoven |
| 在本项目中的地位 | 房源量最大的一个源，且是唯一开启自动预订的平台 |

---

## 2. 端点迁移史

该端点已迁移两次，**每次都是静默的**：旧路径不重定向、不返回结构化错误，直接消失。

| 时间 | 端点 | 旧路径的表现 |
|---|---|---|
| — | `api.holland2stay.com/graphql` | 被 Cloudflare 封锁，curl_cffi 直连不再可行 |
| 2026-06 | `www.holland2stay.com/api/graphql` | 同域，进入 Cloudflare 托管挑战之后 |
| 2026-08-11 19:34 | `www.holland2stay.com/api/service/residences` | 旧路径返回 **404 + Next.js 错误页**（HTML，非 JSON） |

两次迁移中，**GraphQL schema 与查询语句均逐字未变**——变的只有路径。2026-08-14
实测：生产查询在新路径上返回 43 条（Eindhoven，三种可用状态），字段集与响应体大小
（571 B/条）与迁移前完全一致。

路径常量为 `browser_fetcher._H2S_GQL_PATH`。**判断新路径是否正确的方法**：以空 body
打一次，端点正确时返回 GraphQL 的语法错误而非 HTML：

```json
{"errors":[{"message":"Syntax Error: Unexpected <EOF>","locations":[{"line":1,"column":1}]}]}
```

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

该机制在 2026-08-11 改版前后**未作任何改动**即继续工作。

---

## 4. 加密信道：存在，但当前用不上

同一次改版引入了一层应用级加密。客户端以 AES-GCM 加密请求体、RSA-OAEP 包裹会话
密钥，投递如下信封：

```json
{"v":1,
 "k":"<RSA-OAEP 包裹的 AES-256 密钥, base64>",
 "iv":"<12 字节 nonce, base64>",
 "d":"<AES-GCM 密文, base64>",
 "ct":"application/json"}
```

- **GET**：把 `path + query` 加密后置于 `x-enc-q` 头，URL 改写为 `/api/rest/__enc__`
- **POST**：信封作为 body，置 `x-enc: 1`
- **响应**：带 `x-enc: 1` 时以同一会话密钥解密

**但它只作用于部分路径。** axios 拦截器中的判据是：

```js
if (!(t.url && t.url.startsWith("/api/rest/"))) return t;   // 不加密，原样放行
```

`/api/service/residences` 不以 `/api/rest/` 开头，因此**仍是明文 GraphQL**，本项目无需
实现这套加密。

若其日后迁入 `/api/rest/`：**不要自行实现 RSA+AES**。公钥内嵌于 JS 且可能轮换，自建
实现会成为第三个需要跟随上游改版的地方。正确做法是从页面的 webpack runtime 中取出
该加密函数直接调用——反正已经跑着真实浏览器，页面自己就能加密。相关代码位于
`_next/static/chunks/common-*.js`，搜 `__enc__` 即可定位。

---

## 5. 端点契约

```
POST https://www.holland2stay.com/api/service/residences
Content-Type: application/json

{"query": "<GraphQL>", "variables": {...}}
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

### 5.1 attribute label 映射

枚举字段返回的是 attribute option ID，须经 aggregations 接口映射为可读 label：

```
query{products(filter:{category_uid:{eq:"Nw=="}}){
  aggregations{attribute_code label options{label value}}}}
```

返回 15 个 aggregation（`city` / `finishing` / `type_of_contract` /
`tenant_profile_restrictions` 等）。`available_to_book` **不在其中**，其 ID→label 由
`_STATUS_MAP` 硬编码。

上游返回的 label 荷兰语与英语混杂，且同一字段在不同房源上可能不一致（生产库中
`Finishing: Furnished` 与 `Finishing: Gemeubileerd` 并存）。归一由
`models.FEATURE_SYNONYMS` 与 `canonical_feature()` 在过滤层完成，**不在抓取层做**——
上游文案随时可能新增写法，抓取层归一会把未收录的值直接丢掉。

---

## 6. 风险

| 风险 | 现状 |
|---|---|
| 端点再次迁移 | 已发生两次，且均为静默 404。判据见 §2；404 已单独成支并指向「上游改了路径」 |
| `/api/service/residences` 迁入 `/api/rest/` | 届时需接入加密信道。做法见 §4——取用页面自身的加密函数，不要自行实现 |
| Turnstile 加码 | 当前由站点前端自动完成，本项目只等待。若改为需要交互，则须接入打码服务 |
| 出口 IP 被 Cloudflare 盯上 | 挑战连续失败即熔断退避；重建浏览器会更换出口 IP |
| GraphQL schema 变更 | `total_pages` 缺失时标记不完整；字段增减由 `tests/test_h2s_query_fields.py` 守卫 |
| label 出现新的语言写法 | 过滤层归一，未收录的值原样保留而非丢弃；补 `FEATURE_SYNONYMS` 即可 |

---

## 7. 自动预订

H2S 是当前唯一开启自动预订的平台（`monitor._AUTO_BOOK_SOURCES`）。所有 GraphQL
mutation 同样经由 `BrowserFetcher`，与抓取共用会话与 clearance。流程止于支付页，不代
填任何金融凭据。
