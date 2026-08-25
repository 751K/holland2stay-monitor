"""
scrapers/xior.py — Xior Student Housing scraper
=================================================

Xior uses a WordPress + Yardi (RENTCafe) backend. Room data comes from a
form POST to ``admin-ajax.php`` with ``action=yardi_room_availability``.
The Turnstile widget on the page is a client-side decoration — the endpoint
does not validate tokens.

Per-task flow
-------------
1. Look up the building in ``BUILDINGS`` (a **hardcoded registry**:
   ``property_page_id`` / ``semester_id`` / room-type IDs, auto-discovered
   once on 2026-05-22). ``discover_buildings()`` at the bottom of this file
   can regenerate it, but is not part of the scrape path.
2. POST ``yardi_room_availability`` once per room type, **through the
   browser transport** (``BrowserFetcher`` + ``XIOR_PROFILE``) — the
   endpoint sits behind a Cloudflare challenge since 2026-08-02 and TLS
   fingerprint spoofing no longer gets through.
3. Deduplicate units by ``apartmentId``, map to ``Listing``.
4. If any unit looks bookable within the availability window, cross-check
   against RentCafe's ``floorplans.aspx`` (the authoritative source) — the
   WordPress feed lags and keeps listing units that are already taken.
   Fail-open: if that page is unreachable, trust the feed.

Rate limiting
-------------
Cloudflare rate-limits this endpoint **per IP** at ~15–20 req/window, and it
accumulates across rounds. Two mechanisms together:

- ``_MIN_REQUEST_INTERVAL`` paces requests through a process-wide lock shared
  by every building, so bursts stay under the window.
- ``XIOR_PROFILE.rotating_proxy=True`` + a short ``_BROWSER_MAX_AGE`` means
  rebuilding the browser also rotates the exit IP, which spreads the
  accumulated count. 429s additionally retry with ``RATE_LIMIT_BACKOFF``.

**The WordPress endpoint's traffic all goes out one exit IP** — the browser's.
The ``floorplans.aspx`` cross-check in step 4 is the deliberate exception: it
targets a different origin (``*.securerc.co.uk``), where the browser's
clearance is worthless, so it rotates its own exit IP and TLS fingerprint the
way OurDomain does against that same platform. Pinning it to the browser's
sticky IP is what kept it at 0/10 for its entire life — see
``_fetch_bookable_floorplan_ids``.
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from datetime import date
from threading import Lock
from typing import Optional

import curl_cffi.requests as req

from browser_fetcher import XIOR_PROFILE, BrowserFetcher
from config import assumed_features, get_proxy_url
from models import Listing

from ._browser_backed import BrowserBackedScraper
# floorplans.aspx 在 *.securerc.co.uk 上，和 OurDomain / OurCampus 同一套
# RentCafe + Cloudflare。取数打法（浏览器 header、同 session 403 重试、TLS 指纹
# 冷却状态机）都在那边，直接复用——ourcampus 也是这么接的。
from .ourdomain import (
    _get_text,
    _headers_for,
    _impersonate_attempts,
    _mark_fingerprint_blocked,
    _mark_fingerprint_good,
)
from .base import (
    RATE_LIMIT_BACKOFF,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    UpstreamMaintenanceError,
)

logger = logging.getLogger(__name__)

# 同源请求只需要路径——origin 由 XIOR_PROFILE.challenge_url 决定。
# 完整 URL 是 https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php，
# 但**不要**直接拿它去 curl：该端点在 CF 挑战后面，2026-08-02 实测恒 403。
_AJAX_PATH = "/wp-admin/admin-ajax.php"

# ``availability_response.errorCode`` 装的是 **HTTP 风格状态码**，2xx 都表示
# 上游调用成功：
#   200 —— 正常返回（units 可能为空）
#   204 —— 无可用单元；官方前端走完整流程收到的也是它
# 只有 2xx 之外才是真故障。
#
# 曾经把「非 204 即故障」当判据，结果返回 200 的那栋楼整晚每一轮、每个房型都被
# 误标为 incomplete，而同期真正的 429 只是零星几次。

# 请求最小间隔（秒）。CF 对这个端点按 IP 限流在 ~15–20 req/window。
#
# 限流是**按速率**而不是按轮次算的，所以关键是瞬时突发而非总量：
# 2026-08-02 实测，4 栋楼共 12 个房型、间隔 1.5s → 18 秒内打完 12 个请求
# （瞬时约 40 req/min），稳定触发 429，整轮退化成 2/6。
#
# 5s 间隔把同样 12 个请求摊到 60 秒（12 req/min），留出安全余量。轮次间隔
# 本身是 3–5 分钟，这点耗时完全放得下。
#
# 注意：楼栋数增加时单轮耗时线性增长（每栋楼的房型数 × 5s）。楼栋很多时
# 应考虑分轮抓取，而不是把这个值调小。
_MIN_REQUEST_INTERVAL = 5.0

# floorplans.aspx 权威校验的时间预算（秒）。见 _fetch_bookable_floorplan_ids。
# 45 秒 ≈ 一轮 Xior 的耗时（中位 55s），也就是说这道闸最多把轮次拖慢一倍，
# 到点就 fail-open。
_VERIFY_TIME_BUDGET = 45.0

# 装修档位的登记表与判定住在 config（它是「平台事实」，和
# SOURCE_ASSUMED_FEATURES 同一类），这里只做转发——mstorage 的存量订正也要用，
# 而 mstorage 不能 import scrapers。
from config import (  # noqa: E402
    XIOR_BUILDING_FURNISHING as BUILDING_FURNISHING,
    xior_furnishing_for as furnishing_for,
)


def _is_upstream_ok(code) -> bool:
    """上游 errorCode 是否表示成功（2xx）。无法解析时保守当作成功。

    保守方向的理由：误判成失败会把正常的零可用标成 incomplete，stale 收敛
    因此永不执行；误判成成功最多是少记一次故障，代价小得多。
    """
    try:
        return 200 <= int(code) < 300
    except (TypeError, ValueError):
        return True


class XiorScraper(BrowserBackedScraper):
    """Unit-level scraper for Xior properties backed by RENTCafe.

    浏览器生命周期归 ``BrowserBackedScraper``（与 H2S 共用同一份实现）。
    """

    source = "xior"
    _BROWSER_PROFILE = XIOR_PROFILE

    # ── building registry (auto-discovered 2026-05-22) ─────────────────

    BUILDINGS: dict[str, dict] = {
        "p0196062": {"url":"https://www.xiorstudenthousing.eu/netherlands/amsterdam/karspeldreef-student-accommodation/","display":"Amsterdam Karspeldreef","property_page_id":498,"semester_id":3281,"room_type_ids":[32249,33960,32251]},
        "p0196102": {"url":"https://www.xiorstudenthousing.eu/netherlands/amsterdam/naritaweg-student-accommodation/","display":"Amsterdam Naritaweg","property_page_id":499,"semester_id":3281,"room_type_ids":[29891,29892,29893,33947]},
        "p0196099": {"url":"https://www.xiorstudenthousing.eu/netherlands/breda/kraanstraat-student-accommodation/","display":"Breda Kraanstraat","property_page_id":1133,"semester_id":3281,"room_type_ids":[29890,37729,37734]},
        "p0196103": {"url":"https://www.xiorstudenthousing.eu/netherlands/breda/rat-verleghstraat-student-accommodation/","display":"Breda Rat Verleghstraat","property_page_id":1132,"semester_id":3281,"room_type_ids":[32257]},
        "p0196106": {"url":"https://www.xiorstudenthousing.eu/netherlands/breda/tramsingel-21-student-accommodation/","display":"Breda Tramsingel 21","property_page_id":1131,"semester_id":3281,"room_type_ids":[29902,29903]},
        "p0196107": {"url":"https://www.xiorstudenthousing.eu/netherlands/breda/tramsingel-27-student-accommodation/","display":"Breda Tramsingel 27","property_page_id":1130,"semester_id":3281,"room_type_ids":[32258,37735]},
        "p0196059": {"url":"https://www.xiorstudenthousing.eu/netherlands/delft/antonia-veerstraat-student-accommodation/","display":"Delft Antonia Veerstraat","property_page_id":1129,"semester_id":3281,"room_type_ids":[33935,33936]},
        "p0196060": {"url":"https://www.xiorstudenthousing.eu/netherlands/delft/barbarasteeg-student-accommodation/","display":"Delft Barbarasteeg","property_page_id":1128,"semester_id":3281,"room_type_ids":[32259]},
        "p0196499": {"url":"https://www.xiorstudenthousing.eu/netherlands/delft/phoenixstraat-student-accommodation/","display":"Delft Phoenixstraat","property_page_id":1127,"semester_id":3281,"room_type_ids":[32262,32261,32260]},
        "p0196467": {"url":"https://www.xiorstudenthousing.eu/netherlands/eindhoven/kronehoefstraat-student-accommodation/","display":"Eindhoven Kronehoefstraat","property_page_id":1126,"semester_id":3281,"room_type_ids":[33944,33945,33946]},
        "p0195855": {"url":"https://www.xiorstudenthousing.eu/netherlands/eindhoven/zernikestraat-student-accommodation/","display":"Eindhoven Zernikestraat","property_page_id":1125,"semester_id":3281,"room_type_ids":[29908,33951]},
        "p0196098": {"url":"https://www.xiorstudenthousing.eu/netherlands/groningen/eendrachtskade-student-accommodation/","display":"Groningen Eendrachtskade","property_page_id":1121,"semester_id":3281,"room_type_ids":[29888,32266]},
        "p0196468": {"url":"https://www.xiorstudenthousing.eu/netherlands/groningen/oosterhamrikkade-student-accommodation/","display":"Groningen Oosterhamrikkade","property_page_id":1120,"semester_id":3281,"room_type_ids":[29894]},
        "p0195447": {"url":"https://www.xiorstudenthousing.eu/netherlands/groningen/zernike-tower-student-accommodation/","display":"Groningen Zernike Tower","property_page_id":1119,"semester_id":3281,"room_type_ids":[29907,32267]},
        "p0196104": {"url":"https://www.xiorstudenthousing.eu/netherlands/leeuwarden/ritsumastraat-student-accommodation/","display":"Leeuwarden Ritsumastraat","property_page_id":1117,"semester_id":3281,"room_type_ids":[29899]},
        "p0196105": {"url":"https://www.xiorstudenthousing.eu/netherlands/leeuwarden/tesselschadestraat-student-accommodation/","display":"Leeuwarden Tesselschadestraat","property_page_id":1116,"semester_id":3281,"room_type_ids":[29901,33949,38022]},
        "p0196501": {"url":"https://www.xiorstudenthousing.eu/netherlands/leiden/verbeekstraat-student-accommodation/","display":"Leiden Verbeekstraat","property_page_id":1115,"semester_id":3281,"room_type_ids":[32270,33950]},
        "p0196111": {"url":"https://www.xiorstudenthousing.eu/netherlands/maastricht/annadal-student-accommodation/","display":"Maastricht Annadal","property_page_id":1114,"semester_id":3281,"room_type_ids":[32272,33934]},
        "p0195680": {"url":"https://www.xiorstudenthousing.eu/netherlands/maastricht/bonnefanten-student-accommodation/","display":"Maastricht Bonnefanten","property_page_id":1113,"semester_id":3281,"room_type_ids":[29883,38072]},
        "p0196471": {"url":"https://www.xiorstudenthousing.eu/netherlands/maastricht/vijverdalseweg-student-accommodation/","display":"Maastricht Vijverdalseweg","property_page_id":1112,"semester_id":3281,"room_type_ids":[29904,29905,32274]},
        "p0196502": {"url":"https://www.xiorstudenthousing.eu/netherlands/rotterdam/burgemeester-oudlaan-student-accommodation/","display":"Rotterdam Burgemeester Oudlaan","property_page_id":1111,"semester_id":3281,"room_type_ids":[32277,32275,32276]},
        "p0196500": {"url":"https://www.xiorstudenthousing.eu/netherlands/the-hague/eisenhowerlaan-student-accommodation/","display":"The Hague Eisenhowerlaan","property_page_id":1110,"semester_id":3281,"room_type_ids":[32278,32279,32280,33939,33940]},
        "p0196100": {"url":"https://www.xiorstudenthousing.eu/netherlands/the-hague/lutherse-burgwal-student-accommodation/","display":"The Hague Lutherse Burgwal","property_page_id":1107,"semester_id":3281,"room_type_ids":[32283,32284]},
        "p0195853": {"url":"https://www.xiorstudenthousing.eu/netherlands/utrecht/rotsoord-student-accommodation/","display":"Utrecht Rotsoord","property_page_id":1105,"semester_id":3281,"room_type_ids":[32286,32287]},
        "p0196503": {"url":"https://www.xiorstudenthousing.eu/netherlands/utrecht/willem-dreeslaan-student-accommodation/","display":"Utrecht Willem Dreeslaan","property_page_id":1104,"semester_id":3281,"room_type_ids":[29906]},
        "p0196469": {"url":"https://www.xiorstudenthousing.eu/netherlands/venlo/peperstraat-student-accommodation/","display":"Venlo Peperstraat","property_page_id":1103,"semester_id":3281,"room_type_ids":[29895]},
        "p0196470": {"url":"https://www.xiorstudenthousing.eu/netherlands/venlo/spoorstraat-student-accommodation/","display":"Venlo Spoorstraat","property_page_id":1102,"semester_id":3281,"room_type_ids":[29900,32288,33948]},
        "p0196465": {"url":"https://www.xiorstudenthousing.eu/netherlands/wageningen/costerweg-student-accommodation/","display":"Wageningen Costerweg","property_page_id":1101,"semester_id":3281,"room_type_ids":[29887]},
        "p0196466": {"url":"https://www.xiorstudenthousing.eu/netherlands/wageningen/duivendaal-student-accommodation/","display":"Wageningen Duivendaal","property_page_id":1100,"semester_id":3281,"room_type_ids":[32290,32291,32292,32293]},
        "p0196061": {"url":"https://www.xiorstudenthousing.eu/netherlands/aachen-vaals/katzensprung-student-accommodation/","display":"Aachen Vaals Katzensprung","property_page_id":1134,"semester_id":3281,"room_type_ids":[29889]},
    }

    # ── 浏览器生命周期 ─────────────────────────────────────────────────
    # 浏览器最大存活时间（秒）。比 H2S 的 2 小时短得多，因为重建浏览器同时
    # **换出口 IP**（XIOR_PROFILE.rotating_proxy=True）——这是把「按 IP 累积
    # 的限流」摊开的手段。
    #
    # 15 分钟 ≈ 3–4 轮 ≈ 40 个请求/IP，对 ~15–20 req/window 的端点留有余量。
    # 代价是每小时多 4 次 CF 挑战（Xior 的挑战约 7–9s，比 H2S 便宜）。
    _BROWSER_MAX_AGE = 900  # 15 分钟

    def _new_fetcher(self, *, headless: bool) -> BrowserFetcher:
        """引用**本模块**的 BrowserFetcher —— 见基类同名方法的说明（测试接缝）。"""
        return BrowserFetcher(headless=headless, profile=XIOR_PROFILE)

    # ── public API ─────────────────────────────────────────────────────

    # 全局限流锁：CF 按 IP 限流，所有 building 的请求共用这一把，
    # 保证跨 task 也满足 _MIN_REQUEST_INTERVAL。
    _rate_lock = Lock()
    _last_request_at = 0.0

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        bldg = self._building_for_task(task)
        display = task.city_display or bldg.get("display", task.city_key)
        prop_id = bldg["property_page_id"]
        semester = bldg["semester_id"]
        room_ids = bldg["room_type_ids"]

        fetcher = self._fetcher or self._ensure_browser()

        all_units: dict[str, dict] = {}
        complete = True

        # 串行，不再用 ThreadPoolExecutor：Playwright 的对象绑定创建线程，
        # 浏览器传输层不能跨线程并发调用。实际也没损失——所有 worker 本来就
        # 共享同一把全局限流锁，吞吐等于串行。
        for room_id in room_ids:
            with self._rate_lock:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < _MIN_REQUEST_INTERVAL:
                    time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
                XiorScraper._last_request_at = time.monotonic()

            data = _post_ajax(
                fetcher,
                property_page_id=prop_id,
                room_type_id=room_id,
                semester_id=semester,
            )
            if data is None:
                complete = False
                continue
            for unit in data.get("units", []):
                uid = str(unit.get("apartmentId", ""))
                if uid and uid not in all_units:
                    all_units[uid] = unit

        today = date.today()

        # floorplans.aspx 权威校验：仅当存在「窗口内的候选可订单元」时才多查一次
        # 该楼的 floorplans.aspx（绝大多数轮次没有候选 → 零额外请求）。fail-open：
        # 拿不到（None）就不 gate，信 WP feed，绝不漏报真房源。
        #
        # 但 fail-open **只填空白、不推翻证据**：拿不到校验时报出来的可订会带上
        # ``Listing.status_unverified``，存储层据此拒绝把一条已知不可订的房源翻
        # 成可订（mstorage._listings._should_hold_unverified）。2026-08-25 之前
        # 没有这道拦截，一条 Zernikestraat 的单元一天翻转 5 次、发了 190 条通知，
        # 其中两次「又可订」只是因为校验请求没打通。
        bookable_fp_ids = self._verify_bookable_floorplans(
            list(all_units.values()), today, display,
        )

        listings = [
            _to_listing(
                u, display=display, building_url=bldg.get("url", ""),
                building_key=(task.city_key or "").strip().lower(),
                today=today, bookable_floorplan_ids=bookable_fp_ids,
            )
            for u in all_units.values()
        ]

        logger.info("[%s] Xior 共抓取 %d 个单元", display, len(listings))
        return ScrapeResult(task=task, listings=listings, complete=complete)

    def _verify_bookable_floorplans(
        self,
        units: list[dict],
        today: date,
        display: str,
    ) -> Optional[set[int]]:
        """对窗口内的候选可订单元，抓 floorplans.aspx 求权威可订 floorplan 集合。

        - 无候选 → 返回 None（不 gate，省一次请求）
        - 有候选但无法推导/抓取 floorplans.aspx → 返回 None（fail-open）

        取数怎么打见 ``_fetch_bookable_floorplan_ids``。这里曾经把请求绑在
        ``fetcher.proxy_url``（浏览器那条 sticky 线路）上，理由是「WP feed 和
        这条校验应当来自同一个出口 IP」——**那个理由不成立**：浏览器的
        clearance 属于 Xior 主站的 origin，到了 ``securerc.co.uk`` 上不顶用，
        而绑死一个 IP 恰恰是 CF 在那边最吃的一招。
        """
        candidates = [u for u in units if _is_candidate_available(u, today)]
        if not candidates:
            return None
        apply_url = next(
            (u.get("applyOnlineURL") for u in candidates if u.get("applyOnlineURL")),
            "",
        )
        fp_url = _floorplans_url(apply_url or "")
        if not fp_url:
            logger.warning(
                "[%s] Xior 无法从 applyOnlineURL 推导 floorplans.aspx，"
                "fail-open 按 WP feed 结果", display,
            )
            return None
        ids = _fetch_bookable_floorplan_ids(fp_url)
        if ids is None:
            logger.warning(
                "[%s] Xior floorplans.aspx 验证不可用，fail-open 按 WP feed 结果"
                "（可能含已订走的房源）", display,
            )
        else:
            logger.info(
                "[%s] Xior floorplans.aspx 权威可订户型: %s（候选 %d 个单元）",
                display, sorted(ids), len(candidates),
            )
        return ids

    def _building_for_task(self, task: ScrapeTask) -> dict:
        key = (task.city_key or "").strip().lower()
        bldg = self.BUILDINGS.get(key)
        if bldg is not None:
            return bldg
        # allow ad-hoc buildings via extra fields (for testing / future auto-discovery)
        pid = task.extra.get("property_page_id")
        sem = task.extra.get("semester_id")
        rids = task.extra.get("room_type_ids")
        if not pid or not sem or not rids:
            raise ValueError(
                f"Unknown Xior city_key={task.city_key!r}; "
                "provide extra.property_page_id, extra.semester_id, extra.room_type_ids"
            )
        return {
            "url": task.extra.get("url", ""),
            "display": task.city_display,
            "property_page_id": int(pid),
            "semester_id": int(sem),
            "room_type_ids": list(rids),
        }


# ── HTTP helpers ─────────────────────────────────────────────────────────

def _post_ajax(
    fetcher: "BrowserFetcher",
    *,
    property_page_id: int,
    room_type_id: int,
    semester_id: int,
) -> Optional[dict]:
    """POST the Yardi AJAX endpoint through the browser transport.

    Returns the decoded *data* dict, or None on non-retryable failure (the
    caller marks the round incomplete).

    走浏览器而不是 curl_cffi：该端点已被 Cloudflare 挑战保护，TLS 指纹伪装
    过不去（2026-08-02 实测恒 403 + 挑战页）。CF 相关的失败——挑战、clearance
    过期、屏蔽——统一由 BrowserFetcher 处理，这里只管业务语义和限流退避。
    """
    payload = {
        "action": "yardi_room_availability",
        "property_page_id": str(property_page_id),
        "room_type_id": str(room_type_id),
        "semester_id": str(semester_id),
    }
    total_wait = 0
    for attempt, wait in enumerate([0] + list(RATE_LIMIT_BACKOFF)):
        if wait:
            total_wait += wait
            logger.warning(
                "Xior 限流，第 %d/%d 次退避，等待 %d 秒（累计 %ds）",
                attempt, len(RATE_LIMIT_BACKOFF), wait, total_wait,
            )
            time.sleep(wait)

        try:
            envelope = fetcher.fetch_form(_AJAX_PATH, payload, timeout_ms=30_000)
        except RateLimitError:
            # 429：退避后重试；退完还不行就把异常抛给上层
            if attempt < len(RATE_LIMIT_BACKOFF):
                continue
            raise
        except (BlockedError, UpstreamMaintenanceError):
            # CF 屏蔽 / 平台维护：重试没有意义，交给 source 级熔断
            raise
        except ScrapeNetworkError as exc:
            logger.error("Xior AJAX 请求失败 attempt=%d: %s", attempt, exc)
            if attempt < len(RATE_LIMIT_BACKOFF):
                continue
            return None

        if not envelope.get("success"):
            msg = (envelope.get("data") or {}).get("message", "unknown error")
            logger.warning("Xior AJAX 业务失败 attempt=%d: %s", attempt, msg)
            return None

        data = envelope.get("data", {}) or {}

        # WordPress 层成功 ≠ 上游成功：真实结果在 availability_response 里。
        #
        # 但 204 是例外，它就是 Xior 表达「该房型当前无可用单元」的方式，属于
        # 正常结果。这一点用官方前端验证过：在站点自己的 modal 里选房型、走完
        # 带 Turnstile 的完整流程，前端收到的同样是
        # ``{"success":true,"units":[],"total":0}`` + errorCode 204。
        # 把 204 当失败会让每一轮零可用都被标成 incomplete，stale 收敛永远不
        # 执行——那是把「没房」误报成「抓取坏了」。
        upstream = data.get("availability_response")
        if isinstance(upstream, dict):
            code = upstream.get("errorCode")
            if code and not _is_upstream_ok(code):
                logger.warning(
                    "Xior 上游可用性查询失败 attempt=%d: errorCode=%s msg=%s params=%s",
                    attempt,
                    code,
                    upstream.get("errorMessage"),
                    data.get("availability_params"),
                )
                return None

        return data

    raise RateLimitError(
        f"Xior 持续返回 429（已退避重试 {len(RATE_LIMIT_BACKOFF)} 次，"
        f"累计等待 {total_wait}s）。请降低轮询频率或配置 HTTPS_PROXY。"
    )


# ── Listing mapping ────────────────────────────────────────────────────────

# Yardi 的 unitStatus → 本项目的统一状态。
#
# 两个 Yardi 状态的区别只在**为什么现在没人住**：
#   Notice Unrented           现住户已递交退租通知，人还没搬走
#   Vacant Unrented Not Ready 已经空了，但房间还没收拾好（保洁/维修）
#
# 对用户而言两者没有差别——**都能立刻提交申请**。实测这两类单元都带
# ``applyOnlineURL``，``availableDate`` 分布也完全重叠，而且都得过闸②
# （floorplans.aspx 权威校验），过不了的一律降级 Occupied。
#
# ⚠️ ``Vacant Unrented Not Ready`` 曾被映射成 ``Available in lottery``。那是错的：
# **Xior 没有抽签机制**，"lottery" 是 Holland2Stay 专有概念（H2S 的
# availability filter id=336 摇号池）。这个错标有两个实际后果，不只是标签难看：
#   1. 面板给用户显示橙色 "Lottery" 徽标，等于告诉他们要去参加一个不存在的摇号
#   2. stale 收敛对 lottery 用的是 **2 天**阈值而非 7 天（见
#      ``mstorage/_listings.py`` 的 ``_STALE_LOTTERY_STATUS``），这些单元会比
#      应有的速度快 3.5 倍被推测成 Occupied
#
# 「还不能入住」这层信息由 ``available_from`` 表达，闸①（60 天窗口）已经把太
# 远的滤掉了，不需要再借一个语义不符的状态来编码。
_STATUS_MAP = {
    "notice unrented": "Available to book",
    "vacant unrented not ready": "Available to book",
}

# 这些状态才算「可订」，受可用日期窗口约束。
_AVAILABLE_STATUSES = ("Available to book",)

# 可用日期窗口（天）：只有 availableDate 落在 [今天, 今天+N] 内的单元才算
# 「现在真·可订」。Xior 的 Yardi 数据里大量 "Notice Unrented" 是「现住户已
# 递交退租通知、但要到很久以后（甚至一年多）才搬走」的单元——它们挂在可用
# feed 里只是给人提前申请，对「现在就要找房」的用户是噪音。超出窗口的降级为
# Occupied（仍留库跟踪：日后进入窗口会触发 Occupied→可订 的状态变更通知）。
_AVAILABLE_HORIZON_DAYS = 60


def _days_until(iso_date: Optional[str], today: date) -> Optional[int]:
    """``YYYY-MM-DD`` 距 today 的天数；无法解析/为空返回 None。"""
    if not iso_date:
        return None
    try:
        return (date.fromisoformat(iso_date) - today).days
    except ValueError:
        return None


# ── floorplans.aspx 权威可订校验 ─────────────────────────────────────────
#
# WordPress 的 yardi_room_availability feed 会滞后/宽松——单元已被订走或从可订
# 池移除后仍可能列在 feed 里（用户点 apply 链接发现「没了」）。RentCafe OLE 的
# floorplans.aspx 是权威来源：每个户型 tile 要么
#   (Available)              + <button class="applyButton" ... floorPlans=<id>>   真能订
#   (Contact for Availability) + <button class="contactButton" data-function='contactUsLink'>  订不了
# 我们抓这一页，取出「真正可订」的 floorplan id 集合，用来 gate WP feed 的单元
# （join key：WP 单元的 floorplanId == floorplans.aspx 的 floorPlans=<id>）。
_FP_TILE_SPLIT = re.compile(r'data-selenium-id\s*=\s*"FloorPlanAvailability"')
#: 连按钮文字一起吃进来——**类名和 data-selenium-id 分辨不了可订与否**，见下。
_FP_APPLY_BTN = re.compile(
    r'<button[^>]*data-selenium-id\s*=\s*"ApplyNow"[^>]*>(?P<label>.*?)</button>',
    re.S,
)
_FP_FLOORPLAN_ID = re.compile(r'floorPlans=(\d+)')

# ── 按钮文字才是判据 ────────────────────────────────────────────────
#
# 2026-08-25 实测，同一时刻的两栋楼：
#
#   Zernikestraat / Comfy（1109741）
#     <button id="Comfy" data-selenium-id="ApplyNow"
#             class="applyButton btn btn-primary">Rented Out</button>
#   Karspeldreef（1111515）
#     <button ... data-selenium-id="ApplyNow"
#             class="applyButton btn btn-primary">Available</button>
#
# 属性一模一样，只有**文字**不同。而 tile 顶上那句 ``(Available)``
# （``availability-count``）两边都写着，租完了也不更新——所以它不能用。
#
# 后果实测：xr_373301（Zernikestraat 1-222，19 m²，€781）在 WP feed 里挂着，
# 闸门看类名放行，面板上一直显示 Book，实际早已租出。
#
# 未知文字**放行并告警**：漏报真房源比误报一条贵得多（见模块头 step 4 的
# fail-open 取舍），但要在日志里留下痕迹，别再靠用户来发现。
_FP_UNBOOKABLE_LABELS = (
    "rented out", "sold out", "unavailable", "not available",
    "waiting list", "contact us",
)
_FP_BOOKABLE_LABELS = ("available", "apply", "book")
_FP_TAGS = re.compile(r"<[^>]+>")


def _button_label(raw: str) -> str:
    """按钮内文字，去标签、去实体、压空白。"""
    from html import unescape
    return re.sub(r"\s+", " ", unescape(_FP_TAGS.sub(" ", raw or ""))).strip()


def _floorplans_url(apply_url: str) -> Optional[str]:
    """从单元的 ``applyOnlineURL`` 推导出该楼的 floorplans.aspx URL。

    applyOnlineURL 形如::

        https://<slug>.securerc.co.uk/onlineleasing/<path>/oleapplication.aspx
            ?stepname=RentalOptions&myLeaseCafeType=2&myOlePropertyId=185589&...

    无法识别（缺 oleapplication.aspx 或 myOlePropertyId）时返回 None。
    """
    if not apply_url or "oleapplication.aspx" not in apply_url:
        return None
    base = apply_url.split("oleapplication.aspx", 1)[0]  # .../onlineleasing/<path>/
    m = re.search(r"[?&]myOlePropertyId=(\d+)", apply_url)
    if not m:
        return None
    pid = m.group(1)
    lct = re.search(r"[?&]myLeaseCafeType=(\d+)", apply_url)
    lct_val = lct.group(1) if lct else "2"
    return (
        f"{base}floorplans.aspx?stepname=Floorplan&myOlePropertyId={pid}"
        f"&propertyId={pid}&IsFromBrochure=False&myLeaseCafeType={lct_val}"
        f"&myStuApplicantType=Student"
    )


def parse_bookable_floorplan_ids(html_body: str) -> set[int]:
    """解析 floorplans.aspx HTML，返回「真正可订」（applyButton）的 floorplan id 集合。"""
    ids: set[int] = set()
    for tile in _FP_TILE_SPLIT.split(html_body)[1:]:
        seg = tile[:4000]  # 一个户型 tile 的范围；apply 按钮+floorPlans id 都在内
        btn = _FP_APPLY_BTN.search(seg)
        if not btn or "applyButton" not in btn.group(0):
            continue  # contactButton / 无按钮 = 订不了
        m = _FP_FLOORPLAN_ID.search(seg)
        if not m:
            continue
        label = _button_label(btn.group("label"))
        low = label.lower()
        if any(k in low for k in _FP_UNBOOKABLE_LABELS):
            continue  # 类名是 applyButton，文字却写着 Rented Out —— 订不了
        if not any(k in low for k in _FP_BOOKABLE_LABELS):
            logger.warning(
                "Xior floorplans.aspx 出现没见过的按钮文字 %r（floorplan %s），"
                "按可订放行——若它其实代表订不了，会误报一条房源",
                label[:40], m.group(1),
            )
        ids.add(int(m.group(1)))
    return ids


def _fetch_bookable_floorplan_ids(url: str) -> Optional[set[int]]:
    """取 floorplans.aspx 并解析可订 floorplan id 集合。

    返回 None 表示「无法判定」——调用方据此 **fail-open**（信 WP feed，不漏报
    真房源）。

    为什么走 OurDomain 那套而不是单发一次
    -----------------------------------
    这个页面在 ``*.securerc.co.uk`` 上，和 OurDomain / OurCampus 打的是同一套
    RentCafe + Cloudflare。原实现是「浏览器的 sticky 出口 IP + 默认指纹 + 单发、
    不带 header」，2026-08-25 复盘：**生产日志里 10 次尝试 10 次失败**（5 次
    403、5 次 challenge 页），0 次成功——这道「权威闸门」自上线起就一直敞着，
    所有 Xior 通知走的都是未经校验的 WP feed。Naritaweg 60S 就是这么发出去的。

    当天在生产上做的对照（同一个 URL，前后几分钟内）：

        ===========================================  ======
        打法                                          结果
        ===========================================  ======
        现状：sticky IP + 默认指纹 + 单发 + 无 header   0/2
        OurDomain 打法（下面这套）                     10/10
        ===========================================  ======

    差别在三件事，缺一不可：**浏览器风格 header**、**同 session 内 403 重试**
    （CF 首次 403 会顺手下发 cf_clearance，第二次同 URL 往往就软通过）、
    **每次尝试换出口 IP + 换 TLS 指纹**。这正是 ``ourdomain._get_text`` 和
    它的指纹状态机，直接复用而不是抄一遍——``ourcampus`` 也是这么做的。

    指纹的冷却状态是模块级、跨 source 共享的：三个 source 打的是同一个
    SecureRC 集群，某个指纹被烧对谁都一样，所以这里也如实记账。

    出口 IP 用 ``rotating=True``，和浏览器那条 sticky 线路无关：原注释主张
    「跟浏览器同一个 IP」是为了复用 clearance，但**浏览器的 clearance 属于
    Xior 主站那个 origin，在 securerc.co.uk 上一文不值**——这里没有 clearance
    可复用，换 IP 才是恢复手段（与 OurDomain 同理）。
    """
    tried: list[str] = []
    blocked: list[str] = []
    transport_errors: list[str] = []
    started = time.monotonic()
    for impersonate in _impersonate_attempts():
        # 时间预算：这是一道**尽力而为**的闸，拿不到就 fail-open。而
        # ``_get_text`` 遇 429 会退避 30s + 60s，四个指纹轮完最坏能吃掉六分钟
        # ——而整轮 Xior 本身才 55 秒。宁可这一轮不校验，也不能让它把轮次拖垮：
        # 拖垮的代价是**所有**楼的房源都晚到，比放过一条未校验的房源更贵。
        #
        # ``started`` 在函数入口取，所以第一次尝试一定跑得到——预算管的是
        # 「还要不要继续轮换」，不是「要不要开始」。
        if time.monotonic() - started > _VERIFY_TIME_BUDGET:
            logger.warning(
                "Xior floorplans.aspx 校验超出 %.0fs 预算，剩余指纹不再试 url=%s",
                _VERIFY_TIME_BUDGET, url,
            )
            break
        tried.append(impersonate)
        proxy = get_proxy_url("xior", rotating=True)
        proxies = {"https": proxy, "http": proxy} if proxy else {}
        try:
            with req.Session(impersonate=impersonate, proxies=proxies) as session:
                html = _get_text(session, url, headers=_headers_for(url))
        except BlockedError:
            _mark_fingerprint_blocked(impersonate)
            blocked.append(impersonate)
            continue
        except Exception as exc:
            # 传输类异常（代理 502 / CONNECT tunnel failed / 连接重置）。
            #
            # 这里原先是 ``return None``，注释的理由是「网络类异常换指纹也没
            # 用」——这话对了一半：换**指纹**确实没用，但每次尝试还会
            # ``get_proxy_url(rotating=True)`` **换出口 IP**，而代理 502 恰恰
            # 是出口那一端的事，换一条线路就好了。旧写法把这次重试机会一起
            # 扔掉，直接落到 fail-open。2026-08-25 生产日志里两次「又可订」
            # 假告警就是这么来的。
            #
            # 指纹不记账：这不是它的错，别把一个好指纹烧进冷却池。
            transport_errors.append(f"{impersonate}: {exc}")
            logger.debug("Xior floorplans.aspx 传输异常，换线路重试 url=%s: %s", url, exc)
            continue
        _mark_fingerprint_good(impersonate)
        return parse_bookable_floorplan_ids(html)

    # 走到这里 = 一次都没成功。把「被挡」和「线路坏了」分开报：前者要看指纹池
    # 和 CF，后者要看代理商，混成一句话会把人指向错误的方向。
    if blocked and not transport_errors:
        logger.warning(
            "Xior floorplans.aspx 全部 TLS 指纹都被挡 url=%s，已试: %s",
            url, ", ".join(tried),
        )
    elif transport_errors and not blocked:
        logger.warning(
            "Xior floorplans.aspx 全部线路都连不通 url=%s，已试 %d 条: %s",
            url, len(transport_errors), " ｜ ".join(transport_errors),
        )
    else:
        logger.warning(
            "Xior floorplans.aspx 校验全部失败 url=%s｜被挡 %d 次（%s）"
            "｜线路不通 %d 次（%s）",
            url, len(blocked), ", ".join(blocked) or "-",
            len(transport_errors), " ｜ ".join(transport_errors) or "-",
        )
    return None


def _is_candidate_available(unit: dict, today: date) -> bool:
    """该单元是否「窗口内的候选可订」——即映射为可订/可抽签且 availableDate 不超窗。

    只有存在这类候选时才值得去抓 floorplans.aspx 做权威校验。
    """
    raw_status = (unit.get("unitStatus") or "").strip().lower()
    if _STATUS_MAP.get(raw_status, "Occupied") not in _AVAILABLE_STATUSES:
        return False
    days = _days_until(_normalise_date(unit.get("availableDate", "")), today)
    return not (days is not None and days > _AVAILABLE_HORIZON_DAYS)


def _to_listing(
    unit: dict,
    *,
    display: str,
    building_url: str,
    building_key: str = "",
    today: Optional[date] = None,
    bookable_floorplan_ids: Optional[set[int]] = None,
) -> Listing:
    today = today or date.today()
    apt_id = str(unit.get("apartmentId", ""))
    apt_name = unit.get("apartmentName") or f"#{apt_id}"
    fp_name = unit.get("floorplanName") or ""
    sqm = unit.get("sqm", 0)
    min_rent = unit.get("minimumRent", 0)
    max_rent = unit.get("maximumRent", 0)
    deposit = unit.get("deposit", 0)
    avail_date = _normalise_date(unit.get("availableDate", ""))
    raw_status = (unit.get("unitStatus") or "").strip().lower()

    status = _STATUS_MAP.get(raw_status, "Occupied")

    # 可用日期窗口闸：远期才空出的单元不算现在可订。只在「有明确日期且超窗」
    # 时降级——日期缺失/不可解析时保守保留可订状态（不过度隐藏真房源）。
    if status in _AVAILABLE_STATUSES:
        days = _days_until(avail_date, today)
        if days is not None and days > _AVAILABLE_HORIZON_DAYS:
            logger.debug(
                "Xior 单元 %s available_date=%s 超出 %d 天窗口（%d 天后），"
                "降级为 Occupied 不报可订",
                apt_id, avail_date, _AVAILABLE_HORIZON_DAYS, days,
            )
            status = "Occupied"
        elif bookable_floorplan_ids is not None:
            # floorplans.aspx 权威校验：户型不在可订集合 = WP feed 滞后/已订走。
            # bookable_floorplan_ids 为 None 时不进此分支（fail-open，信 feed），
            # 改由下面的 status_unverified 交给存储层拦住状态翻转。
            # floorplanId 解析不出来也不 gate（保守，避免误杀真房源）。
            try:
                fp_id = int(unit.get("floorplanId"))
            except (TypeError, ValueError):
                fp_id = None
            if fp_id is not None and fp_id not in bookable_floorplan_ids:
                logger.debug(
                    "Xior 单元 %s floorplan %s 不在 floorplans.aspx 权威可订集合，"
                    "降级为 Occupied（feed 滞后/已订走）",
                    apt_id, fp_id,
                )
                status = "Occupied"

    price_raw = f"€{min_rent}"
    if max_rent and max_rent != min_rent:
        price_raw = f"€{min_rent}–€{max_rent}"

    features = [
        f"Unit: {apt_name}",
        f"Building: {display}",
        # 整个 source 恒定成立、feed 里不上报的属性（目前只有 Tenant）。
        # 装修档位**不在这里**——它按 room type 变，见 furnishing_for。
        *assumed_features("xior"),
    ]
    # 装修档位：房型名优先，其次按楼登记，都没有就不写（fail-closed，理由见
    # BUILDING_FURNISHING 的注释）。
    furnishing = furnishing_for(building_key, fp_name)
    if furnishing:
        features.append(f"Finishing: {furnishing}")
    else:
        logger.debug(
            "Xior 单元 %s 的装修档位未知（楼栋 %s 未登记、房型名 %r 也没说），"
            "不写 Finishing——该房源会被装修筛选 fail-closed 排除",
            apt_id, building_key or "?", fp_name,
        )
    if fp_name:
        features.append(f"Floorplan: {fp_name}")
    if sqm:
        features.append(f"Area: {sqm} m²")
    if deposit is not None and deposit > 0:
        features.append(f"Deposit: €{deposit}")
    elif deposit == 0:
        features.append("Deposit: €0")

    # 这一轮报的「可订」有没有权威依据？没有的话让存储层拦住状态翻转
    # （见 Listing.status_unverified / mstorage.diff）。只有真报可订才有意义：
    # Occupied 不需要校验背书，feed 说没有就是没有。
    status_unverified = (
        bookable_floorplan_ids is None and status in _AVAILABLE_STATUSES
    )

    return Listing(
        id=f"xr_{apt_id}",
        name=f"{display} {apt_name}",
        status=status,
        status_unverified=status_unverified,
        price_raw=price_raw,
        available_from=avail_date,
        features=features,
        url=unit.get("applyOnlineURL") or building_url,
        city=display,
        source="xior",
    )


def building_key_for(listing) -> str:
    """从一条 Xior ``Listing`` 反查它属于哪栋楼（``BUILDINGS`` 的 key）。

    自动预订需要这个：Xior **一栋楼一个账号**（每栋楼是独立的 RENTCafe
    property 门户），必须按楼栋取对应凭据。

    按 ``listing.city`` 反查——对 Xior 而言 city 存的就是楼栋的 display 名
    （见 ``_to_listing``）。没有用 ``applyOnlineURL`` 的 host 做主键，是因为
    ``BUILDINGS`` 注册表里根本没有 securerc host：它是抓取时才从单元数据里带
    出来的，注册表只有 Xior 官网 URL。

    对不上就返回空串，调用方据此跳过。**不要猜**——猜错等于拿 A 楼账号去 B 楼
    门户登录，必然失败，还白白消耗 RENTCafe 的 IP 级尝试额度（连续失败锁 30 分钟）。
    """
    city = (getattr(listing, "city", "") or "").strip()
    if city:
        for key, meta in XiorScraper.BUILDINGS.items():
            if (meta.get("display") or "").strip() == city:
                return key
    return ""


def _normalise_date(raw: str) -> Optional[str]:
    """``DD/MM/YYYY`` → ``YYYY-MM-DD``.  Returns None on unparseable input."""
    raw = raw.strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


# ── Auto-discovery helpers (for future use) ────────────────────────────────

def discover_buildings(
    session: req.Session,
    country: str = "netherlands",
) -> list[dict]:
    """Walk city pages under *country*, return building metadata dicts.

    Each dict contains ``url``, ``display``, ``property_page_id``,
    ``semester_id``, and ``room_type_ids`` — suitable for feeding into
    ``BUILDINGS`` or an ad-hoc ``ScrapeTask.extra``.
    """
    from html import unescape as _unescape
    import json as _json

    buildings: list[dict] = []
    country_url = f"https://www.xiorstudenthousing.eu/{country}/"
    resp = session.get(country_url, timeout=30)
    city_links = re.findall(
        rf'href="(https://www\.xiorstudenthousing\.eu/{country}/[^"]+/)',
        resp.text,
    )
    city_urls = list(dict.fromkeys(city_links))

    for city_url in city_urls:
        resp2 = session.get(city_url, timeout=30)
        bldg_links = re.findall(
            rf'href="(https://www\.xiorstudenthousing\.eu/{country}/[^"]*student-accommodation[^"]*)"',
            resp2.text,
        )
        for bldg_url in dict.fromkeys(bldg_links):
            bldg = _extract_building_meta(session, bldg_url)
            if bldg:
                buildings.append(bldg)
    return buildings


def _extract_building_meta(
    session: req.Session,
    bldg_url: str,
) -> Optional[dict]:
    """Fetch a single building page and return its scrape metadata."""
    from html import unescape as _unescape
    import json as _json

    resp = session.get(bldg_url, timeout=30)
    html = resp.text

    # window.xior = { ... }
    m = re.search(r"window\.xior\s*=\s*(\{[^;]+\});", html)
    if not m:
        return None
    xior = _json.loads(m.group(1))
    if xior.get("booking_engine") != "yardi":
        return None

    # property_page_id from the Yardi modal init
    ppid_m = re.search(r"propertyPageId\s*=\s*(\d+);", html)
    property_page_id = int(ppid_m.group(1)) if ppid_m else None

    # semester_id from hidden input
    sem_m = re.search(r'name="semester"\s+value="(\d+)"', html)
    semester_id = int(sem_m.group(1)) if sem_m else None

    # room type IDs from <input data-room-id="...">
    room_ids = list(dict.fromkeys(
        int(m2.group(1))
        for m2 in re.finditer(r'data-room-id="(\d+)"', html)
    ))

    if not property_page_id or not semester_id or not room_ids:
        return None

    return {
        "url": bldg_url,
        "display": xior.get("building_name") or xior.get("city", ""),
        "property_page_id": property_page_id,
        "semester_id": semester_id,
        "room_type_ids": room_ids,
    }
