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
- H2S 现有所有行为（dry_run / prewarmed session 复用 / 各种
  phase 分类）通过 ``HollandStayBooker`` thin-wrapper 原样转发
- 不支持下单的 source 走 ``phase="unsupported"`` 分支，``book_with_fallback``
  会过滤掉这些候选，不会产生误报"失败"通知
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from models import Listing
    from users import UserConfig


# ── 跨平台共用的结果类型 ────────────────────────────────────────────
#
# 原先定义在 H2S 的结账流程（当时的顶层 booker.py，现 bookers/h2s_checkout.py）里，
# 于是抽象层反过来依赖具体实现。现在住在这里，h2s_checkout 保留 re-export。

BookingPhase = Literal[
    "", "dry_run", "success", "race_lost",
    "reserved_conflict", "unknown_error",
    "blocked",
    # 403，但正文是上游应用说「这条 GraphQL operation 没登记」，不是 CF 屏蔽。
    # 和 blocked 分开的理由是上层动作完全相反：blocked 要换 IP、失效 session、
    # 暂停登录链路；这个换多少 IP 都一样，只能改代码把站点原文照抄回来。
    # 混在一起的代价见 OperationNotAllowedError 的 docstring。
    "operation_rejected",
    "unsupported",
    # 半自动预订：申请已起草并存在用户账号下，但**没有占住房**——
    # 还差用户自己上传证件和付款。与 "success" 必须分开：把它当成功报给
    # 用户，用户会以为房到手了、慢悠悠去传证件，结果被别人抢走。
    "draft_saved",
    # 前置校验没过（缺凭据 / 档案不完整），压根没触网。
    "not_configured",
    # 凭据被平台拒了。和 not_configured 分开：那个是"没填"，这个是"填了但不对"，
    # 用户看到的提示不一样；也和 blocked 分开——重试和换 IP 都救不回来。
    "auth_failed",
    # 凭据对，但账号开了两步验证，无法全自动（需人工输验证码）。和 auth_failed
    # 分开：不是账号密码的问题，提示用户去关 2FA 或改半自动。
    "auth_2fa",
    # 走到了最后一步但服务端拒绝保存。**绝不能报成 draft_saved**——那条消息
    # 让用户以为表单已填好、安心去传证件，而实际上什么都没存下。
    "save_rejected",
    # 占房已成立，但后续（支付方式 / 下单 / 支付链接）某一步失败。**绝不能报成
    # race_lost 或 unknown_error**：房正被这个账号占着，用户以为没抢到就不会去
    # 处理，既不付款也不取消，直到预留超时。必须把 cart/order 号给他。
    "held_incomplete",
    "uncertain",
]


@dataclass
@dataclass
class BookingResult:
    listing: Listing
    success: bool
    message: str
    dry_run: bool = False
    pay_url: str = ""
    contract_start_date: str = ""
    phase: BookingPhase = ""
    #: 占房已经成立、但后续步骤失败时的凭据。**必须交给用户。**
    #:
    #: create_booking 一旦返回，这套房就被这个账号占着了。此后
    #: set_payment_method / place_order / _ideal_checkout 任一失败，原实现只报
    #: 「预订失败」——用户以为没抢到，实际房在自己名下挂着，既不知道去哪付款，
    #: 也不知道要去取消，直到预留超时自动释放（或者更糟：一直占着）。
    held_cart_id: str = ""
    held_order_number: str = ""


class PrewarmedSession:
    """
    预认证的 BrowserFetcher + token，供 try_book() 直接复用。

    Attributes
    ----------
    fetcher    : 已过 CF 挑战的 BrowserFetcher 实例
    token      : NextAuth session 的 accessToken(JWT)，用作下单的 Bearer
    created_at : time.monotonic() 创建时刻
    email      : 对应的 H2S 账号邮箱
    owns_fetcher : 这个 fetcher 归本 session 所有吗。

        False 表示借的是常驻浏览器（mcore/warm_browser.py）。**借来的一律不许
        关**——它是全进程共用的那一个，关掉之后下一个用它的人拿到的是空壳，而
        且没人会察觉，只会看到「莫名其妙又要过一次挑战」。

    use_lock   : 借来时是那条常驻的锁，自有时为 None。

        一个 BrowserFetcher 不能被多线程同时使唤。持有者在整段 I/O 期间握着它，
        所以多个用户同时下单会串行——每笔下单是秒级，而实测可订窗口 p10 约 10
        分钟，这个代价换得起。
    """

    __slots__ = ("fetcher", "token", "created_at", "token_expiry", "email",
                 "owns_fetcher", "use_lock")

    def __init__(self, fetcher, token: str, created_at: float, token_expiry: float,
                 email: str, owns_fetcher: bool = True, use_lock=None):
        self.fetcher = fetcher
        self.token = token
        self.created_at = created_at
        self.token_expiry = token_expiry
        self.email = email
        self.owns_fetcher = owns_fetcher
        self.use_lock = use_lock

    def close_if_owned(self) -> None:
        """只关自己的。借来的常驻浏览器留给下一个人。"""
        if not self.owns_fetcher:
            return
        try:
            self.fetcher.close()
        except Exception:
            pass


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
