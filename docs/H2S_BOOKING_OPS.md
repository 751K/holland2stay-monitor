# H2S 预订链路 —— operation 原文与现状

**采集时间** 2026-08-19，方式：浏览器内读取站点自己的 JS chunk（纯静态读取，
未登录、未提交任何表单、未产生任何订单）。

采集手法与 `docs/H2S.md` §4.2 找加密公钥是同一套：operation 文档在构建时被内联进
chunk 的模板字符串里，`fetch(chunkUrl).then(r => r.text())` 就能拿到原文，不必跑一遍
真实业务流程。

- www 侧全部预订/购物车 operation：`_next/static/chunks/common-6710491a-*.js`（16 KB）
- 租户侧（登录、账单、工单）：`tenant.holland2stay.com` 的 `_app-*.js`

---

## 1. 结论先行

`booker.py` 那 9 条 operation 与站点实际使用的对照：

| booker 里写的 | 站点实际 | 状态 |
|---|---|---|
| `GetProduct` | **`GetProductDetail`** | 名字就不对，字段集也不同。403 的直接原因 |
| `GenerateCustomerToken` | **已不是 GraphQL** | 见 §3。死路，不是照抄能解决的 |
| `CreateEmptyCart` | `CreateEmptyCart` | 可照抄，见 §2 |
| `AddNewBooking` | `AddNewBooking` | 可照抄 |
| `SetPaymentMethodOnCart` | `SetPaymentMethodOnCart` | 可照抄 |
| `PlaceOrder` | `PlaceOrder` | 可照抄 |
| `IdealCheckOut` | `IdealCheckOut` | 可照抄 |
| `GetCheckoutAgreements` | `GetCheckoutAgreements` | **已经逐字一致** —— 所以它一直 200 |
| `CancelOrder` | 两个 bundle 里都没有 | 未解决。可能在未加载的租户页 chunk 里 |

**8 条里 7 条能照抄，但没用** —— 登录那条是死的，后面 7 条一条都走不到。

---

## 2. 可照抄的 operation 原文（逐字）

### GetProductDetail —— 替代 booker 的 `GetProduct`

```graphql
query GetProductDetail($filters: ProductAttributeFilterInput) {
  products(filter: $filters) {
    aggregations {
      label
      count
      attribute_code
      options { label count value }
      position
    }
    items {
      name sku city neighborhood living_area building_name resident_type
      no_of_rooms min_income floor finishing flooring curtains lighting
      price_range {
        minimum_price {
          regular_price { value currency }
          final_price { value currency }
        }
      }
      private_outside_area next_contract_startdate current_lottery_subscribers
      allin_excl_text maximum_day_selection basic_rent location_in_building
      lumpsum_service_charge inventory caretaker_costs start_unit_date
      service_costs_website supplies_website income_requirements tenant_profile
      cleaning_common_areas energy_label energy_common_areas residence_video
      residence_google_maps maximum_number_of_persons type_of_contract
      allowance_price pets_allowed parking_status storage_available
      minimum_stay meta_description meta_title meta_keyword overview
      book_now_text
      short_description { html }
      description { html }
      location { html }
      url_key offer_text offer_text_two available_to_book view_from_residence
      deposit
      small_image { url label }
      image_manager {
        tour360
        images { position image thumb }
      }
    }
    total_count
    page_info { page_size }
  }
}
```

`image_manager` 里那段是站点侧的片段常量（chunk `common-f3956634` 的 module 1511），
原文就是内联进来的，这里已经展开。

**顺带**：这条查询里有 `building_name`、`tenant_profile`、`min_income`、
`income_requirements`。`docs/H2S.md` §5.2 记着前者「需要时可经 aggregations 取回」
—— 实际站点自己有一条现成的、白名单登记过的查询直接给。抓取侧要不要用它是另一个
话题（它是单房源查询，不适合列表扫描）。

### CreateEmptyCart

```graphql
mutation CreateEmptyCart {
  createEmptyCart
}
```

### AddNewBooking

```graphql
mutation AddNewBooking(
  $cart_id: String!
  $sku: String!
  $contract_startDate: String
  $contract_id: Int
  $option_selected: String
) {
  addNewBooking(
    cart_id: $cart_id
    sku: $sku
    contract_startDate: $contract_startDate
    contract_id: $contract_id
    option_selected: $option_selected
  ) {
    cart {
      items {
        id
        quantity
        product { name sku }
        prices { price { value currency } }
      }
    }
  }
}
```

