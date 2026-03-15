from __future__ import annotations

import logging
import re
from typing import Optional

import curl_cffi.requests as req

from models import Listing

logger = logging.getLogger(__name__)

GQL_URL = "https://api.holland2stay.com/graphql/"

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

_RELEVANT_ATTRS = {
    "available_startdate",
    "available_to_book",
    "basic_rent",
    "building_name",
    "city",
    "energy_label",
    "finishing",
    "floor",
    "living_area",
    "maximum_number_of_persons",
    "neighborhood",
    "no_of_rooms",
}


def _build_filter(city_ids: list[str], availability_ids: list[str]) -> str:
    city_in = ", ".join(f'"{c}"' for c in city_ids)
    avail_in = ", ".join(f'"{a}"' for a in availability_ids)
    return f'city: {{ in: [{city_in}] }}\n      available_to_book: {{ in: [{avail_in}] }}'


def _parse_attr(attrs: list[dict]) -> dict:
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
    try:
        url_key = item.get("url_key", "")
        listing_id = url_key or item.get("sku", "")
        url = f"https://www.holland2stay.com/residences/{url_key}.html"

        attrs = _parse_attr(item.get("custom_attributesV2", {}).get("items", []))

        # Status
        atb = attrs.get("available_to_book")
        if isinstance(atb, list) and atb:
            status = atb[0]["label"]
        else:
            status = "Unknown"

        # Price: prefer basic_rent attribute, fall back to price_range
        rent_raw = attrs.get("basic_rent")
        if rent_raw:
            price_raw = f"€{float(rent_raw):.0f}"
        else:
            try:
                val = item["price_range"]["minimum_price"]["regular_price"]["value"]
                price_raw = f"€{val:.0f}"
            except (KeyError, TypeError):
                price_raw = None

        # Available from
        avail_date = attrs.get("available_startdate")
        if avail_date:
            available_from = avail_date.split(" ")[0]  # "2026-04-08"
        else:
            available_from = None

        # Build features list
        def label(key: str) -> Optional[str]:
            v = attrs.get(key)
            if isinstance(v, list) and v:
                return v[0]["label"]
            return v  # plain string attrs

        features: list[str] = []
        for key, prefix in [
            ("no_of_rooms", "Type"),
            ("living_area", "Area"),
            ("maximum_number_of_persons", "Occupancy"),
            ("floor", "Floor"),
            ("finishing", "Finishing"),
            ("energy_label", "Energy"),
            ("neighborhood", "Neighborhood"),
            ("building_name", "Building"),
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
    city_tasks: [(city_name, city_id_str), ...]
    availability_ids: list of availability filter IDs, e.g. ["179", "336"]
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
