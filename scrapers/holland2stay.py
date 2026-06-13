"""
scrapers/holland2stay.py — Holland2Stay 抓取器（CloakBrowser + 新 GraphQL API）
============================================================================

H2S 已将 API 从 ``api.holland2stay.com/graphql`` 迁移至
``www.holland2stay.com/api/graphql``（与主站同域，Cloudflare WAF 保护）。

旧 curl_cffi 直连路径已被 CF 封锁。新路径使用 CloakBrowser（patched Chromium）
绕过 CF Turnstile，再通过浏览器内 ``page.evaluate(fetch)`` 调用 GraphQL API。

本次同时完成了当初 P0 多源重构遗留的 TODO：将 H2S 爬取主体从 ``scraper.py``
正式搬入本文件，不再通过 ``from scraper import _scrape_city_pages`` 桥接。

新 API 字段变化
--------------
旧（custom_attributesV2 嵌套）::

    items[0].custom_attributesV2.items → [{code, value|selected_options}, ...]

新（扁平字段）::

    items[0].city → 29 (int ID)
    items[0].basic_rent → 1395 (int)
    items[0].energy_label → "A" (string)
    items[0].building_name → 614 (int ID)
    ...

大部分枚举字段返回原始 attribute option ID，需要通过 aggregations 接口
做 ID→label 映射。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

from models import Listing

from .base import (
    RATE_LIMIT_BACKOFF,
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
)

logger = logging.getLogger(__name__)

# 翻页安全上限
_MAX_PAGES = 50

# available_to_book 状态 ID → label
_STATUS_MAP: dict[int, str] = {
    179: "Available to book",
    336: "Available in lottery",
    6253: "Coming soon",
    180: "Occupied",
    6203: "Reserved",
    6204: "To be in lottery",
}

# 新 GraphQL 查询（扁平字段，不再有 custom_attributesV2）
_GQL_QUERY = """
query GetCategories(
  $pageSize: Int!,
  $currentPage: Int!,
  $filters: ProductAttributeFilterInput!,
  $sort: ProductAttributeSortInput
) {
  products(
    pageSize: $pageSize,
    currentPage: $currentPage,
    filter: $filters,
    sort: $sort
  ) {
    total_count
    page_info { current_page total_pages }
    items {
      name
      sku
      url_key
      city
      basic_rent
      living_area
      energy_label
      building_name
      no_of_rooms
      floor
      finishing
      maximum_number_of_persons
      available_to_book
      available_startdate
      next_contract_startdate
      minimum_stay
      type_of_contract
      offer_text_two
      tenant_profile_restrictions
      price_range {
        minimum_price {
          regular_price { value __typename }
          __typename
        }
        __typename
      }
      media_gallery { url label }
    }
  }
}
"""


# ── Attribute 标签查询（一次获取，批次内复用）──────────────────────────

_ATTRS_TO_LABEL = {
    "city",
    "building_name",
    "finishing",
    "floor",
    "maximum_number_of_persons",
    "no_of_rooms",
    "type_of_contract",
    "tenant_profile_restrictions",
}


def _fetch_attr_labels(fetcher: "BrowserFetcher") -> dict[str, dict[str, str]]:
    """
    通过 aggregations 接口获取所有 attribute option ID → label 映射。

    一次查询覆盖所有需要的属性，结果在批次内缓存复用。
    """
    query = """
    query GetAggregations($filters: ProductAttributeFilterInput!) {
      products(filter: $filters) {
        aggregations {
          attribute_code
          options { label value }
        }
      }
    }
    """
    data = fetcher.fetch_gql(query, {"filters": {"category_uid": {"eq": "Nw=="}}})

    labels: dict[str, dict[str, str]] = {}
    try:
        aggs = data["data"]["products"]["aggregations"]
    except (KeyError, TypeError):
        logger.warning("aggregations 响应格式异常，标签映射将降级为原始 ID")
        return labels

    for agg in aggs:
        code = agg.get("attribute_code", "")
        if code not in _ATTRS_TO_LABEL:
            continue
        code_map: dict[str, str] = {}
        for opt in agg.get("options", []):
            val = str(opt.get("value", ""))
            lbl = opt.get("label", "")
            if val and lbl:
                code_map[val] = lbl
        if code_map:
            labels[code] = code_map

    return labels


# ── BrowserFetcher（从共享模块导入）─────────────────────────────────
from browser_fetcher import BrowserFetcher  # noqa: E402


# ── Listing 转换 ────────────────────────────────────────────────────────

def _to_listing(
    item: dict,
    city_name: str,
    attr_labels: dict[str, dict[str, str]],
) -> Optional[Listing]:
    """
    将新 API 返回的单个 product item 转换为 Listing 对象。

    新 API 返回扁平字段（不再有 custom_attributesV2），大部分枚举字段
    返回原始 attribute option ID，需通过 attr_labels 做 ID→label 映射。
    """
    try:
        url_key = item.get("url_key", "")
        listing_id = url_key or item.get("sku", "")
        url = f"https://www.holland2stay.com/residences/{url_key}.html" if url_key else ""

        sku = item.get("sku", "")

        # ── status ──
        atb_id = item.get("available_to_book")
        status = _STATUS_MAP.get(atb_id, f"Unknown({atb_id})") if atb_id is not None else "Unknown"

        # ── price ──
        rent = item.get("basic_rent")
        if rent is not None:
            price_raw = f"€{float(rent):.0f}"
        else:
            try:
                val = item["price_range"]["minimum_price"]["regular_price"]["value"]
                price_raw = f"€{float(val):.0f}"
            except (KeyError, TypeError):
                price_raw = None

        # ── available_from ──
        avail_date = item.get("available_startdate") or ""
        available_from = avail_date.split(" ")[0] if avail_date else None

        # ── contract fields ──
        contract_id: Optional[int] = None
        toc_id = item.get("type_of_contract")
        if toc_id is not None:
            try:
                contract_id = int(toc_id)
            except (ValueError, TypeError):
                pass

        raw_next = item.get("next_contract_startdate") or ""
        contract_start_date = raw_next.strip()[:10] if raw_next.strip() else None

        # ── features ──
        labels = attr_labels

        def _label(attr_code: str, raw_value) -> Optional[str]:
            """将 attribute option ID 解析为可读 label。"""
            if raw_value is None:
                return None
            str_val = str(raw_value)
            code_map = labels.get(attr_code, {})
            return code_map.get(str_val, str_val)  # 映射缺失时返回原始值

        features: list[str] = []

        # Type（no_of_rooms）
        v = _label("no_of_rooms", item.get("no_of_rooms"))
        if v:
            features.append(f"Type: {v}")

        # Area（living_area — 已是 string）
        area = item.get("living_area")
        if area:
            features.append(f"Area: {area} m²")

        # Occupancy
        v = _label("maximum_number_of_persons", item.get("maximum_number_of_persons"))
        if v:
            features.append(f"Occupancy: {v}")

        # Floor
        v = _label("floor", item.get("floor"))
        if v:
            features.append(f"Floor: {v}")

        # Finishing
        v = _label("finishing", item.get("finishing"))
        if v:
            features.append(f"Finishing: {v}")

        # Energy（已是 string）
        energy = item.get("energy_label")
        if energy:
            features.append(f"Energy: {energy}")

        # Building
        v = _label("building_name", item.get("building_name"))
        if v:
            features.append(f"Building: {v}")

        # Offer
        offer = item.get("offer_text_two", "")
        if offer and offer.strip():
            features.append(f"Offer: {offer.strip()}")

        # Contract type
        v = _label("type_of_contract", item.get("type_of_contract"))
        if v:
            features.append(f"Contract: {v}")

        # Tenant profile
        v = _label("tenant_profile_restrictions", item.get("tenant_profile_restrictions"))
        if v:
            features.append(f"Tenant: {v}")

        return Listing(
            id=listing_id,
            name=item.get("name") or listing_id,
            status=status,
            price_raw=price_raw,
            available_from=available_from,
            features=features,
            url=url,
            city=city_name,
            sku=sku,
            contract_id=contract_id,
            contract_start_date=contract_start_date,
        )
    except (TypeError, KeyError, ValueError, AttributeError) as e:
        try:
            uk = item.get("url_key", "?") if isinstance(item, dict) else "?"
        except Exception:
            uk = "?"
        logger.warning(
            "[%s] 解析房源失败 url_key=%s: %s",
            city_name, uk, e,
            exc_info=True,
        )
        return None


# ── 分页抓取 ────────────────────────────────────────────────────────────

def _scrape_city_pages(
    fetcher: BrowserFetcher,
    city_name: str,
    city_ids: list[str],
    availability_ids: list[str],
    attr_labels: dict[str, dict[str, str]],
) -> tuple[list[Listing], bool]:
    """
    对单个城市执行分页抓取，直到取完所有页为止。

    与旧版 _scrape_city_pages 的返回契约完全一致：
    (listings, complete) — complete 语义不变。
    """
    listings: list[Listing] = []
    total_items = 0
    skipped = 0
    current_page = 1
    complete = False

    while True:
        filters: dict = {
            "category_uid": {"eq": "Nw=="},
        }
        if city_ids:
            filters["city"] = {"in": city_ids}
        if availability_ids:
            filters["available_to_book"] = {"in": availability_ids}

        variables = {
            "pageSize": 100,
            "currentPage": current_page,
            "filters": filters,
            "sort": {"available_startdate": "ASC"},
        }

        logger.info("[%s] 抓取第 %d 页", city_name, current_page)
        try:
            data = fetcher.fetch_gql(_GQL_QUERY, variables)
        except (RateLimitError, BlockedError, ScrapeNetworkError):
            raise
        except Exception as e:
            logger.error(
                "[%s] 请求失败 page=%d: %s",
                city_name, current_page, e,
                exc_info=True,
            )
            if current_page == 1:
                raise ScrapeNetworkError(
                    f"[{city_name}] 第 1 页网络错误: {e}"
                ) from e
            break

        if "errors" in data:
            logger.error(
                "[%s] GraphQL 错误 page=%d errors=%s",
                city_name, current_page, data["errors"],
            )
            break

        gql_data = data.get("data")
        if gql_data is None:
            logger.error(
                "[%s] GraphQL 返回 data=null page=%d",
                city_name, current_page,
            )
            if current_page == 1:
                raise ScrapeNetworkError(
                    f"[{city_name}] GraphQL 返回 data=null"
                )
            break

        products = gql_data.get("products", {})
        items = products.get("items") or []
        page_info = products.get("page_info", {})
        total_pages = page_info.get("total_pages", 1)

        for item in items:
            listing = _to_listing(item, city_name, attr_labels)
            if listing:
                listings.append(listing)
            else:
                skipped += 1
        total_items += len(items)

        logger.info(
            "[%s] 第 %d/%d 页，本页 %d 条",
            city_name, current_page, total_pages, len(items),
        )

        if current_page >= total_pages:
            complete = True
            break
        if current_page >= _MAX_PAGES:
            logger.warning(
                "[%s] 触发 _MAX_PAGES=%d 截断，实际 total_pages=%s",
                city_name, _MAX_PAGES, total_pages,
            )
            break
        current_page += 1

    rate = skipped / total_items if total_items else 0
    if rate > 0.05:
        complete = False
        logger.warning(
            "[%s] 解析失败率 %.1f%% 超过 5%%，本轮扫描标记为不完整",
            city_name, rate * 100,
        )
    if skipped:
        logger.warning(
            "[%s] 共抓取 %d/%d 条房源，%d 条解析失败（%.0f%%）",
            city_name, len(listings), total_items, skipped, rate * 100,
        )
    else:
        logger.info("[%s] 共抓取 %d 条房源", city_name, len(listings))
    return listings, complete


# ── Scraper ─────────────────────────────────────────────────────────────

class HollandStayScraper(AbstractScraper):
    """
    Holland2Stay 抓取器（CloakBrowser + 新 GraphQL API）。

    浏览器生命周期
    --------------
    浏览器**跨轮复用**——首轮创建，后续轮复用同一个实例，避免每轮重新执行
    CF Turnstile 挑战（~4s 冷启动 + CF challenge 频率过高会被标记）。
    仅在 BlockedError（CF 会话过期）或进程退出时关闭重建。

    batch_session() 不再创建/关闭浏览器，只负责让 dispatcher 拿到共享实例。
    """

    source = "holland2stay"

    # 浏览器最大存活时间（秒）：超过后主动重建，避免会话过期被 CF 拦
    _BROWSER_MAX_AGE = 7200  # 2 小时

    def __init__(self) -> None:
        self._fetcher: Optional[BrowserFetcher] = None
        self._attr_labels: dict[str, dict[str, str]] = {}
        self._browser_created_at: float = 0.0

    def _ensure_browser(self) -> BrowserFetcher:
        """懒创建或复用浏览器实例。BlockedError 后自动重建。"""
        from config import CLOAKBROWSER_HEADLESS

        now = time.monotonic()
        if self._fetcher is not None:
            # 超龄 → 主动重建
            if now - self._browser_created_at > self._BROWSER_MAX_AGE:
                logger.info("浏览器已存活 %.0f 分钟，主动重建", (now - self._browser_created_at) / 60)
                self._close_browser()
            else:
                return self._fetcher

        # 新建浏览器
        self._fetcher = BrowserFetcher(headless=CLOAKBROWSER_HEADLESS)
        self._fetcher.__enter__()
        self._fetcher.ensure_initialized()
        self._attr_labels = _fetch_attr_labels(self._fetcher)
        self._browser_created_at = time.monotonic()
        logger.info("浏览器已创建并完成 CF 挑战 (第 %d 次)", getattr(self, '_browser_create_count', 0) + 1)
        setattr(self, '_browser_create_count', getattr(self, '_browser_create_count', 0) + 1)
        return self._fetcher

    def _close_browser(self) -> None:
        """关闭浏览器，释放资源。BlockedError 时调用。"""
        if self._fetcher is not None:
            try:
                self._fetcher.__exit__(None, None, None)
            except Exception:
                pass
            self._fetcher = None
            self._attr_labels = {}

    @contextmanager
    def batch_session(self):
        """
        批次上下文：确保浏览器存活，dispatcher 通过此入口拿到共享实例。

        浏览器跨轮复用——不再每批次创建/关闭。CF 挑战只在浏览器首次创建
        或 BlockedError 后重建时执行。
        """
        try:
            self._ensure_browser()
            yield
        except BlockedError:
            # CF 会话被标记：关闭当前浏览器，下轮重建
            logger.warning("抓取遇 BlockedError，关闭浏览器（下轮将重建 CF 会话）")
            self._close_browser()
            raise

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        availability_ids = task.extra.get("availability_ids") or ["179", "336"]

        if self._fetcher is not None:
            # 批次内：复用共享浏览器
            listings, complete = _scrape_city_pages(
                self._fetcher,
                task.city_display,
                city_ids=[task.city_key],
                availability_ids=availability_ids,
                attr_labels=self._attr_labels,
            )
        else:
            # 独立调用（单测 / 调试 / 非 dispatcher 路径）
            from config import CLOAKBROWSER_HEADLESS

            with BrowserFetcher(headless=CLOAKBROWSER_HEADLESS) as fetcher:
                fetcher.ensure_initialized()
                labels = _fetch_attr_labels(fetcher)
                listings, complete = _scrape_city_pages(
                    fetcher,
                    task.city_display,
                    city_ids=[task.city_key],
                    availability_ids=availability_ids,
                    attr_labels=labels,
                )

        for l in listings:
            l.source = self.source

        logger.info(
            "[%s] Holland2Stay 共抓取 %d 条房源%s",
            task.city_display,
            len(listings),
            " (完整)" if complete else "",
        )
        return ScrapeResult(
            task=task,
            listings=listings,
            complete=complete,
        )

    def prewarm_session(self) -> None:
        """
        H2S 自动预订登录预热 — 暂未适配新 API。

        booker.py 下单路径也需迁移到 CloakBrowser（独立 follow-up）。
        当前 no-op，不影响抓取。
        """
        return None
