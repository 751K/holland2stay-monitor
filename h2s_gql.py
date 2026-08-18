"""H2S 的 GraphQL 白名单文档。

**这段查询必须与站点自己发的逐字段一致，一个字段都不能增删。**

2026-08-18 起 H2S 按 operation 白名单放行，不在名单里的查询一律返回
``403 {"code":"operation_not_allowed"}``。实测判据：

    原文                              200
    删掉 image_manager 块              403
    加 tenant_profile_restrictions     403
    加 available_startdate             403
    只改空格                           200   ← 空白不敏感，字段集敏感

也就是说白名单比对的是归一化后的**字段集合**。我们此前为省流量裁剪过查询
（删掉 media_gallery 等），正是那份裁剪版在 2026-08-18 08:11 被全量拒绝，
H2S 抓取中断。

因此本文件是**照抄品，不是设计品**。不要「优化」它：
- 想省流量，去调查询频率与筛选条件（见 scrapers/holland2stay.py 的分层抓取），
  那些走 variables，白名单不管。
- 上游改版后需要重新照抄：钩住 ``crypto.subtle.encrypt`` 截获站点加密前的明文
  即可拿到，步骤见 docs/H2S.md §2。

取自 2026-08-18 的线上明文。
"""
from __future__ import annotations

#: 白名单登记的 operation 名。缺了它同样 403。
OPERATION_NAME = "GetCategories"

#: 站点原文，逐字照抄。
GQL_QUERY = """\
query GetCategories($pageSize: Int!, $currentPage: Int!, $filters: ProductAttributeFilterInput!, $sort: ProductAttributeSortInput) {
  products(
    pageSize: $pageSize
    currentPage: $currentPage
    filter: $filters
    sort: $sort
  ) {
    ...ProductsFragment
    __typename
  }
}

fragment ProductsFragment on Products {
  sort_fields {
    options {
      label
      value
      __typename
    }
    __typename
  }
  aggregations {
    label
    count
    attribute_code
    options {
      label
      count
      value
      __typename
    }
    position
    __typename
  }
  items {
    name
    sku
    city
    url_key
    available_to_book
    next_contract_startdate
    current_lottery_subscribers
    finishing
    living_area
    no_of_rooms
    offer_text_two
    offer_text
    maximum_number_of_persons
    type_of_contract
    price_analysis_text
    allowance_price
    floor
    basic_rent
    price_range {
      minimum_price {
        regular_price {
          value
          __typename
        }
        __typename
      }
      __typename
    }
    energy_label
    minimum_stay
    media_gallery {
      url
      label
      position
      disabled
      __typename
    }
    image_manager {
      tour360
      images {
        position
        image
        thumb
        __typename
      }
      __typename
    }
    __typename
  }
  page_info {
    total_pages
    __typename
  }
  total_count
  __typename
}
"""
