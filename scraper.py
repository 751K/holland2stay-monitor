"""
scraper.py — Holland2Stay 房源抓取
====================================
职责
----
通过直接请求 Holland2Stay GraphQL API 抓取房源列表，返回 `Listing` 对象列表。

技术要点
--------
- **Cloudflare 绕过**：使用 `curl_cffi` 的 `impersonate=get_impersonate()` 在 TLS 层模拟随机指纹
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
import time

import curl_cffi.requests as req

from config import get_impersonate, get_proxy_url
from models import Listing

logger = logging.getLogger(__name__)

GQL_URL = "https://api.holland2stay.com/graphql/"


class RateLimitError(Exception):
    """
    Holland2Stay API 持续返回 429 Too Many Requests，所有重试均已耗尽。

    由 _post_gql() 抛出，经 _scrape_city_pages() / scrape_all() 上传，
    最终由 monitor.py 的 main_loop 捕获并触发冷却期。
    """


class BlockedError(Exception):
    """
    Holland2Stay API 返回 403 — 通常是 Cloudflare WAF 屏蔽。

    与 429 的区别
    -------------
    429 = "请求太快，等等就好"，退避后通常自动恢复。
    403 = "我们不想服务你"，等待不会自动恢复，需要：
      - 更换 HTTPS_PROXY 出口 IP（住宅代理换池）
      - 重启 monitor（重建 curl_cffi session + 新 TLS 指纹）
      - 临时关闭 monitor 让 Cloudflare 冷却该 IP/指纹组合

    现象识别：响应体含 `<!DOCTYPE html>` + `no-js ie6 oldie` 等 Cloudflare
    挑战页标志（HTML，不是预期的 JSON）。
    """


class ScrapeNetworkError(Exception):
    """
    抓取第一页时遭遇网络错误（连接超时、TLS 中断、DNS 失败等），
    非 API 层错误——换代理/检查网络即可恢复。

    与 RateLimitError / BlockedError 的区别
    ---------------------------------------
    - RateLimitError → API 说"太快"（429），退避后可自动恢复
    - BlockedError   → API 说"不服务你"（403），等待无法恢复
    - ScrapeNetworkError → 根本没拿到 API 响应——代理挂了、网络断了、DNS 故障

    由 _scrape_city_pages() 在第一页网络失败时抛出，经 scrape_all() 上传，
    最终由 monitor.py 的 main_loop 做连续失败计数并在超过阈值后冷却。
    """


# 429 退避策略：依次等待这些秒数后重试。
# 两次重试 = 最多额外等待 90 秒后才放弃并抛出 RateLimitError。
_RATE_LIMIT_BACKOFF: tuple[int, ...] = (30, 60)

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

_MAX_PAGES = 50  # 安全上限：防止 API 返回异常 total_pages 导致无限翻页


def _mask_proxy_url(url: str) -> str:
    """脱敏代理 URL 中的密码（日志安全）。"""
    import re as _re
    return _re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", url)


def _post_gql(session: req.Session, query: str) -> dict:
    """
    发送单次 GraphQL POST 请求，遇 429 自动退避重试。

    重试策略
    --------
    依次等待 _RATE_LIMIT_BACKOFF 中各值后重试，全部耗尽仍 429 则抛 RateLimitError。
    sleep 在 executor 线程中执行，不阻塞 asyncio 事件循环。

    Returns
    -------
    resp.json() 返回的完整 dict（含 data / errors 字段，由调用方检查）

    Raises
    ------
    RateLimitError  重试耗尽仍 429
    BlockedError    返回 403（Cloudflare WAF 屏蔽，等待无法恢复）
    HTTPError       其他 4xx/5xx
    Exception       网络超时、JSON 解析失败等
    """
    total_wait = 0
    for attempt, wait in enumerate([0] + list(_RATE_LIMIT_BACKOFF)):
        if wait:
            total_wait += wait
            logger.warning(
                "429 Too Many Requests，第 %d/%d 次退避，等待 %d 秒（累计 %ds）",
                attempt, len(_RATE_LIMIT_BACKOFF), wait, total_wait,
            )
            time.sleep(wait)
        try:
            resp = session.post(GQL_URL, json={"query": query}, headers=_HEADERS, timeout=30)
        except Exception as e:
            # 网络异常（连接超时、TLS 失败、读超时等）— 含 traceback 入 errors.log
            logger.error(
                "GraphQL POST 网络异常 attempt=%d url=%s timeout=30s: %s",
                attempt, GQL_URL, e,
                exc_info=True,
            )
            raise
        # 403 → Cloudflare WAF 屏蔽，等待无法恢复，立刻抛 BlockedError。
        # 与 429（重试可能恢复）不同，403 需要换代理/重启来切 IP 或指纹。
        if resp.status_code == 403:
            body = resp.text[:500]
            is_cf = (
                "cloudflare" in body.lower()
                or "no-js ie6 oldie" in body
                or "challenge-platform" in body.lower()
                or "<!DOCTYPE html>" in body[:50]
            )
            logger.error(
                "GraphQL POST HTTP 403 (%s) url=%s body=%r",
                "Cloudflare WAF" if is_cf else "其他 403", GQL_URL, body[:200],
            )
            reason = "Cloudflare WAF 屏蔽" if is_cf else "API 拒绝服务"
            raise BlockedError(
                f"{reason}（HTTP 403）。等待无法恢复。请尝试："
                f"1) 更换 HTTPS_PROXY 出口 IP；"
                f"2) 重启 monitor（重建 curl_cffi session + TLS 指纹）；"
                f"3) 暂停几小时让 Cloudflare 冷却。"
            )
        if resp.status_code == 429:
            continue          # 触发下一次重试
        if not resp.ok:
            # 非 429 的 4xx/5xx：记录 status + 响应片段（截断防止超大日志）
            logger.error(
                "GraphQL POST HTTP %d attempt=%d url=%s response=%r",
                resp.status_code, attempt, GQL_URL, resp.text[:300],
            )
        resp.raise_for_status()
        return resp.json()

    raise RateLimitError(
        f"API 持续返回 429（已退避重试 {len(_RATE_LIMIT_BACKOFF)} 次，"
        f"累计等待 {total_wait}s）。"
        "请降低轮询频率（CHECK_INTERVAL / PEAK_INTERVAL）或配置 HTTPS_PROXY。"
    )


# 只提取这些属性，其余忽略，减少处理量。
# 增加新属性时需同时更新 _to_listing() 中的解析逻辑。
_RELEVANT_ATTRS = {
    "available_startdate",   # AttributeValue: "2026-04-08 00:00:00"
    "available_to_book",     # AttributeSelectedOptions: [{label, value}]，决定状态
    "basic_rent",            # AttributeValue: "707.000000"，基础租金（不含服务费）
    "price",                 # AttributeValue: "1654.000000"，总租金（含所有附加费）
    "building_name",         # AttributeSelectedOptions: 楼盘名
    "city",                  # AttributeSelectedOptions: 城市
    "energy_label",          # AttributeValue: "A" / "B"
    "finishing",             # AttributeSelectedOptions: "Upholstered" / "Shell"
    "floor",                 # AttributeSelectedOptions: 楼层数字字符串
    "living_area",           # AttributeValue: "26.0"（m²，无单位）
    "maximum_number_of_persons",  # AttributeSelectedOptions: 入住人数描述
    "neighborhood",          # AttributeValue: 片区名
    "next_contract_startdate",    # AttributeValue: "2026-06-01"，预订专用入住日期
    "no_of_rooms",           # AttributeSelectedOptions: 房间数 / 户型标签
    "offer_text_two",        # AttributeValue: "Short-stay" / 空，区分短租和长租
    "tenant_profile",        # AttributeSelectedOptions: [{label, value}]，租客要求
    "type_of_contract",      # AttributeSelectedOptions: [{label, value}]，合同类型 ID
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

        # 提取预订所需字段（方案 1：前置抓取，省去 try_book 中的独立查询）
        sku = item.get("sku", "")

        attrs = _parse_attr(item.get("custom_attributesV2", {}).get("items", []))

        atb = attrs.get("available_to_book")
        if isinstance(atb, list) and atb:
            status = atb[0]["label"]
        else:
            status = "Unknown"

        # 优先取总价（含服务费/水电/管理费），其次基础租金，最后从 price_range 降级
        raw = attrs.get("price") or attrs.get("basic_rent")
        if raw:
            price_raw = f"€{float(raw):.0f}"
        else:
            try:
                val = item["price_range"]["minimum_price"]["regular_price"]["value"]
                price_raw = f"€{val:.0f}"
            except (KeyError, TypeError):
                price_raw = None

        avail_date = attrs.get("available_startdate")
        available_from = avail_date.split(" ")[0] if avail_date else None

        # contract_id：从 type_of_contract 属性的 selected_options[0].value 解析
        contract_id: Optional[int] = None
        toc = attrs.get("type_of_contract")
        if isinstance(toc, list) and toc:
            try:
                contract_id = int(toc[0]["value"])
            except (KeyError, ValueError, TypeError):
                pass

        # contract_start_date：预订专用，优先 next_contract_startdate
        raw_next = attrs.get("next_contract_startdate")
        contract_start_date: Optional[str] = None
        if raw_next:
            contract_start_date = raw_next.strip()[:10]  # "YYYY-MM-DD"

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
        # 合同类型 / 短租标签
        offer = attrs.get("offer_text_two", "")
        if offer and offer.strip():
            features.append(f"Offer: {offer.strip()}")
        toc = attrs.get("type_of_contract")
        if isinstance(toc, list) and toc:
            features.append(f"Contract: {toc[0]['label']}")
        tp = attrs.get("tenant_profile")
        if isinstance(tp, list) and tp:
            features.append(f"Tenant: {tp[0]['label']}")

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
    except Exception as e:
        # 含 city 上下文：哪个城市的哪个 url_key 解析失败，便于排查 API schema 变化
        try:
            uk = item.get("url_key", "?") if isinstance(item, dict) else "?"
        except Exception:
            uk = "?"
        try:
            sk = item.get("sku", "?") if isinstance(item, dict) else "?"
        except Exception:
            sk = "?"
        logger.warning(
            "[%s] 解析房源失败 url_key=%s sku=%r: %s",
            city_name, uk, sk, e,
            exc_info=True,
        )
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
    该城市所有页面抓到的 Listing 列表。

    Raises
    ------
    ScrapeNetworkError  第 1 页网络错误（连接超时/TLS中断/DNS故障）→ 该城市
                        完全无法抓取，由上层做连续失败计数和冷却
    RateLimitError / BlockedError  直接从 _post_gql 上传

    注意
    ----
    第 1 页之外的请求失败（如第 3 页超时）仍返回已有数据，不抛异常——
    已经拿到前 2 页的结果，部分数据优于零数据。
    GraphQL 错误（errors 字段）视为致命错误，立即停止该城市的抓取。
    单条房源解析失败（_to_listing 返回 None）不影响其他条目。
    """
    listings: list[Listing] = []
    total_items = 0
    skipped = 0
    current_page = 1

    while True:
        filter_str = _build_filter(city_ids, availability_ids)
        query = _GQL_QUERY % (filter_str, current_page)

        logger.info("[%s] 抓取第 %d 页", city_name, current_page)
        try:
            data = _post_gql(session, query)
        except (RateLimitError, BlockedError):
            raise   # 直接上传：429 等待可恢复 / 403 需人工介入，monitor 各自处理
        except Exception as e:
            logger.error(
                "[%s] 请求失败 page=%d city_ids=%s avail_ids=%s: %s",
                city_name, current_page, city_ids, availability_ids, e,
                exc_info=True,
            )
            # 第 1 页失败 = 该城市完全无法抓取 → 抛异常让上层感知并触发冷却
            # 后续页失败 → break 返回已有数据（至少拿到了前面几页）
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

        products = data.get("data", {}).get("products", {})
        items = products.get("items") or []
        page_info = products.get("page_info", {})
        total_pages = page_info.get("total_pages", 1)

        for item in items:
            listing = _to_listing(item, city_name)
            if listing:
                listings.append(listing)
            else:
                skipped += 1
        total_items += len(items)

        logger.info("[%s] 第 %d/%d 页，本页 %d 条", city_name, current_page, total_pages, len(items))

        if current_page >= total_pages or current_page >= _MAX_PAGES:
            break
        current_page += 1

    rate = skipped / total_items if total_items else 0
    if skipped:
        logger.warning(
            "[%s] 共抓取 %d/%d 条房源，%d 条解析失败（%.0f%%）",
            city_name, len(listings), total_items, skipped, rate * 100,
        )
    else:
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
    所有城市抓取结果合并后的 Listing 列表。个别城市失败（含 ScrapeNetworkError）
    不影响其他城市结果；仅全部城市均失败时向上抛出。

    Raises
    ------
    ScrapeNetworkError  全部城市第 1 页均网络错误 → 无有效抓取结果
    RateLimitError      任意城市持续 429 → 直接上传
    BlockedError        任意城市 403 → 直接上传

    副作用
    ------
    每次调用都创建一个新的 curl_cffi Session（TCP 连接不跨调用复用）。
    代理通过 HTTPS_PROXY / HTTP_PROXY 环境变量控制，热重载时自动生效。

    Raises
    ------
    RateLimitError  任意城市遭遇持续 429（已退避重试仍失败），直接上传给 monitor
    """
    if availability_ids is None:
        availability_ids = ["179", "336"]

    # 代理：统一通过 get_proxy_url() 读取，支持 socks5:// 和 http:// 格式
    proxy = get_proxy_url()
    proxies = {"https": proxy, "http": proxy} if proxy else {}
    if proxy:
        logger.debug("使用代理: %s", _mask_proxy_url(proxy))

    all_listings: list[Listing] = []
    network_failures: list[str] = []  # 遭遇 ScrapeNetworkError 的城市

    with req.Session(impersonate=get_impersonate(), proxies=proxies) as session:
        for city_name, city_id in city_tasks:
            try:
                listings = _scrape_city_pages(
                    session,
                    city_name,
                    city_ids=[str(city_id)],
                    availability_ids=availability_ids,
                )
                all_listings.extend(listings)
            except (RateLimitError, BlockedError):
                raise   # 429/403 都是 IP/指纹级别的问题，不是单城市失败，直接上传
            except ScrapeNetworkError as e:
                network_failures.append(city_name)
                logger.error(
                    "[%s] 第 1 页网络失败 city_id=%s avail_ids=%s proxy=%s: %s",
                    city_name, city_id, availability_ids,
                    "yes" if proxy else "no", e,
                    exc_info=True,
                )
            except Exception as e:
                logger.error(
                    "[%s] 抓取失败 city_id=%s avail_ids=%s proxy=%s: %s",
                    city_name, city_id, availability_ids,
                    "yes" if proxy else "no", e,
                    exc_info=True,
                )

    # 全部城市第 1 页均网络失败 → 不存在有效抓取结果，
    # 上传让 monitor 做连续失败计数和冷却，避免空数据污染 last_scrape_at
    if not all_listings and network_failures and len(network_failures) == len(city_tasks):
        raise ScrapeNetworkError(
            f"全部 {len(city_tasks)} 个城市第 1 页网络失败: {', '.join(network_failures)}"
        )

    return all_listings
