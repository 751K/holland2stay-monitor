"""
scrapers/holland2stay.py — Holland2Stay 抓取器（CloakBrowser + 新 GraphQL API）
============================================================================

H2S 的 GraphQL 端点已迁移两次，当前为
``www.holland2stay.com/api/service/residences``（与主站同域，Cloudflare WAF
保护）。路径常量在 ``browser_fetcher._H2S_GQL_PATH``，迁移史与判据见那里。

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
    OperationNotAllowedError,
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

# ── 分层抓取：省流量的唯一旋钮 ────────────────────────────────────────
#
# 白名单锁死了字段集（见 h2s_gql.py），响应体没得裁。能动的只有「查什么」。
#
# 2026-08-18 实测（两城合计，线上真实字节，加密响应不可压缩）：
#
#     只查 可订 + 抽签 + 即将上线      2.2 KB/轮     ← 当前 0 条房源
#     再加上 Reserved               292.2 KB/轮    ← 那 48 条全在这儿
#
# 贵的不是「有没有新房」，是那批已被预订的房源——它们每轮被完整拉一遍，而
# Reserved 状态几乎不动。所以拆成两层：
#
#   每轮      只查 _FRESH_STATUSES。新房源必然先出现在这里，通知不延迟。
#   低频      加上 _ARCHIVE_STATUSES 做全量，用于状态流转与库存统计。
#
# 关键：**高频轮不能参与 stale 收敛**。它看不见 Reserved 房源，若标成完整
# 扫描，那批房源会被判定为「已下架」而被清掉。所以高频轮一律 complete=False，
# 只有全量轮才可能为 True。
_FRESH_STATUSES = ("179", "336", "6253")     # 可订 / 抽签 / 即将上线
_ARCHIVE_STATUSES = ("6203", "6204")         # Reserved / 待抽签

#: 全量扫描间隔（秒）。Reserved 的状态分辨率粗到这个粒度，够用；
#: Reserved→可订 的转变仍会在下一个高频轮里立刻出现，不受影响。
_FULL_SCAN_INTERVAL = 1800.0


# GraphQL 查询与 operation 名从 h2s_gql 导入——**那份是照抄品，不能改**。
#
# H2S 自 2026-08-18 起按 operation 白名单放行，字段增删一个即
# 403 operation_not_allowed。我们此前为省流量裁剪过查询（删掉 media_gallery
# 等），正是那份裁剪版当天被全量拒绝，抓取中断。理由与实测判据见 h2s_gql.py。
#
# 因此本文件里**不要**再定义查询。省流量改走「查什么」而不是「要哪些字段」：
# 见 _AVAILABILITY_TIERS 的分层抓取。
from h2s_gql import GQL_QUERY as _GQL_QUERY, OPERATION_NAME as _GQL_OPERATION


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


def _labels_from_aggregations(products: dict) -> dict[str, dict[str, str]]:
    """从 GetCategories 响应自带的 ``aggregations`` 里取 ID → label 映射。

    以前这里单独发一条 ``GetAggregations`` 查询。2026-08-18 起 H2S 按 operation
    白名单放行，那条自定义查询直接 403——好在白名单这条 ``GetCategories`` 的
    ProductsFragment 本来就带 ``aggregations``，同一个响应里就能取，反而省掉
    一次请求。

    注意 aggregations 是**按当前 filters 统计**的：只查「可订」时，取值为
    Reserved 的房源不在统计里，其 label 也就不会出现。所以映射要跨轮累积，
    见 ``HollandStayScraper._attr_labels`` 的合并逻辑。
    """
    labels: dict[str, dict[str, str]] = {}
    aggs = (products or {}).get("aggregations")
    if not aggs:
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


# ── 详情补齐（GetProductDetail）─────────────────────────────────────
#
# 白名单那条 GetCategories 的字段集里没有 building_name / tenant_profile /
# neighborhood / min_income（docs/H2S.md §5.2），加进去就是全量 403。
# 但站点另有一条**同样在白名单里**的 GetProductDetail，字段集大得多，全都有。
# 2026-08-19 实测：200 放行；裁剪它的字段集同样 403（按字段集判，与 GetCategories
# 一条规律）。
#
# 代价：单条约 11.4 KB（aggregations 4 KB + item 7 KB），且**没有分页变量**
# （page_size 固定 20）。所以只能按需单取，绝不能拿它替代列表抓取——
# 20 条就是 160 KB，全量替换会把 v1.16.6 省下的流量原样吐回去。
#
# 策略：进程内缓存 + 每轮预算上限。同一房源只取一次；冷启动分摊到若干轮补齐。
from h2s_booking_gql import (  # noqa: E402
    GETPRODUCTDETAIL as _GQL_DETAIL,
    OP_GETPRODUCTDETAIL as _OP_DETAIL,
)

#: url_key → 补齐出来的 feature 片段。进程级，重启后重建（每次重建 ≈ 房源数 × 11 KB，
#: 按下面的预算分摊到多轮，不会在单轮里炸开）。
_DETAIL_CACHE: dict[str, dict[str, str]] = {}

#: 单次 scrape 最多补齐几条。缓存命中后稳态是 0，这个数只影响**冷启动**。
#:
#: ⚠️ 不能设得太小，原因不是性能而是**正确性**：每轮抓取都会重建 Listing，而
#: 列表查询里没有 building_name / tenant_profile，所以未补齐的房源写库时会把
#: 上一轮已经存进去的值**抹掉**（storage 每轮整体覆盖 features）。预算若盖不满
#: 一轮的房源数，就会出现「补上一批、抹掉另一批」的来回拉锯。
#:
#: 实测每城每轮约 6–45 条，60 能一次盖满，之后稳态 0。留着上限兜住
#: 「上游忽然返回几百条」的异常情况。
#:
#: ⚠️ 光有条数上限不够：2026-08-20 生产实测，一口气连发 46 条详情请求会被 H2S
#: 打 429（46 条里 24 条失败）。**限制速率比限制条数更重要**——见下面两个常量。
_DETAIL_BUDGET_PER_ROUND = 60

#: 两次详情请求之间的间隔。429 是按速率触发的，不是按总量。
#: 0.6s × 46 条 ≈ 28s，摊在本来就要几十秒的 H2S 抓取里可以接受；冷启动铺满后
#: 全是缓存命中，这个开销就归零。
_DETAIL_REQUEST_SPACING = 0.6

#: 撞到 429 就本轮收手。继续打只会加重限流，而且这些房源下轮还有机会——
#: 补齐本来就是跨轮渐进的。没有这个开关时，46 条里有 24 条在反复撞墙。
_DETAIL_STOP_ON_RATE_LIMIT = True

#: tenant_profile 的 option ID → 语义。2026-08-19 从站点**详情页正文**逐条实测确定
#: （不是猜的，也不是从下单向导的选项推断的——那次推断错了）：
#:     6213  "Important: Students only"
#:     6214  "You can book this residence as a working professional"
#:     6215  "You can book this residence as a student or a working professional"
#: 取值语义与 OurDomain / Xior 的 Tenant 维度对齐（见 config.SOURCE_ASSUMED_FEATURES）。
#: tenant_profile 不在 aggregations 里（实测），也不是可筛选属性（filter 报错），
#: 所以只能靠这张写死的表。上游若新增取值，未知 ID 原样跳过而不是瞎猜。
_TENANT_PROFILE_LABELS: dict[str, str] = {
    "6213": "student only",
    "6214": "employed only",
    "6215": "student or employed",
}


def _detail_features(item: dict, aggregations: list | None) -> dict[str, str]:
    """从 GetProductDetail 的单条响应里抽出我们要的几个 feature。

    ``building_name`` 回的是 attribute option ID；同一响应的 aggregations 里带着
    该 ID 的 label，而且因为 filter 收窄到了单个 url_key，那个 aggregation 通常
    只剩这一条选项——正好精确对应，不需要跨轮累积。
    """
    out: dict[str, str] = {}

    # building_name：ID → label（走本次响应自带的 aggregations）
    bid = item.get("building_name")
    if bid is not None:
        for agg in (aggregations or []):
            if agg.get("attribute_code") != "building_name":
                continue
            for opt in agg.get("options", []):
                if str(opt.get("value", "")) == str(bid) and opt.get("label"):
                    out["Building"] = opt["label"]
                    break
            break

    # tenant_profile：ID → 语义（写死表，未知 ID 跳过）
    tp = item.get("tenant_profile")
    if tp is not None:
        label = _TENANT_PROFILE_LABELS.get(str(tp))
        if label:
            out["Tenant"] = label

    # neighborhood 直接就是字符串
    hood = (item.get("neighborhood") or "").strip()
    if hood:
        out["Neighborhood"] = hood

    # min_income 是「月租的几倍」，站点就这么表述
    inc = str(item.get("min_income") or "").strip()
    if inc:
        out["MinIncome"] = f"{inc}x rent"

    return out


def _fetch_detail(fetcher: "BrowserFetcher", url_key: str) -> dict[str, str]:
    """取单条房源详情并抽出补齐字段。失败返回空 dict（fail-open，不影响抓取）。"""
    data = fetcher.fetch_gql(
        _GQL_DETAIL,
        variables={"filters": {"url_key": {"eq": url_key}}},
        operation_name=_OP_DETAIL,
    )
    if "errors" in data and not data.get("data"):
        msgs = "; ".join(e.get("message", "") for e in data["errors"])
        raise RuntimeError(f"GetProductDetail 错误: {msgs}")
    products = (data.get("data") or {}).get("products") or {}
    items = products.get("items") or []
    if not items:
        return {}
    return _detail_features(items[0], products.get("aggregations"))


def _enrich(fetcher: "BrowserFetcher", listings: list[Listing]) -> int:
    """给还没补齐过的房源补上 Building / Tenant / Neighborhood / MinIncome。

    返回本次真正发出去的请求数。**fail-open**：任何一条失败都只记日志跳过，
    不让「补齐」这种锦上添花的事拖垮主抓取。
    """
    spent = 0
    failed = 0
    rate_limited = False
    first_err: str = ""
    for l in listings:
        extra = _DETAIL_CACHE.get(l.id)
        if extra is None:
            if spent >= _DETAIL_BUDGET_PER_ROUND:
                continue
            if spent:
                time.sleep(_DETAIL_REQUEST_SPACING)
            try:
                extra = _fetch_detail(fetcher, l.id)
                spent += 1
            except RateLimitError as e:
                # 速率触顶。继续打只会加重限流；这些房源下轮还有机会。
                rate_limited = True
                failed += 1
                if not first_err:
                    first_err = f"RateLimitError: {e}"
                if _DETAIL_STOP_ON_RATE_LIMIT:
                    break
                continue
            except Exception as e:
                # 只跳过这一条，不缓存失败——下轮还有机会。
                # **但要吵一声**：fail-open + debug 日志 = 静默半残，
                # 补齐悄悄只成功一半时从日志上完全看不出来（实测踩过）。
                failed += 1
                if not first_err:
                    first_err = f"{type(e).__name__}: {e}"
                continue
            _DETAIL_CACHE[l.id] = extra
        if not extra:
            continue
        # 已有同名 feature 的不覆盖：列表查询给的值优先（它是每轮新鲜的）
        have = {f.split(":", 1)[0] for f in l.features if ":" in f}
        for k, v in extra.items():
            if k not in have:
                l.features.append(f"{k}: {v}")
    if failed:
        logger.warning(
            "详情补齐 %d 条失败（成功 %d，本轮共 %d 条房源）%s。"
            "失败的房源这轮没有 Building/Tenant，会被 fail-closed 的租客筛选拒掉。"
            "首个错误: %s",
            failed, spent, len(listings),
            "，已因 429 本轮收手（下轮继续）" if rate_limited else "",
            first_err,
        )
    return spent


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
        #
        # 首选 available_startdate；它自 2026-08-18 起不在白名单查询的字段集里
        # （加进去会全量 403，见 h2s_gql.py），所以实际走的是下面那条。留着它是
        # 因为上游哪天把字段放回来，这里不必再改。
        #
        # 退而用 next_contract_startdate：**站点自己就是拿它渲染「Available per
        # …」的**。详情页 HTML 里那个日期就是这个字段——一度以为要另开一条 SSR
        # 解析的路，实际我们本来就有。
        #
        # 2050-01-01 是「没有下一个合同起始日」的哨兵，不是真日期。实测
        # Reserved 里约一半是它，抽签里一个都没有。按年份判而不是精确匹配那一天：
        # 哨兵值换个写法（2099、2050-12-31）时不至于原样透出去。
        avail_date = item.get("available_startdate") or item.get("next_contract_startdate") or ""
        available_from = avail_date.split(" ")[0] if avail_date else None
        if available_from and available_from[:4].isdigit() and int(available_from[:4]) >= 2050:
            available_from = None

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
            data = fetcher.fetch_gql(
                _GQL_QUERY, variables, operation_name=_GQL_OPERATION,
            )
        except (
            RateLimitError,
            BlockedError,
            OperationNotAllowedError,
            ScrapeNetworkError,
        ):
            # OperationNotAllowedError 必须原样上抛。落进下面那条 except Exception
            # 会被改判成「第 1 页网络错误」——和 2026-08-11 端点迁移时那句
            # 「请检查代理/网络」是同一类误诊：把「查询没登记」说成网络问题，
            # 排查会往代理方向走，而代理是好的。
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

        products = gql_data.get("products") or {}
        items = products.get("items") or []
        page_info = products.get("page_info") or {}
        total_pages = page_info.get("total_pages")

        # 标签映射就在这个响应里，顺手并进去（以前是单独一条 GetAggregations
        # 查询，2026-08-18 起被 operation 白名单挡掉）。
        #
        # **合并而不是覆盖**：aggregations 是按当前 filters 统计的，只查
        # 「可订」那一轮里 Reserved 房源的 label 根本不出现。覆盖会让上一轮
        # 攒到的映射丢失，features 里就会冒出裸 ID。
        for _code, _map in _labels_from_aggregations(products).items():
            attr_labels.setdefault(_code, {}).update(_map)

        # total_pages 缺失以前默认成 1，于是 current_page(1) >= 1 直接判
        # complete=True——**「没拿到数据」被当成了「确认没有数据」**，正是那次
        # 7 周静默故障的判据类型。字段改名 / schema 变更时会得到「0 条房源 +
        # 完整扫描」，而这恰好是让 stale 收敛清空整城的组合。
        #
        # 拿不到就标不完整：已抓到的部分照常入库，只是不参与状态收敛。
        if total_pages is None:
            logger.error(
                "[%s] GraphQL 响应缺少 products.page_info.total_pages page=%d，"
                "本轮标记为不完整（响应结构可能已变更）: %.300s",
                city_name, current_page, gql_data,
            )
            break

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

    关闭重建发生在三种情况：
    - 超过 ``_BROWSER_MAX_AGE``（2 小时）→ ``_ensure_browser`` 主动重建
    - 本批次出现过 403 或未预期异常 → dispatcher 调 ``invalidate_session()``
    - 进程退出

    batch_session() 不创建/关闭浏览器，只负责让 dispatcher 拿到共享实例。
    """

    source = "holland2stay"

    # 浏览器最大存活时间（秒）：超过后主动重建，避免会话过期被 CF 拦
    _BROWSER_MAX_AGE = 7200  # 2 小时

    def __init__(self) -> None:
        self._fetcher: Optional[BrowserFetcher] = None
        self._attr_labels: dict[str, dict[str, str]] = {}
        self._browser_created_at: float = 0.0
        # 上次全量扫描的时刻。**不跟着浏览器重建清零**——浏览器 2 小时一换，
        # 清零会让每次换浏览器都强制一次全量，分层就白做了。
        self._last_full_scan_at: float = 0.0
        #: 本批次是否做全量扫描。由 _begin_batch 每轮算一次，见 _plan_scan。
        #: 独立调用（非 dispatcher）路径没有批次，默认全量，行为与旧版一致。
        self._full_this_batch: bool = True

    def _ensure_browser(self) -> BrowserFetcher:
        """懒创建或复用浏览器实例。

        只在两种情况下真正重建：实例还没有，或已超过 ``_BROWSER_MAX_AGE``。
        抓取期的 403 由 dispatcher 在批次结束后调 ``invalidate_session()``
        丢弃会话，下一轮再走到这里时自然重建。
        """
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
        try:
            self._fetcher.__enter__()
            self._fetcher.ensure_initialized()
            # 标签映射不再单独查（GetAggregations 已被 operation 白名单挡掉），
            # 改由每次 GetCategories 响应自带的 aggregations 累积，见
            # _labels_from_aggregations。这里只备好空表。
            self._attr_labels = {}
            self._browser_created_at = time.monotonic()
            logger.info("浏览器已创建并完成 CF 挑战 (第 %d 次)", getattr(self, '_browser_create_count', 0) + 1)
            setattr(self, '_browser_create_count', getattr(self, '_browser_create_count', 0) + 1)
            return self._fetcher
        except Exception:
            self._close_browser()
            raise

    def _close_browser(self) -> None:
        """关闭浏览器，释放资源。由 ``invalidate_session()`` 和超龄重建调用。"""
        if self._fetcher is not None:
            try:
                self._fetcher.__exit__(None, None, None)
            except Exception:
                pass
            self._fetcher = None
            self._attr_labels = {}

    def invalidate_session(self) -> None:
        """未预期异常后丢弃浏览器——坏掉的会话留着会让后续每轮重复失败。"""
        self._close_browser()

    @contextmanager
    def batch_session(self):
        """
        批次上下文：确保浏览器存活，dispatcher 通过此入口拿到共享实例。

        浏览器跨轮复用——不再每批次创建/关闭。

        这里**不**捕获抓取期的异常。dispatcher 是按 task 隔离的，``scrape()``
        抛的东西根本到不了 ``yield``——曾经写在这里的 ``except BlockedError:
        self._close_browser()`` 因此是死代码，「403 后关闭浏览器下轮重建」
        实际从未发生过。现在由 dispatcher 在批次结束后统一调
        ``invalidate_session()``。
        """
        self._ensure_browser()
        self._begin_batch()
        yield

    def _plan_scan(self, configured: list[str]) -> tuple[list[str], bool]:
        """本轮查哪些可用状态，以及这轮算不算全量。见 _FRESH_STATUSES 上方注释。

        ``configured`` 是用户配置的可用状态白名单——分层不能越过它去查用户
        没要的状态，否则库里会冒出用户根本不想看的房源。

        **决策按批次做，不按城市。** 一轮里每个城市各调一次 scrape()，若在这里
        直接推进计时器，第一个城市会把「该做全量」消耗掉，后面的城市统统被降级
        ——那一轮就只有第一个城市是全量，其余全是高频层。批次标记由
        ``batch_session()`` 在进入时算一次。
        """
        want = set(configured)
        fresh = [s for s in _FRESH_STATUSES if s in want]
        archive = [s for s in _ARCHIVE_STATUSES if s in want]
        if not archive:
            # 用户本来就没要 Reserved 这类，没有可省的，每轮都是全量
            return configured, True
        if not fresh:
            # 用户只要 Reserved 这类：没有高频层可走，退回全量，否则永远查不到
            return configured, True
        if self._full_this_batch:
            return configured, True
        return fresh, False

    def _begin_batch(self) -> None:
        """每批次开头决定这一轮是不是全量扫描。"""
        now = time.monotonic()
        self._full_this_batch = (now - self._last_full_scan_at) >= _FULL_SCAN_INTERVAL
        if self._full_this_batch:
            self._last_full_scan_at = now

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        configured = task.extra.get("availability_ids") or ["179", "336"]
        availability_ids, is_full = self._plan_scan(list(configured))
        if not is_full:
            logger.info(
                "[%s] 高频轮：只查 %s（省去 Reserved 全量），%.0f 秒后做全量",
                task.city_display, ",".join(availability_ids),
                max(0.0, _FULL_SCAN_INTERVAL - (time.monotonic() - self._last_full_scan_at)),
            )

        enriched = 0
        if self._fetcher is not None:
            # 批次内：复用共享浏览器
            listings, complete = _scrape_city_pages(
                self._fetcher,
                task.city_display,
                city_ids=[task.city_key],
                availability_ids=availability_ids,
                attr_labels=self._attr_labels,
            )
            enriched = _enrich(self._fetcher, listings)
        else:
            # 独立调用（单测 / 调试 / 非 dispatcher 路径）
            from config import CLOAKBROWSER_HEADLESS

            with BrowserFetcher(headless=CLOAKBROWSER_HEADLESS) as fetcher:
                fetcher.ensure_initialized()
                labels: dict[str, dict[str, str]] = {}
                listings, complete = _scrape_city_pages(
                    fetcher,
                    task.city_display,
                    city_ids=[task.city_key],
                    availability_ids=availability_ids,
                    attr_labels=labels,
                )
                enriched = _enrich(fetcher, listings)

        if not is_full:
            # 高频轮看不见 Reserved，标成完整扫描会让那批房源被 stale 收敛
            # 判成「已下架」清掉。见 _FRESH_STATUSES 上方注释。
            complete = False

        for l in listings:
            l.source = self.source

        logger.info(
            "[%s] Holland2Stay 共抓取 %d 条房源%s%s",
            task.city_display,
            len(listings),
            " (完整)" if complete else "",
            f"，详情补齐 {enriched} 条（缓存 {len(_DETAIL_CACHE)}）" if enriched else "",
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