### SetPaymentMethodOnCart

```graphql
mutation SetPaymentMethodOnCart(
  $cartId: String!
  $paymentMethod: PaymentMethodInput!
) {
  setPaymentMethodOnCart(
    input: { cart_id: $cartId, payment_method: $paymentMethod }
  ) {
    cart {
      selected_payment_method { code title }
    }
  }
}
```

### PlaceOrder

```graphql
mutation PlaceOrder($cartId: String!, $storeId: Int) {
  placeOrder(input: { cart_id: $cartId, store_id: $storeId }) {
    orderV2 { order_number }
    errors { message code }
  }
}
```

### IdealCheckOut

```graphql
mutation IdealCheckOut($order_id: String!, $plateform: String) {
  idealCheckOut(order_id: $order_id, plateform: $plateform) {
    redirect
  }
}
```

### GetCheckoutAgreements（booker 现有的已经一致）

```graphql
query GetCheckoutAgreements {
  checkoutAgreements {
    name
    content
    checkbox_text
    mode
  }
}
```

### 同一 chunk 里还有（暂未用到，登记备查）

`GetCart` · `GetCartItems` · `GetLoginUserCart` · `GetCustomerQuery` ·
`GetCustomerAddresses` · `GetAvailablePaymentMethods` · `GetCountries` ·
`GetProducts` · `GetProductsWithOptions` · `GetBlock` · `CategoryUrl` ·
`SitemapData` · `GetAMastyForm` · `SubmitAmastyForm` · `TruncateCart` ·
`SetBillingAddressOnCart` · `PayRegFee` · `PlaceOrderMutation` ·
`PlaceOrderMollie` · `MollieProcessTransaction`

---

## 3. 登录：不是照抄能解决的

**`generateCustomerToken` 在两个 bundle 里都不存在。** 登录已经整体搬走：

- 换了主机 —— `tenant.holland2stay.com`，不再是 `www.holland2stay.com`
- 换了协议 —— Magento REST，不是 GraphQL：

```
GET_TOKEN      /api/rest/en/V1/integration/customer/token
VERIFY_TOKEN   /api/rest/en/V1/integration/customer/token/verify?token=…
REFRESH_TOKEN  /api/rest/V1/integration/customer/token/refresh
LOGOUT         /api/rest/V1/integration/customer/logout
```

- 前面加了两道闸：

```
/api/auth/captcha     POST {token, type, provider, action}   redux thunk "auth/verifyCaptchaToken"
/api/auth/verify-2fa  POST {...}                             redux thunk "tfa/tfaverify"
```

登录页（`https://tenant.holland2stay.com/`，根路径，不是 `/customer/account/login`
——那个 404）实测加载了 **Cloudflare Turnstile**：

```
https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit
window.turnstile === "object"
```

`render=explicit` 说明控件由 JS 按需渲染，页面静态 DOM 里看不到 —— 所以「打开登录页
没看见验证码」不等于没有。`/api/auth/captcha` 收的那个 `token` 就是 Turnstile token。

**这一步是自动化的硬边界。** 绕过 Turnstile 要么接打码服务，要么用真人。2FA 那条同理。
不是「照抄 operation」这个层面的问题。

未验证的部分：captcha / 2FA 是否每次登录都触发，还是按风险条件触发。要确认得真的登录
一次，而那需要真实账号密码。

---

## 4. 顺带记下的东西

- 详情页不走 GraphQL 取房源数据 —— 是 Next.js 服务端渲染 + 两个 REST 端点：
  `/rest/V1/building-housing-permit-info/{code}`、`/api/rest/V1/is-bookview/{sku}`
- 加密信封的调用结构（hook `crypto.subtle.encrypt` 实测）：
  `RSA-OAEP` 加密 32 字节 = AES 会话密钥；`AES-GCM` 加密业务载荷。
  **载荷不止是 GraphQL body，路径本身也走同一条加密信道**
  （实测捕获到 `/rest/V1/building-housing-permit-info/pbs` 明文）
- 站点内部把受保护路径写死成 `["/residences", "/checkout"]`，
  clearance cookie 的常量名是 `h2s_verified`
  （与我们记录的 `h2s_clr` 不同，可能是新旧两套，待确认）
