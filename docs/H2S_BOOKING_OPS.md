# H2S 预订接口与验证状态

本文记录 Holland2Stay 预订实现与 2026-08-19 的站点探测。接口可能变化；运行流程以
[`booker.py`](../booker.py)、[`h2s_booking_gql.py`](../h2s_booking_gql.py) 和
[`browser_fetcher.py`](../browser_fetcher.py) 为准。

## 1. 当前实现

1. 必要时查询 `GetProductDetail`，取得房源 SKU 和入住日期。
2. 通过 NextAuth 的 `/api/auth/csrf`、`/api/auth/callback/credentials` 和
   `/api/auth/session` 登录并取得 access token。
3. 通过加密的 `POST /api/booking` 创建购物车并发起占房。
4. 调用 `SetPaymentMethodOnCart`、`GetCheckoutAgreements`、`PlaceOrder` 和
   `IdealCheckOut`，返回支付链接，由用户自行支付。

`CreateEmptyCart` 和 `AddNewBooking` 的原文保留供研究；当前下单流程不调用它们。
2026-08-19 的探测中，后者被公开 API 拒绝。历史上曾有旧流程真实预订成功；当前
`/api/booking` 路径的完整真实下单尚未验证，不能把旧流程的成功当作该路径的验证。
结果不确定时系统会暂停同账号自动预订，等待用户核实。

## 2. 站点 operation 原文（2026-08-19 采集）

### GetProductDetail

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

这条单房源查询包含 `building_name`、`tenant_profile`、`min_income` 等字段。
抓取侧按需调用它补充详情，并使用缓存、预算及限速控制请求量；它不替代列表扫描。

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

### GetCheckoutAgreements

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

## 6. 登录与传输

NextAuth 使用同源明文 `/api/auth/*` 请求，均由浏览器发送。2026-08-19 真实账号
曾通过登录；如果上游要求双因素认证，程序返回需要人工处理的结果。

### 6.5 尚待验证

- 带真实登录态的 `/api/booking` 完整占房及支付链接流程尚未实测。
- 上游何时要求双因素认证、何时要求额外验证，需在实际条件下重新确认。
- 接口实测具有时间性，不应据历史白名单结果推断当前服务一定放行。

### 6.6 取消预留

租户门户使用 REST：通过 `/api/rest/V1/newdashboard/contract/me` 查询预留，
按 SKU 调用 `POST /api/rest/V1/customer/bookingcancel/{sku}`。项目中的
`cancel_pending_orders` 仅考虑本进程记录的自有预留；无记录则不取消。该取消路径
尚未用真实取消操作验证。是否会调用由预订流程和配置决定。

### 6.9 REST 与 GraphQL 的加密信封

| 路径 | 实现方式 |
|---|---|
| `/api/auth/*` | 明文请求，使用 `fetch_plain` |
| `GET /api/rest/*` | 加密路径和查询，通过 `x-enc-q` 发送至 `/api/rest/__enc__` |
| `POST /api/rest/*` | 加密 body，发送至原 URL |
| GraphQL `/api/__enc__` | 加密 body |
| `POST /api/booking` | 加密 body，携带 Bearer token |

具体信封实现见 `BrowserFetcher.fetch_rest()`、`fetch_plain()` 和加密请求方法。

### 6.10 预订入口与白名单探测

2026-08-19 使用无效参数探测时，`CreateEmptyCart`、`GetCheckoutAgreements`、
`SetPaymentMethodOnCart`、`PlaceOrder` 和 `IdealCheckOut` 返回成功或业务错误；
`AddNewBooking` 返回不在公开 API 中。站点前端使用 `POST /api/booking`，请求体
包括 `sku`、`contract_startDate`、`challengeToken`、`challengeProvider`；当前实现
用返回的 `cartId` 继续后续步骤。

未登录访问 `/api/booking` 曾返回 401，说明当时路由存在，不等于已验证真实占房。
原始 operation 定义见上文及 `h2s_booking_gql.py`；实际运行只调用当前接线的步骤。
