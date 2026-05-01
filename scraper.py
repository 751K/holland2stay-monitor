"""
scraper.py — Holland2Stay 房源抓取
====================================
职责
----
通过直接请求 Holland2Stay GraphQL API 抓取房源列表，返回 `Listing` 对象列表。

技术要点
--------
- **Cloudflare 绕过**：使用 `curl_cffi` 的 `impersonate="chrome110"` 在 TLS 层模拟
  Chrome 指纹，无需 headless 浏览器。直接请求 HTML 会得到 403。
- **GraphQL 端点**：`https://api.holland2stay.com/graphql/`（Magento 后端）
  Holland2Stay 前端为 Next.js + Apollo Client CSR，页面 HTML 中无房源数据。
- **自动翻页**：每页最多 100 条，`page_info.total_pages` 控制循环。
- **多城市**：调用方传入 `city_tasks` 列表，本模块对每个城市串行请求。

对外接口
--------
只有一个公开函数 `scrape_all()`，其余均为模块私有。

依赖
----
- `curl_cffi.requests`（外部库，需 pip install）
- `models.Listing`（内部）
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import curl_cffi.requests as req

from models import Listing

logger = logging.getLogger(__name__)

GQL_URL = "https://api.holland2stay.com/graphql/"

# GraphQL 查询模板。
# %s → city/availability filter 字符串（由 _build_filter 生成）
# %d → 当前页码（从 1 开始）
# category_uid "Nw==" 对应 Residences 分类，固定不变。
_GQL_QUERY = """
{
  products(
    filter: {
      category_uid: { eq: "Nw==" }
      %s
    },
    pageSize: 100,
    currentPage: %d
  ) {
    total_count
    page_info { current_page total_pages }
    items {
      name
      sku
      url_key
      price_range { minimum_price { regular_price { value } } }
      custom_attributesV2 {
        items {
          code
          ... on AttributeValue { value }
          ... on AttributeSelectedOptions {
            selected_options { label value }
          }
        }
      }
    }
  }
}
"""

_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.holland2stay.com",
    "Referer": "https://www.holland2stay.com/",
    "Accept": "application/json",
}

# 只提取这些属性，其余忽略，减少处理量。
# 增加新属性时需同时更新 _to_listing() 中的解析逻辑。
_RELEVANT_ATTRS = {
    "available_startdate",   # AttributeValue: "2026-04-08 00:00:00"
    "available_to_book",     # AttributeSelectedOptions: [{label, value}]，决定状态
    "basic_rent",            # AttributeValue: "707.000000"，月租金
    "building_name",         # AttributeSelectedOptions: 楼盘名
    "city",                  # AttributeSelectedOptions: 城市
    "energy_label",          # AttributeValue: "A" / "B"
    "finishing",             # AttributeSelectedOptions: "Upholstered" / "Shell"
    "floor",                 # AttributeSelectedOptions: 楼层数字字符串
    "living_area",           # AttributeValue: "26.0"（m²，无单位）
    "maximum_number_of_persons",  # AttributeSelectedOptions: 入住人数描述
    "neighborhood",          # AttributeValue: 片区名
    "no_of_rooms",           # AttributeSelectedOptions: 房间数 / 户型标签
}


def _build_filter(city_ids: list[str], availability_ids: list[str]) -> str:
    """
    构造 GraphQL filter 字符串片段，嵌入 _GQL_QUERY 的 %s 位置。

    Parameters
    ----------
    city_ids         : 城市 ID 字符串列表，e.g. ["29"]
    availability_ids : 可用性 ID 列表，e.g. ["179", "336"]

    Returns
    -------
    形如::

        city: { in: ["29"] }
        available_to_book: { in: ["179", "336"] }
    """
    city_in = ", ".join(f'"{c}"' for c in city_ids)
    avail_in = ", ".join(f'"{a}"' for a in availability_ids)
    return f'city: {{ in: [{city_in}] }}\n      available_to_book: {{ in: [{avail_in}] }}'


def _parse_attr(attrs: list[dict]) -> dict:
    """
    从 `custom_attributesV2.items` 原始列表中提取感兴趣的属性。

    Parameters
    ----------
    attrs : GraphQL 返回的 custom_attributesV2.items 列表，每项含 code 及以下之一：
            - `value` (AttributeValue)
            - `selected_options` (AttributeSelectedOptions: [{label, value}])

    Returns
    -------
    dict，key 为属性 code，value 为：
        - str（AttributeValue）
        - list[dict]（AttributeSelectedOptions，含 label/value）
    只包含 _RELEVANT_ATTRS 中的属性，其余略过。
    """
    result = {}
    for a in attrs:
        code = a.get("code")
        if code not in _RELEVANT_ATTRS:
            continue
        if "value" in a and a["value"] is not None:
            result[code] = a["value"]
        elif "selected_options" in a:
            result[code] = a["selected_options"]
    return result


def _to_listing(item: dict, city_name: str) -> Optional[Listing]:
    """
    将 GraphQL 返回的单个 product item 转换为 Listing 对象。

    转换规则
    --------
    - id        : url_key 优先，否则用 sku
    - status    : available_to_book[0].label，无数据时为 "Unknown"
    - price_raw : basic_rent 属性格式化为 "€707"；
                  缺失时从 price_range.minimum_price 降级
    - available_from : available_startdate 取前 10 字符（"YYYY-MM-DD"）
    - features  : 按顺序从 8 个属性拼装为 "Key: Value" 字符串列表

    Parameters
    ----------
    item      : GraphQL products.items 中的单个元素
    city_name : 所属城市名（由调用方传入，GraphQL 结果不含此信息）

    Returns
    -------
    Listing 对象；解析异常时记录警告并返回 None（调用方跳过该条）
    """
    try:
        url_key = item.get("url_key", "")
        listing_id = url_key or item.get("sku", "")
        url = f"https://www.holland2stay.com/residences/{url_key}.html"

        attrs = _parse_attr(item.get("custom_attributesV2", {}).get("items", []))

        atb = attrs.get("available_to_book")
        if isinstance(atb, list) and atb:
            status = atb[0]["label"]
        else:
            status = "Unknown"

        rent_raw = attrs.get("basic_rent")
        if rent_raw:
            price_raw = f"€{float(rent_raw):.0f}"
        else:
            try:
                val = item["price_range"]["minimum_price"]["regular_price"]["value"]
                price_raw = f"€{val:.0f}"
            except (KeyError, TypeError):
                price_raw = None

        avail_date = attrs.get("available_startdate")
        available_from = avail_date.split(" ")[0] if avail_date else None

        def label(key: str) -> Optional[str]:
            """取属性的第一个 label（selected_options）或原始字符串值。"""
            v = attrs.get(key)
            if isinstance(v, list) and v:
                return v[0]["label"]
            return v

        features: list[str] = []
        for key, prefix in [
            ("no_of_rooms",              "Type"),
            ("living_area",              "Area"),
            ("maximum_number_of_persons","Occupancy"),
            ("floor",                    "Floor"),
            ("finishing",                "Finishing"),
            ("energy_label",             "Energy"),
            ("neighborhood",             "Neighborhood"),
            ("building_name",            "Building"),
        ]:
            v = label(key)
            if v:
                suffix = " m²" if key == "living_area" else ""
                features.append(f"{prefix}: {v}{suffix}")

        return Listing(
            id=listing_id,
            name=item.get("name") or listing_id,
            status=status,
            price_raw=price_raw,
            available_from=available_from,
            features=features,
            url=url,
            city=city_name,
        )
    except Exception as e:
        logger.warning("解析房源失败: %s", e)
        return None


def _scrape_city_pages(
    session: req.Session,
    city_name: str,
    city_ids: list[str],
    availability_ids: list[str],
) -> list[Listing]:
    """
    对单个城市执行分页抓取，直到取完所有页为止。

    Parameters
    ----------
    session          : 已初始化的 curl_cffi Session（由 scrape_all 创建并复用）
    city_name        : 城市显示名，用于日志和 Listing.city 字段
    city_ids         : 该城市的 GraphQL filter ID 列表（通常只有一个）
    availability_ids : 可用性 filter ID 列表

    Returns
    -------
    该城市所有页面抓到的 Listing 列表。若某页请求失败则停止并返回已有数据。

    注意
    ----
    GraphQL 错误（errors 字段）视为致命错误，立即停止该城市的抓取。
    单条房源解析失败（_to_listing 返回 None）不影响其他条目。
    """
    listings: list[Listing] = []
    current_page = 1

    while True:
        filter_str = _build_filter(city_ids, availability_ids)
        query = _GQL_QUERY % (filter_str, current_page)

        logger.info("[%s] 抓取第 %d 页", city_name, current_page)
        try:
            resp = session.post(
                GQL_URL,
                json={"query": query},
                headers=_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("[%s] 请求失败: %s", city_name, e)
            break

        if "errors" in data:
            logger.error("[%s] GraphQL 错误: %s", city_name, data["errors"])
            break

        products = data.get("data", {}).get("products", {})
        items = products.get("items") or []
        page_info = products.get("page_info", {})
        total_pages = page_info.get("total_pages", 1)

        for item in items:
            listing = _to_listing(item, city_name)
            if listing:
                listings.append(listing)

        logger.info("[%s] 第 %d/%d 页，本页 %d 条", city_name, current_page, total_pages, len(items))

        if current_page >= total_pages:
            break
        current_page += 1

    logger.info("[%s] 共抓取 %d 条房源", city_name, len(listings))
    return listings


def scrape_all(
    city_tasks: list[tuple[str, str]],
    availability_ids: Optional[list[str]] = None,
) -> list[Listing]:
    """
    抓取所有指定城市的房源，返回合并后的列表。

    这是本模块唯一的公开接口。在 monitor.py 中通过
    `run_in_executor` 在线程池里调用（scraper 是同步代码）。

    Parameters
    ----------
    city_tasks       : [(city_name, city_id_str), ...]
                       由 `Config.scrape_tasks()` 生成
                       e.g. [("Eindhoven", "29"), ("Amsterdam", "24")]
    availability_ids : 可用性 filter ID 列表，默认 ["179", "336"]
                       （179=Available to book，336=Available in lottery）

    Returns
    -------
    所有城市抓取结果合并后的 Listing 列表。某城市抓取失败不影响其他城市结果。

    副作用
    ------
    每次调用都创建一个新的 curl_cffi Session（TCP 连接不跨调用复用）。
    """
    if availability_ids is None:
        availability_ids = ["179", "336"]

    all_listings: list[Listing] = []

    with req.Session(impersonate="chrome110") as session:
        for city_name, city_id in city_tasks:
            try:
                listings = _scrape_city_pages(
                    session,
                    city_name,
                    city_ids=[str(city_id)],
                    availability_ids=availability_ids,
                )
                all_listings.extend(listings)
            except Exception as e:
                logger.error("[%s] 抓取失败: %s", city_name, e, exc_info=True)

    return all_listings
