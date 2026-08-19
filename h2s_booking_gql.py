"""H2S 预订链路的 GraphQL operation —— 逐字照抄站点原文。

**这个文件和 h2s_gql.py 是同一种东西：照抄品，不是设计品。**

H2S 自 2026-08-17 起按 GraphQL **operation 白名单**放行：不在名单里的 operation
一律 ``403 {"error":"...not available through the public API"}``（另见
scrapers.base.OperationNotAllowedError）。白名单比对的是 operationName + 归一化后的
**字段集合**，所以：

- operation 名必须和站点用的一字不差（``GetProductDetail`` 不是 ``GetProduct``）；
- 字段一个都不能增删（``GetProductDetail`` 里那一大坨字段我们大多用不上，但少一个
  就是全量 403，理由同 h2s_gql.py 的 media_gallery）；
- 每个请求都必须带 ``operationName``，缺了同样 403。

**采集方式**（2026-08-19，docs/H2S_BOOKING_OPS.md §2 有完整记录）：这些 operation
被构建时内联进站点 JS chunk 的模板字符串（``common-*.js``），静态读取即可，无需登录、
无需下单。上游改版后重新照抄：到含 ``AddNewBooking`` 的 chunk 里按 ``(0,x.J1)`` 模板
字符串抽取。``image_manager`` 那段是站点侧的片段常量（chunk 里的 module 1511），这里
已经展开进去。

上游若收紧/改动，重新照抄，别手改字段。由 tests/test_h2s_booking_gql.py 守卫。
"""
from __future__ import annotations

#: 照抄自站点，逐字。改它之前先确认上游真改了。
GETPRODUCTDETAIL = """
  query GetProductDetail($filters: ProductAttributeFilterInput) {
     
    products(filter: $filters) {
       aggregations {
                label
                count
                attribute_code
                options {
                    label
                    count
                    value                   
                }
                position
            }  
      items {
        name
        sku
        city
        neighborhood
        living_area
        building_name
        resident_type
        no_of_rooms
        min_income
        floor
        finishing
        flooring
        curtains
        lighting
        price_range {
          minimum_price {
            regular_price {
              value
              currency
            }
             final_price {
            value
            currency
          }
          }
        }
 
 
        private_outside_area
         next_contract_startdate
            current_lottery_subscribers
        allin_excl_text
        maximum_day_selection
        basic_rent
        location_in_building
        lumpsum_service_charge
        inventory                
        caretaker_costs
        start_unit_date
        service_costs_website
        supplies_website
        income_requirements
        tenant_profile
        cleaning_common_areas
        energy_label
        energy_common_areas
        residence_video
        residence_google_maps
        maximum_number_of_persons
        type_of_contract
        allowance_price
        pets_allowed
        parking_status
        storage_available
        minimum_stay
        meta_description
        meta_title
        meta_keyword
        overview
        book_now_text
        short_description {
          html
        }
        description {
          html
        }
        location {
          html
        }
        url_key
        offer_text
        offer_text_two
        available_to_book
        view_from_residence
        deposit
        small_image {
          url
          label
        }
        image_manager {
          
      tour360
      images {
        position
        image
        thumb
      }

        }
      }
      total_count
      page_info {
        page_size
      }
    }
  }
"""
#: 照抄自站点，逐字。改它之前先确认上游真改了。
CREATEEMPTYCART = """
  mutation CreateEmptyCart {
    createEmptyCart
  }
"""
#: 照抄自站点，逐字。改它之前先确认上游真改了。
ADDNEWBOOKING = """
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
          product {
            name
            sku
          }
          prices {
            price {
              value
              currency
            }
          }
        }
      }
    }
  }
"""
#: 照抄自站点，逐字。改它之前先确认上游真改了。
SETPAYMENTMETHODONCART = """
  mutation SetPaymentMethodOnCart(
    $cartId: String!
    $paymentMethod: PaymentMethodInput!
  ) {
    setPaymentMethodOnCart(
      input: { cart_id: $cartId, payment_method: $paymentMethod }
    ) {
      cart {
        selected_payment_method {
          code
          title
        }
      }
    }
  }
"""
#: 照抄自站点，逐字。改它之前先确认上游真改了。
GETCHECKOUTAGREEMENTS = """
  query GetCheckoutAgreements {
    checkoutAgreements {
      name
      content
      checkbox_text
      mode
    }
  }
"""
#: 照抄自站点，逐字。改它之前先确认上游真改了。
PLACEORDER = """
  mutation PlaceOrder($cartId: String!, $storeId: Int) {
    placeOrder(input: { cart_id: $cartId, store_id: $storeId }) {
      orderV2 {
        order_number
      }
      errors {
        message
        code
      }
    }
  }
"""
#: 照抄自站点，逐字。改它之前先确认上游真改了。
IDEALCHECKOUT = """
  mutation IdealCheckOut($order_id: String!, $plateform: String) {
    idealCheckOut(order_id: $order_id, plateform: $plateform) {
      redirect
    }
  }
"""

#: operation 名。白名单缺 operationName 同样 403。
OP_GETPRODUCTDETAIL = "GetProductDetail"
OP_CREATEEMPTYCART = "CreateEmptyCart"
OP_ADDNEWBOOKING = "AddNewBooking"
OP_SETPAYMENTMETHODONCART = "SetPaymentMethodOnCart"
OP_GETCHECKOUTAGREEMENTS = "GetCheckoutAgreements"
OP_PLACEORDER = "PlaceOrder"
OP_IDEALCHECKOUT = "IdealCheckOut"


#: operation 名 → 文档，便于测试遍历核对。
DOCUMENTS = {
    OP_GETPRODUCTDETAIL: GETPRODUCTDETAIL,
    OP_CREATEEMPTYCART: CREATEEMPTYCART,
    OP_ADDNEWBOOKING: ADDNEWBOOKING,
    OP_SETPAYMENTMETHODONCART: SETPAYMENTMETHODONCART,
    OP_GETCHECKOUTAGREEMENTS: GETCHECKOUTAGREEMENTS,
    OP_PLACEORDER: PLACEORDER,
    OP_IDEALCHECKOUT: IDEALCHECKOUT,
}
