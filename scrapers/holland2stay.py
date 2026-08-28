"""
scrapers/holland2stay.py — Holland2Stay 抓取器（CloakBrowser + 新 GraphQL API）
============================================================================

H2S 的 GraphQL 端点已迁移两次，当前为
``www.holland2stay.com/api/service/residences``（与主站同域，Cloudflare WAF
保护）。路径常量在 ``browser_fetcher._H2S_GQL_PATH``，迁移史与判据见那里。

旧 curl_cffi 直连路径已被 CF 封锁。新路径使用 CloakBrowser（patched Chromium）
绕过 CF Turnstile，再通过浏览器内 ``page.evaluate(fetch)`` 调用 GraphQL API。

本次同时完成了当初 P0 多源重构遗留的 TODO：将 H2S 爬取主体从顶层
``scraper.py`` 正式搬入本文件（那个桥接模块已于 2026-08-20 删除）。

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

from ._browser_backed import BrowserBackedScraper
from .base import (
    RATE_LIMIT_BACKOFF,
    BlockedError,
    OperationNotAllowedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    UpstreamMaintenanceError,
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
# 策略：进程内缓存 + 批次预算 + 请求限速。同一房源只取一次；冷启动分摊到若干轮。
#
# ── 四层机制各管什么（别再加第五层）─────────────────────────────────
#
# 「补齐字段不能丢」这件事历史上被四个地方分别处理过，各自的注释都声称自己是
# 关键的那一层。理清一次，免得下次又长出一层：
#
#   1. _DETAIL_CACHE          省流量。同一房源一个进程内只取一次。进程启动时
#                             由 prime_detail_cache() 用库里已有的值回填，
#                             否则每次重启头几轮都在重问已知答案（2026-08-25
#                             那次 429 挤掉新房源就是这么来的）。
#   2. _DETAIL_BUDGET_PER_ROUND / _DETAIL_REQUEST_SPACING / _DETAIL_STOP_ON_RATE_LIMIT
#                             省流量 + 防 429。见 _DetailBudget。
#   3. _STICKY_FEATURE_KEYS   ← **正确性归它管。** 在 mstorage/_listings.py：
#                             写库时这几个 key 若本轮没给，就从旧值补回来。
#                             判据是「抓取侧没给 ≠ 上游没有了，只是这轮没去问」。
#   4. _enrich() 里的 have 去重  取值优先级。列表查询给的值更新鲜，补齐不覆盖它。
#
# 第 3 层是 v1.17.3 才加的。在它之前，正确性确实压在第 2 层身上——预算盖不满
# 一轮的房源数就会「补一批、抹一批」来回拉锯，所以那时预算注释里写着「不能设
# 太小，原因不是性能而是正确性」，还配了一条 >= 50 的下限断言。
#
# **那个理由现在已经失效了**，但注释和断言一直留着，于是 v1.17.5 调整批次预算
# 时还在被它牵着走。2026-08-20 一并清掉：第 2 层从此是**纯流量旋钮**，调小只是
# 冷启动慢，不会丢数据。守卫见
# tests/test_h2s_detail_enrich.py::TestBudgetIsNotLoadBearingForCorrectness。
from h2s_booking_gql import (  # noqa: E402
    GETPRODUCTDETAIL as _GQL_DETAIL,
    OP_GETPRODUCTDETAIL as _OP_DETAIL,
)

#: url_key → 补齐出来的 feature 片段。**进程级，永不过期**——重启即清零，
#: 每次重建 ≈ 房源数 × 11 KB，按下面的预算分摊到多轮，不会在单轮里炸开。
#:
#: 空结果也缓存（房源查不到详情，已下架等），否则每轮白打一次。代价是上游
#: 后来补上数据时本进程不会再去看——重启即自愈，够用。
_DETAIL_CACHE: dict[str, dict[str, str]] = {}

#: **一个批次**（= 一轮里同一 source 的所有城市合计）最多补齐几条。
#: 缓存命中后稳态是 0，这个数只影响**冷启动铺满得多快**。
#:
#: **这是纯流量旋钮，不承担正确性。** 调小只是冷启动慢；没补到的房源写库时
#: 由 ``mstorage/_listings.py`` 的粘性合并保住旧值（见上面的四层说明）。
#: 这里曾经写着「不能设太小，原因不是性能而是正确性」——v1.17.3 之后那个论证
#: 已经失效，2026-08-20 删除。
#:
#: 数字本身：留着兜住「上游忽然返回几百条」的异常情况（300 条 = 3.4 MB）。
#: 实际很少花得完——2026-08-20 生产实测，0.6s 间隔下约 30 条就会撞 429，真正
#: 的限制器是速率而不是条数，见下面两个常量。
_DETAIL_BUDGET_PER_ROUND = 60

#: 两次详情请求之间的间隔。429 是按速率触发的，不是按总量。
#: 0.6s × 46 条 ≈ 28s，摊在本来就要几十秒的 H2S 抓取里可以接受；冷启动铺满后
#: 全是缓存命中，这个开销就归零。
_DETAIL_REQUEST_SPACING = 0.6

#: 撞到 429 就本轮收手。继续打只会加重限流，而且这些房源下轮还有机会——
#: 补齐本来就是跨轮渐进的。没有这个开关时，46 条里有 24 条在反复撞墙。
_DETAIL_STOP_ON_RATE_LIMIT = True


class _DetailBudget:
    """一个**批次**共享的补齐预算与限速状态。

    为什么必须跨城共享 —— 这是本类存在的唯一理由
    ---------------------------------------------
    上游的限流按**出口 IP 的请求速率**算。而 ``_enrich()`` 是每个城市调一次
    （``scrape()`` 里一次），所以把预算、间隔、撞 429 收手这三件事放成函数
    局部变量，等于给每个城市各发一份配额：

        每个城市一份独立预算、一次独立撞墙：Eindhoven 撞 429 收手，下一个城市
        从零开始再撞一次，间隔也在城市边界断掉。城市越多放大得越厉害
        （2026-08-20 线上是 2 个城市；库里有 19 个城市的历史行，那是过去监控过
        的范围，不是当前配置——写 review 时拿它当规模依据是错的）。

    2026-08-20 修 429 时加的间距与收手开关只覆盖了单城内部，跨城那一半漏了。
    本类把三个状态提到批次级，由 ``HollandStayScraper._begin_batch()`` 每批次
    建一个新的。

    代价：冷启动变慢，铺满要好几个全量轮（全量轮 30 分钟一次）。这在 v1.17.3
    之后是安全的——storage 的粘性字段合并
    （``mstorage/_listings.py:_STICKY_FEATURE_KEYS``）保证没轮到补齐的房源
    不会被抹掉旧值，渐进补齐不再有「补一批、抹一批」的拉锯。
    """

    __slots__ = ("remaining", "stopped", "started")

    def __init__(self, budget: int = _DETAIL_BUDGET_PER_ROUND) -> None:
        #: 本批次还能发几条详情请求
        self.remaining = budget
        #: 本批次撞过 429，剩下的城市一律不再发（继续打只会加重限流）
        self.stopped = False
        #: 本批次已经发过至少一条——决定下一条要不要先等间隔。
        #: 跨城也算数：城市边界不该让限速断掉。
        self.started = False

#: tenant_profile 的 option ID → 语义。2026-08-19 从站点**详情页正文**逐条实测确定
#: （不是猜的，也不是从下单向导的选项推断的——那次推断错了）：
#:     6213  "Important: Students only"
#:     6214  "You can book this residence as a working professional"
#:     6215  "You can book this residence as a student or a working professional"
#: 取值语义与 OurDomain / Xior 的 Tenant 维度对齐（见 config.SOURCE_ASSUMED_FEATURES）。
#: tenant_profile 不在 aggregations 里（实测），也不是可筛选属性（filter 报错），
#: 所以只能靠这张写死的表。上游若新增取值，未知 ID 原样跳过而不是瞎猜。
#: 这里刻意保留站点原文的措辞（6215 是 "student **or** …"），归一交给
#: ``models.FEATURE_SYNONYMS``——它把 ``student or employed`` 和荷兰语的
#: ``studenten of werkenden`` 一起收敛到 ``student and employed``。2026-08-25
#: 之前同义表里只有荷兰语那条，英文这条漏了：库里两种拼法并存（61 / 16 条），
#: 筛选下拉并排列出两个同义项，勾了「学生/上班族」的用户静默漏掉那 16 条。
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


def prime_detail_cache(snapshot: "dict[str, dict[str, str]]") -> int:
    """进程启动时用库里已有的补齐值回填 ``_DETAIL_CACHE``，返回回填条数。

    为什么需要它
    ------------
    缓存是进程级的，重启即清零。于是每次部署后的头几轮，几十条**早就补齐过**
    的房源会被重新问一遍详情——而限流是按速率算的，那一串请求把 429 撞出来之后
    就本批次收手，本轮真正需要补齐的**新房源**排在后面，轮不上。

    2026-08-25 实测：部署两分钟后进来的 ``beukenlaan-143-093`` 正是如此
    （日志：``详情补齐 1 条失败（成功 24，本轮共 37 条房源），已因 429 本批次收手``）。
    它是新房源，库里没有旧值，``_STICKY_FEATURE_KEYS`` 的粘性合并救不了它——
    粘性只能保住**已经有过**的值。结果它带着残缺的 feature 直接发了通知，勾了
    租客条件的用户被 fail-closed 拒掉，而补齐之后不会补发。

    回填之后，稳态下「不在缓存里」就等价于「这条是新的」，预算与限流自然全花在
    新房源上。

    这不是第五层机制
    ----------------
    见上面「四层机制各管什么」：本函数不改变任何一层的职责，只是让第 1 层
    （纯流量旋钮）在重启后不必从零开始。正确性仍然归第 3 层（storage 的粘性
    合并）管。

    代价：上游若改了某条房源的楼盘名，本进程不会再去核实（缓存本来就"永不过期"，
    这里只是把这个性质延长到跨重启）。楼盘名基本不变，可以接受。
    """
    added = 0
    for listing_id, feats in (snapshot or {}).items():
        if not listing_id or listing_id in _DETAIL_CACHE:
            continue
        clean = {k: v for k, v in (feats or {}).items() if k and v}
        if clean:
            _DETAIL_CACHE[listing_id] = clean
            added += 1
    return added


def _enrich(
    fetcher: "BrowserFetcher",
    listings: list[Listing],
    budget: "_DetailBudget | None" = None,
) -> int:
    """给还没补齐过的房源补上 Building / Tenant / Neighborhood / MinIncome。

    返回本次真正发出去的请求数。**fail-open**：任何一条失败都只记日志跳过，
    不让「补齐」这种锦上添花的事拖垮主抓取。

    ``budget`` 是**批次级**的（见 ``_DetailBudget``）——预算、请求间隔、撞 429
    收手三件事都记在它身上，所以同一轮里后面的城市接着前面的用，而不是各自
    重新开一份。不传时退化成一次性预算，给独立调用（单测 / 工具脚本）用。
    """
    if budget is None:
        budget = _DetailBudget()

    spent = 0
    failed = 0
    rate_limited = False
    first_err: str = ""
    for l in listings:
        extra = _DETAIL_CACHE.get(l.id)
        if extra is None:
            # 本批次已经撞过 429 / 预算已用尽 —— 都不再发请求，但**继续遍历**：
            # 缓存里已有的房源仍要把 feature 贴上去。
            if budget.stopped or budget.remaining <= 0:
                continue
            if budget.started:
                time.sleep(_DETAIL_REQUEST_SPACING)
            try:
                extra = _fetch_detail(fetcher, l.id)
            except RateLimitError as e:
                # 速率触顶。继续打只会加重限流；这些房源下轮还有机会。
                # 收手是**整个批次**的事，不只是这个城市——限流按 IP 算，
                # 下一个城市接着打就是接着撞同一堵墙。
                budget.started = True
                budget.remaining -= 1
                rate_limited = True
                failed += 1
                if not first_err:
                    first_err = f"RateLimitError: {e}"
                if _DETAIL_STOP_ON_RATE_LIMIT:
                    budget.stopped = True
                    break
                continue
            except Exception as e:
                # 只跳过这一条，不缓存失败——下轮还有机会。
                # **但要吵一声**：fail-open + debug 日志 = 静默半残，
                # 补齐悄悄只成功一半时从日志上完全看不出来（实测踩过）。
                budget.started = True
                budget.remaining -= 1
                failed += 1
                if not first_err:
                    first_err = f"{type(e).__name__}: {e}"
                continue
            budget.started = True
            budget.remaining -= 1
            spent += 1
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
            "，已因 429 本批次收手（下轮继续）" if rate_limited else "",
            first_err,
        )
    return spent


# ── BrowserFetcher（从共享模块导入）─────────────────────────────────
from browser_fetcher import H2S_PROFILE, BrowserFetcher  # noqa: E402


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
        # 取 price_range（到手价）而不是 basic_rent（**基础租金**，不含服务费与
        # 水电预付）。2026-08-28 在 Eindhoven 实测七条，price_range 比 basic_rent
        # 高 15%–38%（例：707 → 966、780 → 1076），差额量级正是荷兰的服务费加
        # 水电月付。
        #
        # 为什么这件事比"显示不准"严重：租金筛选跨 source 共用 price_value 这
        # 一个数。H2S 报基础租金而 RENTCafe 那几家报的是 RentCafe 的 Rent 列，
        # 同一个「≤ €800」在两边筛的不是同一件事，H2S 因此被系统性地美化，把
        # 别家如实报价的房源挤了出去。
        #
        # basic_rent 留作兜底而不是首选：price_range 是 Magento 的嵌套结构，
        # 任何一层缺失就取不到，而没有价格的房源会被所有带租金上限的筛选直接
        # 漏掉——宁可报一个偏低的价格，也好过整条不可见。
        #
        # allowance_price **不能**用：它是算房补资格的口径，不是租金。实测里
        # 它既可能低于 basic_rent（707 → 431），也可能是 0（1495 那条超过房补
        # 上限）。
        price_raw = None
        try:
            val = item["price_range"]["minimum_price"]["regular_price"]["value"]
            if val:
                price_raw = f"€{float(val):.0f}"
        except (KeyError, TypeError, ValueError):
            pass
        if price_raw is None:
            rent = item.get("basic_rent")
            if rent:
                try:
                    price_raw = f"€{float(rent):.0f}"
                except (TypeError, ValueError):
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
            UpstreamMaintenanceError,
        ):
            # **整个 taxonomy 都要原样上抛。** 这几个类各自代表一种上层要据以
            # 做不同决策的根因（换 IP / 等冷却 / 照抄 operation / 安静等维护 /
            # 查代理）。在这里压成别的类，上层那套决策就全废了。
            #
            # 漏一个的代价是实打实的，两次都栽过：
            #
            # OperationNotAllowedError —— 落进下面那条 except Exception 会被改判成
            #   「第 1 页网络错误」，和 2026-08-11 端点迁移时那句「请检查代理/网络」
            #   是同一类误诊：把「查询没登记」说成网络问题，排查往代理方向走，
            #   而代理是好的。
            #
            # UpstreamMaintenanceError —— 2026-08-04 / 08-15 生产实测共 20 次误判，
            #   日志一句话里同时写着「网络错误」和「平台维护中」：
            #     「[Eindhoven] 第 1 页网络错误: H2S 平台维护中（页面标题:
            #       H2S-Maintenance）」
            #   于是维护期走的是网络失败路径（5 分钟冷却 + 连续失败计数 + ERROR
            #   日志），而不是设计好的维护路径（15 分钟安静冷却 + INFO + 不发用户
            #   告警 + dashboard banner）。
            #   browser_fetcher.fetch() 里专门为这件事把异常小心地原样抛出来
            #   （见那里「这是维护，不是屏蔽」的注释），下一层就是这里给压掉的。
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
            # **有 errors 且没有可用 data 才算致命。** GraphQL 的 NON_NULL 传播
            # 会同时给出 errors 和部分 data，站点自己的前端就靠这个继续渲染；
            # 整页丢掉等于把能用的数据也一起扔了。booker.py 早就按这个规律处理，
            # 抓取侧一直没有。
            msgs = "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                for e in data["errors"]
            )
            if data.get("data"):
                logger.warning(
                    "[%s] GraphQL 带非致命错误 page=%d（有可用 data，继续解析）: %s",
                    city_name, current_page, msgs,
                )
            else:
                logger.error(
                    "[%s] GraphQL 错误 page=%d errors=%s",
                    city_name, current_page, data["errors"],
                )
                if current_page == 1:
                    # 和下面那条 data=null 保持一致：第 1 页没拿到任何数据就上抛。
                    #
                    # 这里以前是 break，于是返回 ([], False)、dispatcher 记一次
                    # **成功**。也就是把「上游拒绝了我们的查询」上报成「这个城市
                    # 成功抓到 0 条」——正是 total_pages 那段注释点名的
                    # 「没拿到数据被当成确认没有数据」那一类。而紧挨着的
                    # data=null 分支同样是「没拿到数据」，第 1 页却是 raise。
                    #
                    # 上抛不会误伤整轮：dispatcher 按 task 隔离，只有所有 source
                    # 的所有任务都失败才会上抛给 monitor 冷却。
                    raise ScrapeNetworkError(
                        f"[{city_name}] 第 1 页 GraphQL 返回错误且无可用数据"
                        f"（上游应用层错误，不是网络问题）: {msgs}"
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

class HollandStayScraper(BrowserBackedScraper):
    """
    Holland2Stay 抓取器（CloakBrowser + 新 GraphQL API）。

    浏览器生命周期归 ``BrowserBackedScraper``（懒创建 / 超龄重建 / 失效丢弃 /
    批次作用域，与 Xior 共用同一份实现）。本类只提供三样站点相关的东西：
    profile、存活时长、以及挂在浏览器生命周期上的 attr 标签表。
    """

    source = "holland2stay"
    _BROWSER_PROFILE = H2S_PROFILE

    #: 浏览器最大存活时间（秒）：超过后主动重建，避免会话过期被 CF 拦。
    #: 比 Xior 的 15 分钟长得多——H2S 靠的是**稳定**出口 IP 复用 clearance，
    #: 而 Xior 靠**频繁换** IP 摊开按 IP 累积的限流，两者的取舍方向相反。
    _BROWSER_MAX_AGE = 7200  # 2 小时

    def __init__(self) -> None:
        super().__init__()
        self._attr_labels: dict[str, dict[str, str]] = {}
        # 上次全量扫描的时刻。**不跟着浏览器重建清零**——浏览器 2 小时一换，
        # 清零会让每次换浏览器都强制一次全量，分层就白做了。
        self._last_full_scan_at: float = 0.0
        #: 本批次是否做全量扫描。由 _begin_batch 每轮算一次，见 _plan_scan。
        #: 独立调用（非 dispatcher）路径没有批次，默认全量，行为与旧版一致。
        self._full_this_batch: bool = True
        #: 本批次共享的详情补齐预算 / 限速状态。由 _begin_batch 每轮建一个。
        #: 见 _DetailBudget —— 每城一份是 2026-08-20 那次 429 没修干净的另一半。
        self._detail_budget: Optional[_DetailBudget] = None
        #: _end_batch 用来判断「计划了全量却一条没成」。
        self._batch_planned_full: bool = False
        self._batch_scan_mark: float = 0.0

    # ── 浏览器生命周期的站点侧钩子 ──────────────────────────────────

    def _new_fetcher(self, *, headless: bool) -> BrowserFetcher:
        """引用**本模块**的 BrowserFetcher —— 见基类同名方法的说明（测试接缝）。"""
        return BrowserFetcher(headless=headless, profile=H2S_PROFILE)

    def _on_browser_ready(self) -> None:
        """标签映射跟着浏览器走：换了浏览器就从空表重新累积。

        标签不再单独查（``GetAggregations`` 已被 operation 白名单挡掉），
        改由每次 ``GetCategories`` 响应自带的 aggregations 累积，见
        ``_labels_from_aggregations``。
        """
        self._attr_labels = {}

    def _on_browser_closed(self) -> None:
        self._attr_labels = {}

    def _begin_batch(self) -> None:
        """每批次开头决定这一轮是不是全量扫描，并重置详情补齐预算。

        预算必须在这里建、且**整批共享**：``_enrich()`` 是每城调一次，把预算
        留在它的局部变量里等于给各个城市各发一份配额。见 ``_DetailBudget``。

        **这里不推进 ``_last_full_scan_at``。** 计时器由 ``_note_full_scan_done()``
        在全量真的跑完之后推进，理由见那个方法。
        """
        now = time.monotonic()
        self._full_this_batch = (now - self._last_full_scan_at) >= _FULL_SCAN_INTERVAL
        self._detail_budget = _DetailBudget()
        # 给 _end_batch 判断「计划了全量却一条没成」用
        self._batch_planned_full = self._full_this_batch
        self._batch_scan_mark = self._last_full_scan_at

    def _end_batch(self) -> None:
        if self._batch_planned_full and self._last_full_scan_at == self._batch_scan_mark:
            # 计时器没被推进 = 没有任何城市完成全量。下一轮会立刻重试（这正是
            # 不提前记账的意义），但这件事本身必须可见：否则「Reserved 长期
            # 没被看到、状态收敛停摆」在日志上完全没有痕迹。
            logger.warning(
                "本批次计划做全量扫描，但没有任何城市完成——计时器不推进，"
                "下一轮立即重试。这期间 Reserved 房源没被看到，其状态收敛推迟。"
            )

    def _note_full_scan_done(self) -> None:
        """记下「全量扫描真的完成了一次」，推进计时器。

        为什么不在 ``_begin_batch()`` 里当场推进 —— 那是**先记账后干活**
        ------------------------------------------------------------------
        原实现在决定要做全量的那一刻就写 ``_last_full_scan_at = now``。批次随后
        403 / 熔断 / 代理挂掉时，那次全量一条数据都没拿到，计时器却已经走了：
        下一次全量要再等 30 分钟。H2S 熔断退避最长 6 小时
        （``monitor._H2S_CIRCUIT_MAX_COOLDOWN``），期间 Reserved 那批房源的状态
        收敛可以停摆很久，而日志上看不出「本该全量的那轮没成」。

        判据是「至少有一个城市跑完了全量」，不是「全部城市都跑完」：后者会让一个
        长期坏掉的城市把整批钉死在每轮全量上，而全量正是我们要省的那部分流量。
        """
        self._last_full_scan_at = time.monotonic()

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

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        configured = task.extra.get("availability_ids") or ["179", "336"]
        availability_ids, is_full = self._plan_scan(list(configured))
        if not is_full:
            logger.info(
                "[%s] 高频轮：只查 %s（省去 Reserved 全量），%.0f 秒后做全量",
                task.city_display, ",".join(availability_ids),
                max(0.0, _FULL_SCAN_INTERVAL - (time.monotonic() - self._last_full_scan_at)),
            )

        # dispatcher 路径下 batch_session() 已经把浏览器备好了；独立调用
        # （工具脚本 / 调试）走 _ensure_browser() 自建一个，之后挂在实例上复用。
        #
        # 这里原本是个 if/else：没有 fetcher 时**另起一个一次性浏览器**，用
        # 局部的 labels dict。两条路径行为并不一致——一次性那条每次都付一整轮
        # CF 挑战，且 attr 标签的跨轮累积在它上面是坏的（每次都是空表，features
        # 里会冒出裸 ID）。dispatcher 路径永远走不到它，于是这份发散一直没人
        # 发现。改成和 XiorScraper 同一个形状。
        fetcher = self._fetcher or self._ensure_browser()
        listings, complete = _scrape_city_pages(
            fetcher,
            task.city_display,
            city_ids=[task.city_key],
            availability_ids=availability_ids,
            attr_labels=self._attr_labels,
        )
        enriched = _enrich(fetcher, listings, self._detail_budget)

        if not is_full:
            # 高频轮看不见 Reserved，标成完整扫描会让那批房源被 stale 收敛
            # 判成「已下架」清掉。见 _FRESH_STATUSES 上方注释。
            complete = False
        elif complete:
            # 全量真的跑完了，这才轮到推进计时器。见 _note_full_scan_done。
            self._note_full_scan_done()

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
