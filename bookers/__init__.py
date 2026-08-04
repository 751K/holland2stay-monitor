"""
bookers — 多源自动下单层
========================

每个支持自动下单的 source 在子模块里实现 ``AbstractBooker``，注册到
``BOOKER_REGISTRY``。``mcore.booking.book_with_fallback`` 通过 ``dispatch_book``
按 ``listing.source`` 路由到具体实现。

支持矩阵（与 ``scrapers/SCRAPER_REGISTRY`` 对照）
-------------------------------------------------

===============  ===================  ==================  =========================
source           scraper              booker              状态
===============  ===================  ==================  =========================
holland2stay     HollandStayScraper   HollandStayBooker   完整自动 checkout，**已开**
xior             XiorScraper          XiorBooker          半自动到「存草稿」，未开
ourdomain        OurDomainScraper     OurDomainBooker     半自动到「存草稿」，未开
ourcampus        OurCampusScraper     （无）              没探过预订流程
===============  ===================  ==================  =========================

「未开」= 注册了 booker，但 ``monitor._AUTO_BOOK_SOURCES`` 里没有它，用户侧
这条路是关的。两个 RENTCafe 平台共用 ``RentCafeBooker``，边界都停在 Save
（往后要填 IBAN，代填金融凭据是硬限制）；开之前各自还差一段端到端验证，
见 ``docs/XIOR.md`` §8.6 / ``docs/OURDOMAIN.md`` §7。

**改这张表时同步改代码**：它上一次和现实脱节是因为 ``OurDomainBooker`` 加进
``BOOKER_REGISTRY`` 时没人回来改这段，于是文档写着「（无）」而代码里明明有
一个——查问题的人会先信文档。

不支持下单的 source（没 booker 注册）走 ``dispatch_book`` 的 unsupported 分支，
返回 ``BookingResult(phase="unsupported", success=False)``——调用方
（``book_with_fallback``）会过滤掉这些候选，不会产生"预订失败"误报通知。

新增 booker 步骤
----------------
1. ``bookers/{name}.py`` 实现 ``AbstractBooker`` 子类
2. 本文件底部 import + 加入 ``BOOKER_REGISTRY``
3. 在用户 ``auto_book_json`` 配置里加该 source 所需字段（如 API key / 凭据）
"""
from __future__ import annotations

from typing import Optional

from booker import BookingResult

from .base import AbstractBooker, BookingRequest
from .holland2stay import HollandStayBooker
from .rentcafe import XiorBooker, OurDomainBooker


BOOKER_REGISTRY: dict[str, type[AbstractBooker]] = {
    cls.source: cls for cls in [
        HollandStayBooker,
        XiorBooker,
        OurDomainBooker,
    ]
}


def get_booker(source: str) -> Optional[AbstractBooker]:
    """根据 source 名取一个 Booker 实例；未注册返回 None。"""
    cls = BOOKER_REGISTRY.get(source)
    return cls() if cls else None


def supports_booking(source: str) -> bool:
    """快速判断某 source 是否支持自动下单。用于过滤候选列表。"""
    return source in BOOKER_REGISTRY


def dispatch_book(request: BookingRequest) -> BookingResult:
    """
    按 ``request.listing.source`` 路由到对应 Booker。

    未注册的 source 返回 ``phase="unsupported"``——这是**正常**结果，不是错误。
    例如 OurCampus 用户收到新房源通知后应自己点链接去申请；把这条路径走出来
    比抛异常更友好（上层不需要 try/except）。
    """
    source = (request.listing.source or "").strip().lower()
    booker = get_booker(source)
    if booker is None:
        return BookingResult(
            listing=request.listing,
            success=False,
            message=(
                f"自动下单不支持 source={source!r}。请点击通知里的链接手动申请。"
            ),
            phase="unsupported",
        )
    return booker.book(request)


__all__ = [
    "AbstractBooker",
    "BOOKER_REGISTRY",
    "BookingRequest",
    "BookingResult",
    "HollandStayBooker",
    "dispatch_book",
    "get_booker",
    "supports_booking",
]