- 官网博客有一篇 `holland2stay-becomes-codomo` —— 品牌在改名，
  可能预示又一轮域名/端点迁移

---

## 5. 对 `booker.py` 的建议

按投入产出排：

1. **先别照抄那 7 条。** 登录不通，照抄了也走不到。改完只是让 403 换个位置发生。
2. **`GetProduct` → `GetProductDetail` 可以单独改**，它不需要登录，是纯公开查询。
   但它在 booker 里只是 `listing.sku` 为空时的兜底，而抓取侧本来就给了
   sku / contract_id / start_date —— 现状下这条路几乎不会触发。改它属于收拾整齐，
   不解决任何用户可见的问题。
3. **真正的决策是自动预订还要不要留。** Turnstile + 2FA 之后，
   「后台替用户自动下单」这个形态在 H2S 上已经不成立。
   要么接打码服务（成本、可靠性、以及是否越界都得单独评估），
   要么把自动预订降级成「秒级通知 + 一键跳转到已填好的下单页」，让用户自己过验证。

---

## 6. 更正（2026-08-19，用户配合实登一次账号后实测）

前面 §3「登录被 Turnstile + 2FA 挡死」的结论**下早了**。用户在浏览器里真实登录一次、
我全程挂钩抓包（密码强制打码、占房/下单请求硬拦截、`Continue to payment` 由安全分类器
挡下未点）后，实际链路如下：

### 6.1 登录已换成 NextAuth（不再是 GraphQL mutation）

`generateCustomerToken` 这条 GraphQL mutation 彻底没了。现在是标准 NextAuth credentials 流：

```
GET  /api/auth/providers            → 确认 credentials provider
GET  /api/auth/csrf                 → { csrfToken }
POST /api/auth/callback/credentials → 表单提交 csrfToken + 账号 + 密码；200 → { url }
GET  /api/auth/session              → 见下
```

`/api/auth/session` 返回（accessToken 是短期令牌，非密码，未打码）：

```json
{
  "user": { ... },
  "expires": "…",
  "twoFaPending": false,
  "requires2fa": false,
  "accessToken": "<JWT>",
  "accessTokenExpiry": "…"
}
```

JWT payload：`{ "uid": 196098, "utypid": 3, "iat": …, "exp": … }`，**有效期 1 小时**。

**这次登录既没弹验证码，也没触发 2FA**（`twoFaPending:false`、`requires2fa:false`）。
所以 Turnstile / 2FA 是**条件触发**，不是每次必过。触发条件（新设备？风控评分？多次
失败？）未知——要摸清得多登几次，超出本次范围。

### 6.2 下单 GraphQL 的端点与鉴权（静态读 JS 确认）

```
端点   /api/service/graphql        （加密变体 /api/__enc__，x-enc:1）
鉴权   Authorization: Bearer ${session.accessToken}
```

即 §6.1 拿到的那个 JWT，直接当 Bearer 用在所有下单 mutation 上。tenant 与 www
共用它——session cookie 落在 `.holland2stay.com` 顶级域，两个子站都读得到。这也是为什么
登录 tenant 之后，回 www 点 Book 直接进下单向导、没有再要一次登录。

### 6.3 下单向导是三步，前两步纯前端

```
Book → 1. Overview（房源/月费概览）
     → 2. Requirements（选 Student / Working professional ← 就是 tenant_profile）
     → 3. Payment（点 "Continue to payment" 才首次触网建单）
```

第 1、2 步不发任何请求，全是前端状态。`createEmptyCart` / `addNewBooking` 要到
「Continue to payment」才触发——那一步会占房，本次未点（安全分类器挡下，与我们自己的
红线一致）。

### 6.4 修 booker 的完整清单（更新版）

| 环节 | 旧代码 | 应改为 |
|---|---|---|
| 登录 | GraphQL `generateCustomerToken` | NextAuth：csrf → callback/credentials → session 取 `accessToken` |
| token | mutation 返回的 token | `/api/auth/session` 的 `accessToken`（JWT，1h） |
| 取房源参数 | `GetProduct` | `GetProductDetail`（§2 有原文） |
| 端点 | `/api/__enc__`（对） | 不变 |
| 鉴权头 | `Bearer <token>`（对） | 不变，token 换成 accessToken |
| 2–7 步 operation | 自写 | 照抄 §2 原文 |
| 2FA | 无处理 | 需处理 `requires2fa:true` 分支——触发时无法全自动 |

