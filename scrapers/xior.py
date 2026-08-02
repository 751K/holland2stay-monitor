"""
scrapers/xior.py — Xior Student Housing scraper
=================================================

Xior uses WordPress + Yardi (RENTCafe) backend. Room data is returned as
JSON via ``admin-ajax.php?action=yardi_room_availability``. The Turnstile
widget is a client-side decoration — the server does not validate tokens.

Three-stage flow
----------------
1. Extract ``property_page_id`` + ``semester_id`` + room-type IDs from
   the building page's Yardi modal HTML.
2. POST ``yardi_room_availability`` for each room type.
3. Deduplicate units by ``apartmentId``, map to ``Listing``.

Cloudflare rate-limits the AJAX endpoint at ~15–20 req/window (IP-level
429).  The scraper paces requests at ~2 req/s and retries on 429 with the
shared ``RATE_LIMIT_BACKOFF`` from ``scrapers/base.py``.
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
from config import get_impersonate, get_proxy_url
from models import Listing

from .base import (
    RATE_LIMIT_BACKOFF,
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    UpstreamMaintenanceError,
    is_cloudflare_body,
)

logger = logging.getLogger(__name__)

AJAX_URL = "https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php"
# 同源请求只需要路径——origin 由 XIOR_PROFILE.challenge_url 决定
_AJAX_PATH = "/wp-admin/admin-ajax.php"

# Xior 上游（Yardi）用 204 表示「该房型当前无可用单元」，是正常结果而非故障。
# 官方前端走完整流程收到的也是它 —— 见 _post_ajax 里的说明。
_UPSTREAM_NO_AVAILABILITY = 204


class XiorScraper(AbstractScraper):
    """Unit-level scraper for Xior properties backed by RENTCafe."""

    source = "xior"

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
    # 浏览器最大存活时间（秒）：超过后主动重建，避免会话过期被 CF 拦
    _BROWSER_MAX_AGE = 7200  # 2 小时

    def __init__(self) -> None:
        self._fetcher: Optional[BrowserFetcher] = None
        self._browser_created_at: float = 0.0

    def _ensure_browser(self) -> BrowserFetcher:
        """懒创建或复用浏览器实例。BlockedError 后自动重建。"""
        from config import CLOAKBROWSER_HEADLESS

        now = time.monotonic()
        if self._fetcher is not None:
            if now - self._browser_created_at > self._BROWSER_MAX_AGE:
                logger.info(
                    "Xior 浏览器已存活 %.0f 分钟，主动重建",
                    (now - self._browser_created_at) / 60,
                )
                self._close_browser()
            else:
                return self._fetcher

        self._fetcher = BrowserFetcher(
            headless=CLOAKBROWSER_HEADLESS, profile=XIOR_PROFILE
        )
        try:
            self._fetcher.__enter__()
            self._fetcher.ensure_initialized()
            self._browser_created_at = time.monotonic()
            logger.info("Xior 浏览器已创建并完成 CF 挑战")
            return self._fetcher
        except Exception:
            self._close_browser()
            raise

    def _close_browser(self) -> None:
        if self._fetcher is not None:
            try:
                self._fetcher.__exit__(None, None, None)
            except Exception:
                pass
            self._fetcher = None

    @contextmanager
    def batch_session(self):
        """批次上下文：确保浏览器存活，跨轮复用。"""
        try:
            self._ensure_browser()
            yield
        except (BlockedError, UpstreamMaintenanceError):
            logger.warning("抓取遇 Xior 浏览器会话不可复用状态，关闭浏览器")
            self._close_browser()
            raise

    # ── public API ─────────────────────────────────────────────────────

    # 全局限流：CF 按 IP 限流，这里保证请求之间至少间隔 1.5s。
    _rate_lock = Lock()
    _last_request_at = 0.0

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        bldg = self._building_for_task(task)
        display = task.city_display or bldg.get("display", task.city_key)
        prop_id = bldg["property_page_id"]
        semester = bldg["semester_id"]
        room_ids = bldg["room_type_ids"]
        proxy = get_proxy_url()
        proxies = {"https": proxy, "http": proxy} if proxy else {}

        fetcher = self._fetcher or self._ensure_browser()

        all_units: dict[str, dict] = {}
        complete = True

        # 串行，不再用 ThreadPoolExecutor：Playwright 的对象绑定创建线程，
        # 浏览器传输层不能跨线程并发调用。实际也没损失——原来的 4 个 worker
        # 共享同一把 1.5s 全局限流锁，吞吐本来就等于串行。
        for room_id in room_ids:
            with self._rate_lock:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < 1.5:
                    time.sleep(1.5 - elapsed)
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
        bookable_fp_ids = self._verify_bookable_floorplans(
            list(all_units.values()), today, proxies, display,
        )

        listings = [
            _to_listing(
                u, display=display, building_url=bldg.get("url", ""),
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
        proxies: dict,
        display: str,
    ) -> Optional[set[int]]:
        """对窗口内的候选可订单元，抓 floorplans.aspx 求权威可订 floorplan 集合。

        - 无候选 → 返回 None（不 gate，省一次请求）
        - 有候选但无法推导/抓取 floorplans.aspx → 返回 None（fail-open）
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
        with req.Session(impersonate=get_impersonate(), proxies=proxies) as vs:
            ids = _fetch_bookable_floorplan_ids(vs, fp_url)
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
            if code and int(code) != _UPSTREAM_NO_AVAILABILITY:
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

_STATUS_MAP = {
    "notice unrented": "Available to book",
    "vacant unrented not ready": "Available in lottery",
}

# 这些状态才算「可订/可抽签」，受可用日期窗口约束。
_AVAILABLE_STATUSES = ("Available to book", "Available in lottery")

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
_FP_APPLY_BTN = re.compile(r'<button[^>]*data-selenium-id\s*=\s*"ApplyNow"[^>]*>')
_FP_FLOORPLAN_ID = re.compile(r'floorPlans=(\d+)')


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
        if m:
            ids.add(int(m.group(1)))
    return ids


def _fetch_bookable_floorplan_ids(
    session: req.Session, url: str
) -> Optional[set[int]]:
    """GET floorplans.aspx 并解析可订 floorplan id 集合。

    返回 None 表示「无法判定」（网络异常 / 非 200 / Cloudflare challenge）——
    调用方据此 **fail-open**（信 WP feed，不漏报真房源）。
    """
    try:
        resp = session.get(url, timeout=30)
    except Exception as exc:
        logger.warning("Xior floorplans.aspx 请求异常 url=%s: %s", url, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Xior floorplans.aspx HTTP %d url=%s", resp.status_code, url)
        return None
    if is_cloudflare_body(resp.text):
        logger.warning("Xior floorplans.aspx 命中 Cloudflare challenge url=%s", url)
        return None
    return parse_bookable_floorplan_ids(resp.text)


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
            # bookable_floorplan_ids 为 None 时不进此分支（fail-open，信 feed）。
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
    ]
    if fp_name:
        features.append(f"Floorplan: {fp_name}")
    if sqm:
        features.append(f"Area: {sqm} m²")
    if deposit is not None and deposit > 0:
        features.append(f"Deposit: €{deposit}")
    elif deposit == 0:
        features.append("Deposit: €0")

    return Listing(
        id=f"xr_{apt_id}",
        name=f"{display} {apt_name}",
        status=status,
        price_raw=price_raw,
        available_from=avail_date,
        features=features,
        url=unit.get("applyOnlineURL") or building_url,
        city=display,
        source="xior",
    )


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
