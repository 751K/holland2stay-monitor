"""
bookers/base.py — 自动下单层抽象（source-agnostic）
====================================================

P1 引入：把 H2S 的电商式 checkout 从 ``booker.try_book`` 单源耦合中解放出来。
每个 source 的下单实现一个 ``AbstractBooker`` 子类，由 ``bookers/__init__.py``
里的 ``BOOKER_REGISTRY`` 路由。

为什么独立成包（而不是寄居在 ``scrapers/`` 里）
------------------------------------------------
- 概念分离：scrape = 只读，book = 写操作 + 用户身份 + 支付 + 法律责任，
  混在一个类里会让 single-responsibility 失守
- 部分 source 只读不能下单（OurDomain 当前；HousingAnywhere 未来）；
  让 scraper 强行实现一个 no-op booker 反而模糊语义
- 单独包让"下单能力矩阵"在代码里一目了然：grep ``bookers/`` 看到哪些 source
  支持自动下单

零回归承诺
----------
- 复用 ``booker.BookingResult`` 作为统一返回类型——这样 ``notifier.py`` /
  ``monitor.py`` 那些读 ``result.pay_url`` / ``result.phase`` 的代码完全不动
- H2S 现有所有行为（dry_run / cancel_enabled / prewarmed session 复用 / 各种
  phase 分类）通过 ``HollandStayBooker`` thin-wrapper 原样转发
- 不支持下单的 source 走 ``phase="unsupported"`` 分支，``book_with_fallback``
  会过滤掉这些候选，不会产生误报"失败"通知
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

# 复用 booker.py 里的 BookingResult / BookingPhase——避免在多处定义
# 让 notifier / monitor 改判别类型。booker.py 里 BookingPhase Literal
# 已经 P1 阶段加了 "unsupported" 值。
from booker import BookingResult

if TYPE_CHECKING:
    from booker import PrewarmedSession
    from models import Listing
    from users import UserConfig


@dataclass
class BookingRequest:
    """
    跨 source 的自动下单请求。

    Fields
    ------
    listing       目标房源。``listing.source`` 决定路由到哪个 Booker
    user          用户配置（凭据、自动下单偏好）。Booker 自取 ``user.auto_book.*``
                  里它需要的字段；不同 source 可能用不同子集（H2S 用
                  email/password/payment_method，OurDomain 未来可能用 API key 等）
    dry_run       仅完成认证 / 资格检查，不真正提交订单
    prewarmed     可选预登录 session。只有支持的 source 会使用——
                  当前只有 H2S 用 ``PrewarmedSession``。其他 booker 收到时直接忽略
    """
    listing: "Listing"
    user: "UserConfig"
    dry_run: bool = False
    prewarmed: Optional["PrewarmedSession"] = None


class AbstractBooker(ABC):
    """
    每个支持自动下单的 source 实现一个子类，注册到 ``BOOKER_REGISTRY``。

    子类约定
    --------
    - 必须设 ``source: str`` 类属性（与对应 scraper 的 ``source`` + ``listing.source`` 一致）
    - 必须实现 ``book(request) -> BookingResult`` 同步方法
    - 失败时 **不抛异常**，返回 ``BookingResult(success=False, phase="...")``
      （由 ``book_with_fallback`` 决定要不要重试 / 通知）
    - ``BookingResult.phase`` 必须是 ``BookingPhase`` Literal 中的合法值

    线程模型
    --------
    book() 在 monitor 的 executor 线程里跑。Booker 实例可能被多个线程并发
    使用——内部状态自行加锁或每次新建。H2S 用的就是"每次新建 Session"策略。
    """

    # 子类必须覆盖。例：``source = "holland2stay"``
    source: str = ""

    @abstractmethod
    def book(self, request: BookingRequest) -> BookingResult:
        """
        对单个 listing 执行下单。

        Returns
        -------
        BookingResult，含 success / phase / message / pay_url 等字段。
        ``phase`` 是给上层 (``book_with_fallback``) 用的判别标签——
        ``"race_lost"`` 时上层会换备选房源继续尝试；其他 phase 立即返回。
        """