**关键改动是登录那一步**：从「发一条 GraphQL mutation」变成「跑一遍 NextAuth 三步握手」。
其余基本是照抄 operation + 改个 token 来源。

### 6.5 仍未验证 / 仍是风险

- `createEmptyCart` / `addNewBooking` 的实际端点与鉴权：只从 JS 静态确认了
  `/api/service/graphql` + `Bearer accessToken`，没有真的发一次（发了就占房）。
  按 §2 原文 + §6.2 鉴权实现后，需要用真实账号跑一次「加购但不下单」来收尾验证。
- 2FA 触发条件未知。若生产账号登录会触发 2FA，全自动预订就断在这——除非那些账号
  能关掉 2FA，或接受半自动（人过 2FA）。
- NextAuth callback 的确切表单字段名（csrfToken 之外账号字段叫 email 还是 username）
  没抓到明文——POST body 是 URLSearchParams，我的钩子只记了 `[non-string]`。实现时
  再定一次即可（或读 provider 配置）。

---

## 7. 已实现（v1.16.9，2026-08-19）

§6 的发现已落地到代码。改动清单：

- `browser_fetcher.py`：`_raw_fetch` 加 `encrypted` 覆盖参数；新增 `fetch_plain()`——
  强制明文、跳过 CF-403 重建，专给 NextAuth 端点用（套加密信封会 400）。
- `booker.py`：`login()` 重写成 NextAuth 三步握手；`_gql` 与全部下单步骤补 `operation_name`；
  `GetProduct` → 照抄的 `GetProductDetail`；新增 `AuthError` / `TwoFactorRequiredError`
  与 phase `auth_failed` / `auth_2fa`。
- `h2s_booking_gql.py`（新）：7 条下单 operation 逐字原文（照抄品）。
- 测试：`test_booker_login.py` · `test_h2s_booking_gql.py` · `test_booker_operations.py`
  · `browser_fetcher` 明文通道。

**仍未收尾**（见 §6.5）：createEmptyCart→…→placeOrder 未经真实下单验证；
`cancel_pending_orders` 的两条 operation 没照抄（在租户门户 bundle 里），仍是自写版，
很可能 403，但只在取消旧预留单时触发且有兜底。首次真实预订前，用真实账号跑一次
「加购但不 placeOrder」即可收尾。

---

## 6.6 取消预留的真实机制（2026-08-19，静态读租户门户 JS）

`cancelOrder` 那块之前标「未验证」。用抓下单 operation 的同一招——静态读
`tenant.holland2stay.com` 的 `_app-*.js`——挖出了真实机制，**零副作用、不碰 Turnstile、
不用真取消**。

结论：**取消不是 GraphQL，是 REST，按 SKU 取消。**

租户门户里的原文（redux thunk `contracts/cancelReservation`）：

```
POST {base}/rest/V1/customer/bookingcancel/{sku}
Body:    {}                         （空）
Headers: Authorization: Bearer {accessToken}
         Content-Type: application/json
```

`{base}` = `/api`。参数是 **sku**，不是订单号 / entity_id。

配套的「列出预留拿 SKU」端点（同一张 REST 端点表里）：

```
GET /api/rest/V1/newdashboard/contract/me
    ?fields=items[id,sku,product_name,building_name,status,start_date,customer_id,refund_date]
```

status 落在 `reserved / pending / pending_payment / processing` 的就是可取消的预留。
另有 `GET /api/rest/V1/customer/orders?fields=items[increment_id,entity_id,...,status]`
返回订单视角，但取消要 SKU，所以用 `contract/me` 那条。

所以 booker 旧的 GraphQL `customer{orders}` + `cancelOrder` **整个是错的**——站点根本
不这么取消。已按上面重写成 REST（`booker.cancel_pending_orders`）。

**仍存的不确定**：租户门户把 REST **路径本身也塞进加密信封**（抓包见过
`/rest/V1/customer-notifications/me` 作为 encrypt 明文）。www 上这些端点走明文还是
走信封，静态读不出来。当前实现按明文（`fetch_plain`）；若线上回 400/403，改走信封
（那需要 REST-over-envelope 的编码，本次没抓）。这条路是 reserved_conflict 边缘场景 +
try/except 兜底，未验证不影响主流程。

## 6.7 下单每步精确报文：为什么不再单独抓

