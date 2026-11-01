# Holland2Stay 预订接口与验证状态

本文记录 H2S 自动预订的当前代码路径、已知验证边界，以及 2026-08-19 采集的站点
operation 原文。上游接口可能变化；抓取、浏览器校验和排障另见 [H2S.md](H2S.md)。
系统的运行行为以 [booker.py](../booker.py)、[bookers/holland2stay.py](../bookers/holland2stay.py)、
[h2s_booking_gql.py](../h2s_booking_gql.py) 和 [browser_fetcher.py](../browser_fetcher.py)
为准。本文保留的接口原文和历史探测记录不能证明线上接口的实时状态。

## 1. 当前预订流程

预订任务在独立工作进程的浏览器会话中执行；符合条件时，系统可以复用同一账号的预登录
结果。当前预订流程如下：

1. 如果房源没有 SKU，系统通过 `GetProductDetail` 补取 SKU 和可用的合同信息。
2. 系统使用预登录会话，或通过 NextAuth 登录获取访问令牌（access token）。
3. 系统调用 `POST /api/booking` 建立购物车并发起占房，随后获取 `cartId`。
4. 系统依次调用 `SetPaymentMethodOnCart`、`GetCheckoutAgreements`、`PlaceOrder`
   和 `IdealCheckOut`，尝试生成支付链接。如果协议查询失败，系统会记录警告并继续下单。
5. 系统返回预订结果并通知用户。用户需要自行完成支付；系统不会填写金融凭据或代付。

试运行只检查 SKU 和登录等提交前步骤，不发起占房请求，也不创建订单。试运行通过
不能保证正式提交成功。如果预订请求发出后的结果不确定，系统会保留账号级保护记录并
通知用户核实；后台任务仍在运行时，用户或管理员不能解除保护。保护规则及用户操作见
[架构总览](ARCHITECTURE.md) 和
[使用指南](https://flatradar.app/guide?lang=zh)。

## 2. 验证状态与限制

| 项目 | 已知状态 |
|---|---|
| 旧预订流程 | 项目曾使用真实账号取得订单号和付款链接。 |
| 当前 `/api/booking` 流程 | 已验证：使用真实账号走通了从占房到取得付款链接的完整过程。 |
| NextAuth 登录 | 项目曾于 2026-08-19 使用真实账号登录。上游及账号状态决定是否需要 2FA 或其他验证；遇到 2FA 时，程序返回需要人工处理的结果。 |
| 2026-08-19 operation 探测 | 测试使用无效参数确认部分操作在当时获得业务响应；`AddNewBooking` 的响应表明该操作不在公开 API 中。这些结果不能保证当前站点仍采用相同规则。 |
| 取消预留 | 项目已实现取消流程，且仅考虑系统记录的自有预留；真实取消操作尚未验证。 |

探测时，未登录的 `/api/booking` 请求曾返回 401，那只说明当时路由存在。完整的占房
与支付链接流程现已用真实账号验证；试运行仍只检查提交前步骤，不代表正式提交一定
成功。

## 3. 登录与传输

### 3.1 登录

系统通过浏览器同源请求访问 NextAuth 的 `/api/auth/csrf`、
`/api/auth/callback/credentials` 和 `/api/auth/session`。session 响应返回访问令牌；
如果响应要求双因素认证，系统会停止自动预订流程。登录和抓取使用相同的传输实现，
但预订工作进程维护独立的浏览器会话。

### 3.2 请求传输

| 路径 | 当前实现 |
|---|---|
| `/api/auth/*` | 系统使用 `fetch_plain` 发送同源请求，不使用业务加密信封。 |
| `GET /api/rest/*` | 系统加密路径和查询参数，并通过 `x-enc-q` 将请求发送至 `/api/rest/__enc__`。 |
| `POST /api/rest/*` | 系统加密请求体，并将请求发送至原 URL。 |
| GraphQL `/api/__enc__` | 系统加密请求体。 |
| `POST /api/booking` | 系统加密请求体，并携带 Bearer 令牌。 |

不使用业务加密信封并不意味着网络明文传输：上述请求仍使用 HTTPS。具体实现见
`BrowserFetcher.fetch_plain()`、`fetch_rest()` 和加密请求方法。

### 3.3 占房入口

当前 `POST /api/booking` 请求包含 `sku`、`contract_startDate`、`challengeToken`、
`challengeProvider` 等字段；系统使用响应返回的 `cartId` 执行后续步骤。站点可能调整
接口和校验规则，因此维护者应对照站点当前发送的请求，不能仅依据历史 401 响应或旧
GraphQL operation 推断当前行为。

## 4. 取消预留

租户门户使用 REST 查询和取消预留：系统通过
`/api/rest/V1/newdashboard/contract/me` 查询预留，再根据 SKU 调用
`POST /api/rest/V1/customer/bookingcancel/{sku}`。项目中的 `cancel_pending_orders`
仅考虑本系统记录的自有预留；如果系统没有相应记录，该函数不会取消预留。预订流程
和配置决定是否调用该函数。该路径尚未通过真实取消操作验证，代码已经实现不能说明
该路径已经验证通过。

## 5. Operation 原文与历史参考

以下原文采集于 **2026-08-19**，本文保留这些内容以供核对请求字段；上游可能已经
调整接口。当前代码使用的定义位于 [h2s_booking_gql.py](../h2s_booking_gql.py)。
维护者在修改定义前，应重新核实站点发送的 operation。抓取器也使用 `GetProductDetail`
按需补齐房源详情。

### 5.1 当前链路使用的 operation

#### GetProductDetail

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

站点将 `image_manager` 字段定义为片段常量，位于脚本资源 `common-f3956634` 的
module 1511。站点在请求中内联该片段；上面的查询已将其展开。

这条单房源查询包含 `building_name`、`tenant_profile`、`min_income` 等字段。
抓取器按需调用该查询补充详情，并使用缓存、请求预算和限速控制请求量。该查询不
替代列表扫描。

#### SetPaymentMethodOnCart

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

#### PlaceOrder

```graphql
mutation PlaceOrder($cartId: String!, $storeId: Int) {
  placeOrder(input: { cart_id: $cartId, store_id: $storeId }) {
    orderV2 { order_number }
    errors { message code }
  }
}
```

#### IdealCheckOut

```graphql
mutation IdealCheckOut($order_id: String!, $plateform: String) {
  idealCheckOut(order_id: $order_id, plateform: $plateform) {
    redirect
  }
}
```

#### GetCheckoutAgreements

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

### 5.2 历史操作与其他备查名称

本文保留以下 `CreateEmptyCart` 和 `AddNewBooking` 原文作为历史参考；当前下单流程
不调用这两个 operation。

#### CreateEmptyCart

```graphql
mutation CreateEmptyCart {
  createEmptyCart
}
```

#### AddNewBooking

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

#### 同一脚本资源中记录的其他 operation

2026-08-19 采集的同一脚本资源还包含以下名称。本文保留这些名称供维护者查询，当前
流程未使用这些 operation。

`GetCart` · `GetCartItems` · `GetLoginUserCart` · `GetCustomerQuery` ·
`GetCustomerAddresses` · `GetAvailablePaymentMethods` · `GetCountries` ·
`GetProducts` · `GetProductsWithOptions` · `GetBlock` · `CategoryUrl` ·
`SitemapData` · `GetAMastyForm` · `SubmitAmastyForm` · `TruncateCart` ·
`SetBillingAddressOnCart` · `PayRegFee` · `PlaceOrderMutation` ·
`PlaceOrderMollie` · `MollieProcessTransaction`
