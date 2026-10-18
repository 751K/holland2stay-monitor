"""
scrapers/holland2stay_rest.py — H2S 的 REST 快速通道（**只发现，不入库，不预订**）
=================================================================================

为什么需要这个文件
------------------
主抓取器（``scrapers/holland2stay.py``）走 CloakBrowser + GraphQL，一条链路要
过 Cloudflare 挑战、站点自有 clearance、请求体加密三道关，所以没法把轮次压到
秒级：高峰期主循环两轮之间中位 27 秒、p90 52 秒（2026-10 生产 7 天）。房源放出
的时刻是随机的，平均要等半个间隔才被看到——对「几秒就没了」的房源太慢。

而站点的 Cloudflare 规则是**按路径**设的：``/residences/*.html`` 与
``/api/service/graphql`` 必须过挑战，``/api/rest/**`` 是免检路径。这条路径上
的 ``products-render-info`` 支持 ``searchCriteria`` 过滤**自定义属性**（实测）：

    filter_groups[0] field=available_to_book  value=179      condition_type=eq
    filter_groups[1] field=city               value=29,25    condition_type=in

组合起来就是「这些城市当前可订的房源集合」。``fields`` 取不到这些属性的
**值**（会被静默丢弃），但本模块只需要集合本身——集合里新出现的 id 就是刚
放出的房。

于是：**3 秒一轮、不管几个城市都只有一次请求（Eindhoven 约 2 KB）、不开浏览器。**

出口走代理池，**每个请求换一个 IP**（``proxy`` 参数，见 ``mcore.h2s_rest.rest_proxy``）。
流量小到无所谓（每天约 22 MB），而工作日 9 个多小时、3 秒一次就是一万多个请求，
全压在服务器 IP 上迟早被盯上——那个 IP 还是代理全挂时的兜底出口，烧不起。

只查「可订」，不查抽签：抽签不是先到先得，晚几十秒发现没有任何损失，主循环
按原节奏抓到就够了。抽签转可订时 id 会新出现在可订集合里，照样会被报出来。

本模块在系统里的位置
--------------------
它是一个**触发器**，不是数据源。发现新 id 之后，monitor 叫醒主循环立刻跑一轮
只抓 H2S 的抓取（见 ``mcore/h2s_rest.py``），写库、通知、预订全走原来那条路。

不让它直接产出 Listing 去写库或预订，是因为它拿不到的东西恰恰是那条路需要的：

- **状态**：只能靠查询条件保证「可订」，读不到字段本身
- **用户筛选**：面积、房型、家具、入住日都在 features / available_from 里
- **下单参数**：``sku`` / ``contract_id`` 拿不到，``try_book`` 会自己用浏览器
  再查一次——和先跑一轮主抓取花的时间差不多
- **去重**：只有一条写库路径，才保证一套房只通知一次、只订一次

为什么不并入 scrapers/holland2stay.py
-----------------------------------
那个文件的传输层是 ``BrowserBackedScraper``，生命周期（懒创建 / 超龄重建 /
失效丢弃）由基类管；本模块是纯 HTTP、无浏览器、无 profile。硬塞进去会让
「浏览器什么时候需要存在」这件事变得不可判——而它恰恰是省流量的核心。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)

#: 免检路径。改这个常量之前先确认 CF 规则没变（用无 UA 伪造的裸请求试）。
_REST_URL = "https://www.holland2stay.com/api/rest/V1/products-render-info"

#: ``available_to_book`` 的「Available to book」option ID，与
#: ``holland2stay._STATUS_MAP`` 同源。**只能放这一档**：``fields`` 取不到
#: available_to_book 的值，混进别的档位（例如 ``in(179,336)``）就分不清一条是
#: 可订还是抽签——抽签房会白白叫醒主循环。
_STATUS_AVAILABLE_ID = 179

#: 非住宅 SKU。车位、储物间、装修费在 REST 结果里和房源混在一起，
#: 价格分别是 €30 / €65 / €46 这种，混进来会把「新放出」的告警淹掉。
#:
#: 这里是**子串**匹配，词要挑不会出现在街名里的。曾经有过 ``"kast"``，把
#: Eindhoven 的 Kastanjelaan 整栋楼（2026-10-18 在售 12 套里的 5 套）一起滤掉；
#: 储物间在站点上叫 ``Storage N``，``"storage"`` 已经覆盖。
_EXCLUDE = (
    "storage", "parking", "locker", "berging", "finishing",
    "booking fee", "deposit", "rent ", "kantoor", "bedrijfsruimte",
)

#: 只取需要的字段。Magento 的 fields 参数对未知字段静默丢弃，多了只会白白
#: 放大响应体。
_FIELDS = "fields=items[id,name,url,price_info[final_price]]"

#: 「直连」要显式给空串。curl_cffi 会**自动读取环境变量**，容器里 ``HTTPS_PROXY``
#: 指向 webshare 的基础 URL（不带 per-source session），空字典会回落到它。
#: 见 net.py 的同名坑。只有不传 ``proxy`` 时（手工命令行）才直连。
_NO_PROXY = {"http": "", "https": ""}

#: 429 / 503 的退避阶梯。实测 3 秒一轮时不会触发；留着是为了上游改限流
#: 策略时有一个能自愈的台阶，而不是直接把这一轮丢掉。403 不在此列：那是
#: Cloudflare 规则变了（免检路径被收回），重试多少次结果都一样。
_BACKOFF_429 = (3, 6, 12, 24)

#: 翻页上限。车位和储物间也占名额，Eindhoven 13 条里只有 8 套是房；十几个城市
#: 加起来也到不了 200。真翻到第 5 页说明查询条件失效了（比如城市过滤被忽略、
#: 返回全国全部 SKU），这时宁可判失败，也不要拿一个错的集合去更新基线。
_MAX_PAGES = 5

_TIMEOUT = 20


def _is_residence(name: str) -> bool:
    low = (name or "").lower().strip()
    if not low:
        return False
    return not any(w in low for w in _EXCLUDE)


def _url_key(url: str) -> str:
    """``https://account.holland2stay.com/kastanjelaan-1-135.html`` → ``kastanjelaan-1-135``

    Listing.id 在主项目里就是 url_key（见 models.Listing 的字段说明），
    monitor 要拿它去库里核对主抓取器有没有看到，所以这里必须剥干净。
    """
    slug = (url or "").rstrip("/").split("/")[-1]
    if slug.endswith(".html"):
        slug = slug[:-5]
    return slug


class RestWatcher:
    """H2S 可订房源集合的秒级差分器。

    用法::

        w = RestWatcher([29, 25], label="Eindhoven/Rotterdam")
        while True:
            for rec in w.poll():      # 首次返回 []，之后只返回新出现的
                print(rec["id"], rec["name"])
            time.sleep(3)

    多个城市合成一次 ``in`` 查询。代价是结果里看不出每条属于哪个城市
    （``fields`` 同样取不到 city 的值）——作为触发器不需要：被叫醒的那一轮
    会抓全部 H2S 城市。

    ``poll()`` / ``snapshot()`` 是线程安全的（一个 Session，由 ``_lock`` 串行化），
    但**不保证**调用间隔——除了 429 退避，它不 sleep，节奏由调用方控制。
    """

    def __init__(
        self,
        city_ids: Iterable[int | str],
        *,
        label: str = "",
        page_size: int = 200,
        proxy: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        """
        proxy
            每次查询前调用一次，返回这次用的代理 URL。返回空串 / None 表示
            「现在没有能用的代理」，这次**不查**（不退回直连），记一次失败。
            不传则直连——只给手工命令行用。
        """
        self.city_ids: tuple[int, ...] = tuple(sorted({int(c) for c in city_ids}))
        if not self.city_ids:
            raise ValueError("RestWatcher 至少要一个城市")
        self.label = label or ",".join(str(c) for c in self.city_ids)
        self.page_size = page_size
        self._proxy_getter = proxy
        self._proxies: dict[str, str] = dict(_NO_PROXY)

        self._session = requests.Session(impersonate="chrome")
        self._session.headers.update({
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.holland2stay.com/en",
        })
        # 默认显式直连，免得 curl_cffi 自己去读 HTTPS_PROXY；每次查询再按
        # proxy 参数换上这次的出口（见 _fetch）
        self._session.proxies = dict(_NO_PROXY)

        #: 上一轮成功查询的 id → 记录。基线建立后可以是空的（城市里一套都没有）。
        self._known: dict[str, dict] = {}
        self._primed = False
        self._lock = threading.Lock()

        #: 统计（调用方可读，用于日志/面板）
        self.rounds = 0
        self.wire_bytes = 0
        self.last_error: Optional[str] = None

    @property
    def primed(self) -> bool:
        return self._primed

    @property
    def known_count(self) -> int:
        return len(self._known)

    # ------------------------------------------------------------------ #
    # 查询构造
    # ------------------------------------------------------------------ #
    def _query(self, page: int = 1) -> str:
        if len(self.city_ids) == 1:
            city_val, city_cond = str(self.city_ids[0]), "eq"
        else:
            city_val, city_cond = ",".join(str(c) for c in self.city_ids), "in"
        parts = [
            f"searchCriteria[pageSize]={self.page_size}",
            f"searchCriteria[currentPage]={page}",
            "storeId=1",
            "currencyCode=EUR",
            _FIELDS,
            "searchCriteria[filter_groups][0][filters][0][field]=available_to_book",
            f"searchCriteria[filter_groups][0][filters][0][value]={_STATUS_AVAILABLE_ID}",
            "searchCriteria[filter_groups][0][filters][0][condition_type]=eq",
            "searchCriteria[filter_groups][1][filters][0][field]=city",
            f"searchCriteria[filter_groups][1][filters][0][value]={city_val}",
            f"searchCriteria[filter_groups][1][filters][0][condition_type]={city_cond}",
        ]
        return _REST_URL + "?" + "&".join(parts)

    def _get_page(self, page: int) -> Optional[list[dict]]:
        """查一页。成功返回原始 items（可以是空列表）；失败返回 ``None``。"""
        url = self._query(page)
        reason = ""
        for i, wait in enumerate((0,) + _BACKOFF_429):
            if wait:
                logger.debug("[h2s-rest] %s，退避 %ss 后重试（第 %d 次）", reason, wait, i)
                time.sleep(wait)
            try:
                r = self._session.get(url, timeout=_TIMEOUT, proxies=self._proxies)
            except Exception as e:                       # 网络层
                reason = f"网络错误: {e}"
                logger.debug("[h2s-rest] %s", reason)
                continue
            self.wire_bytes += len(r.content)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception as e:
                    self.last_error = f"响应非 JSON: {e}"
                    logger.debug("[h2s-rest] %s", self.last_error)
                    return None
                items = data.get("items") if isinstance(data, dict) else None
                if items is None:
                    # Magento 没有结果时给 ``items: []``；连这个键都没有说明响应
                    # 形状变了，不能当「一套都没有」去更新基线。
                    self.last_error = "响应里没有 items 字段"
                    logger.debug("[h2s-rest] %s", self.last_error)
                    return None
                return items
            if r.status_code in (429, 503):
                reason = f"HTTP {r.status_code}"
                continue
            self.last_error = f"HTTP {r.status_code}"
            logger.debug("[h2s-rest] %s", self.last_error)
            return None
        self.last_error = f"{reason}，重试 {len(_BACKOFF_429)} 次仍失败"
        logger.debug("[h2s-rest] %s", self.last_error)
        return None

    def _fetch(self) -> Optional[list[dict]]:
        """查全部页。成功返回原始 items（可以是空列表）；任何一页失败返回 ``None``。

        **空列表和 None 必须分开**：「现在一套可订的都没有」是有效结果，要拿去
        更新基线；「这次没查成」不是，要原样保留上一轮的基线。混为一谈的话，
        城市空着时启动永远建不起基线，第一套放出来的房会被当成基线吞掉。

        同理不接受残页：第 2 页失败时拿第 1 页去更新基线，下一轮第 2 页恢复，
        里面的房会全部被当成「新出现」。

        一次查询的所有页用同一个出口。
        """
        if self._proxy_getter is not None:
            try:
                url = self._proxy_getter()
            except Exception as e:
                url = ""
                logger.debug("[h2s-rest] 取代理失败: %s", e)
            if not url:
                self.last_error = "没有可用代理（全部冷却中或当前是自家线路），本次不查"
                logger.debug("[h2s-rest] %s", self.last_error)
                return None
            self._proxies = {"http": url, "https": url}
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            items = self._get_page(page)
            if items is None:
                return None
            out.extend(items)
            if len(items) < self.page_size:
                return out
        self.last_error = f"翻到第 {_MAX_PAGES} 页还没完，查询条件可能失效"
        logger.debug("[h2s-rest] %s", self.last_error)
        return None

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Optional[dict[str, dict]]:
        """当前可订的 id → 精简记录；没查成返回 ``None``。"""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> Optional[dict[str, dict]]:
        items = self._fetch()
        if items is None:
            return None
        out: dict[str, dict] = {}
        for it in items:
            name = (it.get("name") or "").strip()
            if not _is_residence(name):
                continue
            key = _url_key(it.get("url") or "")
            if not key:
                continue
            pi = it.get("price_info") or {}
            out[key] = {
                "id": key,
                "product_id": it.get("id"),
                "name": name,
                "price": pi.get("final_price"),
            }
        self.last_error = None
        return out

    def poll(self) -> list[dict]:
        """跑一轮差分，返回**本轮新出现**的记录（``id`` / ``name`` / ``price``）。

        「新出现」= 上一轮不在可订集合里的 id：新放出、抽签转可订、订满后
        又被退回来的同一套，都是这一种。

        首次成功查询只建基线并返回空列表——否则第一次就把当前全部在售房源当成
        「新房源」，白白叫醒一轮。查询失败的轮次不动基线、返回空列表。
        """
        with self._lock:
            self.rounds += 1
            cur = self._snapshot_locked()
            if cur is None:
                return []

            prev, self._known = self._known, cur
            if not self._primed:
                self._primed = True
                logger.info("[h2s-rest] 基线已建立：%s 可订 %d 套", self.label, len(cur))
                return []

            return [rec for key, rec in cur.items() if key not in prev]

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# 手工验证入口：python -m scrapers.holland2stay_rest --city 29 --city 25
# ---------------------------------------------------------------------- #
def _main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="H2S REST 快速通道（只发现）")
    ap.add_argument("--city", type=int, action="append",
                    help="H2S 城市 ID，可重复；默认 29（Eindhoven）")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--verbose", action="store_true")
    ns = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    w = RestWatcher(ns.city or [29])
    try:
        for i in range(ns.rounds):
            t0 = time.monotonic()
            new = w.poll()
            dt = time.monotonic() - t0
            print(f"[{i+1}/{ns.rounds}] {dt:.2f}s  "
                  f"可订 {w.known_count} 套  新增 {len(new)}  "
                  f"累计 {w.wire_bytes/1024:.1f} KB"
                  + (f"  错误: {w.last_error}" if w.last_error else ""))
            for rec in new:
                print("   +", json.dumps(rec, ensure_ascii=False))
            if i < ns.rounds - 1:
                time.sleep(max(0.0, ns.interval - dt))
    finally:
        w.close()


if __name__ == "__main__":
    _main()