上次跳转支付页把内存里的抓包冲掉了。要补齐得再走一笔真单（占房+订单），代价不小。
判断是**不值**：

- 7 条 operation 的**原文已逐字照抄**（§2），请求字段名、响应结构都在原文里。
- 真实抓包只能再确认几个**值的格式**。逐一核对后，唯一有实质不确定的是
  `addNewBooking` 的 `contract_startDate` 格式——`booker._to_h2s_date` 转成
  `DD-MM-YYYY`。这是几个月前 booker 能下单时就在用的格式，暂按其正确；上游若改，
  真实下单会在这步报错，到时按站点实际发的值改即可。
- 其余（cart_id/sku 字符串、`paymentMethod {code}` 对象、store_id int）都是标准
  Magento 形状，原文即可确认。

结论：不为「再确认已知结构」去创建一笔真订单。真正的收尾验证放在首次真实预订那次
一起做（§6.5）。

---

## 6.8 ⚠️ 重大发现：占房步骤已迁到 Turnstile 门后（2026-08-19，静态读 www JS）

为验证下单链路,静态读了 www 房源页的下单提交代码。结论比预期严重。

### 确认对的（两处，静态坐实）

- **日期格式** `contract_startDate` = **`DD-MM-YYYY`**。站点原文:
  ``String(getDate).padStart(2,"0")-String(getMonth+1).padStart(2,"0")-getFullYear``。
  booker 的 `_to_h2s_date` 正是这个 ✓
- **store_id = 54**。站点按 sku 前缀（`d-`/`m-`/`sc`/`j-`/`w-`…）判 `r=54`。
  booker 的 `_H2S_STORE_ID = 54` ✓

### 但架构变了：`createEmptyCart` + `addNewBooking` 客户端已不再直接调

站点现在的下单提交是:

```js
const a = await D.zg({ sku, contract_startDate, challengeToken, challengeProvider });
const r = await fetch("/api/booking", { method:"POST", headers:{...a.headers}, body:a.body });
const t = await r.json();          // → { cartId, booking }
localStorage.setItem("cartId", t.cartId);
// 之后 ej()：用 cartId + accessToken + store_id(54) 走 placeOrder/支付
```

要点:

- **占房走服务端代理 `POST /api/booking`,不是直接 GraphQL。** 它内部替你做
  createEmptyCart + addNewBooking，返回 `{cartId}`。
- **带 `challengeToken`（Turnstile）**。占房这一步现在被人机验证挡着。
- bundle 里 `AddNewBooking` / `CreateEmptyCart` 两个 GraphQL operation **只剩定义、
  没有客户端调用点**——已被 `/api/booking` 取代。
- 下游（placeOrder / idealCheckOut）看着仍是直接 GraphQL（只有占房那步被代理）。

### 对 booker 的意义（未决风险）

booker 现在的做法是**直接发 GraphQL `createEmptyCart` + `addNewBooking`**——这是旧流程。
两种可能，静态读分不出，只有真发一次 addNewBooking（会占房）才知道:

- **乐观**:后端仍接受直接 GraphQL `addNewBooking`（operation 还在白名单里），
  `/api/booking` 只是官网前端多加的一层。那 booker 照旧能用。
- **悲观**:后端现在要求占房必须走 `/api/booking` 的 Turnstile 校验，直接 GraphQL
  `addNewBooking` 会被拒。那 booker 的占房这一步是坏的，**且无法全自动修复**——
  Turnstile 挡着，和登录 2FA、取消预留是同一堵墙。

**没有真占一次房，无法判定是哪种。** 我不做这个真实副作用测试。

### 建议

1. **先按现状**：v1.16.9 的其余修复（NextAuth 登录、operationName、GetProductDetail、
   REST 取消）都是实打实的进步，且日期/store_id 已确认。占房那步维持直接 GraphQL。
2. **真实验证只能等一次真预订**：下次有用户真要订，看 `addNewBooking` 是成功还是被拒。
   成功=乐观情形，booker 可用；被拒（尤其带 challenge/verification 字样）=占房已迁到
   Turnstile 门后，需要重新设计（半自动：秒级通知 + 一键跳到官网下单页让用户过 Turnstile）。
3. **长期方向**：占房被 Turnstile 保护后，「后台全自动下单」这个形态在 H2S 上正在关闭。
   半自动（我们抢速度、用户过验证）可能是唯一可持续的路。
