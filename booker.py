"""
booker.py — 自动预订模块（CloakBrowser 版）
==============================================

对 "Available to book" 的房源执行完整的自动化预订流程，最终生成可直接支付的链接。

完整流程（try_book 内部）
--------------------------
1. _fetch_sku_and_contract() [fallback，pre-extracted 时跳过]
       通过 url_key 查询 Magento SKU + type_of_contract ID + 下一个入住日期
2. login()
       NextAuth 三步握手（csrf → callback/credentials → session）→ accessToken(JWT)
       ——2026-08-19 起 H2S 登录已从 GraphQL mutation 迁到 NextAuth，见
       docs/H2S_BOOKING_OPS.md §6
3. _do_book()（内部子流程，失败时可重试）：
   3a+3b. create_booking()
           POST /api/booking（加密信封 + Bearer）→ {cartId, booking}
           一步完成「建车 + 占房」。**这是唯一的占房入口。**

           曾经是 createEmptyCart + addNewBooking 两步。后者 2026-08-19 实测
           被 H2S 摘出公开 API（403 "not available through the public API"），
           站点自己的前端也改走了 /api/booking。那两个函数一度保留着「万一
           要退回去」，但没有任何代码路径会走到它们——「保留作 fallback」在
           没接线的情况下只是错觉（我们在 BookingBlockedError 上栽过同样的坑）。
           2026-08-20 删除，要找原文去 git 历史或 h2s_booking_gql（那份是站点
           报文的照抄记录，不是我们的调用清单）。

           ⚠️ 这条路径**尚未经过一次真实预订验证**——验证它就等于真占一套房。
           首次真实自动预订时需要盯着结果。见 docs/H2S_BOOKING_OPS.md §6.10
   3c. set_payment_method()
           setPaymentMethodOnCart mutation → code="idealcheckout_ideal"
   3d. _fetch_checkout_agreements()
           查询 checkout 协议条款（与浏览器行为对齐，fail-open）
   3e. place_order()
           placeOrder mutation（含 store_id）→ orderV2.order_number
   3e. _ideal_checkout()
           idealCheckOut mutation → redirect（直链付款 URL）

下单 operation 全部照抄站点原文（``h2s_booking_gql``）。白名单只认 operationName +
完整字段集，自写的查询一律 403 ``operation_not_allowed``——历史与判据见
docs/H2S_BOOKING_OPS.md 与 scrapers.base.OperationNotAllowedError。

传输层
------
已从 curl_cffi → CloakBrowser（BrowserFetcher）。下单 GraphQL 走加密信道
（``/api/__enc__``）；登录用的 NextAuth 端点是明文 REST，走 ``fetch_plain``
（不套加密信封）。Bearer accessToken 通过 extra_headers 传递。

验证状态（2026-08-19 逐条实测，用无效参数探白名单，零副作用）
------------------------------------------------------------
    login（NextAuth）        ✅ 真实账号走通
    setPaymentMethodOnCart   ✅ 200（回 CART_NOT_FOUND = 过白名单）
    GetCheckoutAgreements    ✅ 200
    placeOrder               ✅ 200（同上）
    idealCheckOut            ✅ 200（同上）
    createEmptyCart          ✅ 200（但流程已不用它）
    addNewBooking            ❌ 403「not available through the public API」——已弃用
    POST /api/booking        ✅ 端点存在（未登录回 401，非 404）；**带真实登录态的
                                 完整占房未验证**，会在首次真实预订时见分晓
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime as _dt
from typing import Literal, Optional

import json as _json
from urllib.parse import urlencode as _urlencode

from browser_fetcher import BrowserFetcher
from h2s_booking_gql import (
    GETCHECKOUTAGREEMENTS as _GQL_AGREEMENTS,
    GETPRODUCTDETAIL as _GQL_PRODUCT_DETAIL,
    IDEALCHECKOUT as _GQL_IDEAL,
    PLACEORDER as _GQL_PLACE_ORDER,
    SETPAYMENTMETHODONCART as _GQL_SET_PAYMENT,
    OP_GETCHECKOUTAGREEMENTS as _OP_AGREEMENTS,
    OP_GETPRODUCTDETAIL as _OP_PRODUCT_DETAIL,
    OP_IDEALCHECKOUT as _OP_IDEAL,
    OP_PLACEORDER as _OP_PLACE_ORDER,
    OP_SETPAYMENTMETHODONCART as _OP_SET_PAYMENT,
)
from models import STATUS_AVAILABLE, Listing
from scrapers.base import BlockedError, OperationNotAllowedError

logger = logging.getLogger(__name__)


class PrewarmedSession:
    """
    预认证的 BrowserFetcher + token，供 try_book() 直接复用。

    Attributes
    ----------
    fetcher    : 已过 CF 挑战的 BrowserFetcher 实例
    token      : NextAuth session 的 accessToken(JWT)，用作下单的 Bearer
    created_at : time.monotonic() 创建时刻
    email      : 对应的 H2S 账号邮箱
    """

    __slots__ = ("fetcher", "token", "created_at", "token_expiry", "email")

    def __init__(self, fetcher, token: str, created_at: float, token_expiry: float, email: str):
        self.fetcher = fetcher
        self.token = token
        self.created_at = created_at
        self.token_expiry = token_expiry
        self.email = email


# Magento store_id
_H2S_STORE_ID = 54

# setPaymentMethodOnCart 使用的支付方式代码
_PAYMENT_METHOD = "idealcheckout_ideal"

# Magento token 有效期约 1 小时，设 55 分钟上限保留缓冲
_TOKEN_MAX_AGE = 3300

# ── 登录（NextAuth）────────────────────────────────────────────────
# 2026-08-19 起 H2S 登录不再是 GraphQL ``generateCustomerToken``，换成了
# 标准 NextAuth credentials 流（docs/H2S_BOOKING_OPS.md §6）。三步握手，全部
# 在 www 同源、明文（不走加密信封）：
#   GET  /api/auth/csrf                 → { csrfToken }
#   POST /api/auth/callback/credentials → 表单 { email, password, csrfToken, … }
#   GET  /api/auth/session              → { accessToken(JWT), requires2fa, … }
# 拿到的 accessToken 就是后续所有下单 GraphQL 的 Bearer。
_AUTH_CSRF_PATH = "/api/auth/csrf"
_AUTH_CALLBACK_PATH = "/api/auth/callback/credentials"
_AUTH_SESSION_PATH = "/api/auth/session"
_AUTH_CALLBACK_URL = "https://www.holland2stay.com/"

# ── 占房入口（2026-08-19 实测确定）──────────────────────────────────
# GraphQL ``addNewBooking`` **已被摘出公开 API**：同一 clearance 窗口内实测
#     createEmptyCart          → 200（真建了车）
#     addNewBooking            → 403 {"error":"This operation is not available
#                                      through the public API"}
#     setPaymentMethodOnCart   → 200（回 CART_NOT_FOUND = 过了白名单）
#     placeOrder / idealCheckOut → 200（同上）
# 即 6 步里只有占房这一步被摘掉。站点自己也不再直接发它，改走服务端代理
# ``POST /api/booking``（加密信封 + Bearer），一次完成建车 + 占房，返回
# {cartId, booking}。实测该端点存在（未登录时回 401 Unauthorized，而非 404 /
# operation_not_allowed）。详见 docs/H2S_BOOKING_OPS.md §6.10。
_BOOKING_API_PATH = "/api/booking"


class AuthError(Exception):
    """登录被平台拒绝（凭据错误）。换 IP / 重试都无意义——要用户改账号密码。"""


def resolve_start_date(*candidates: "str | None") -> "str | None":
    """按顺序挑第一个**可用**的入住日；都不可用返回 None。

    两道判据，缺一不可：

    ``is_sentinel_available_from``
        ``2050-01-01`` 是上游「没有下一个合同起始日」的哨兵，不是日期。它比今天
        大，所以下面那道 ``>= today`` 挡不住它——挡不住就会带着一个平台从没承诺过
        的入住日去占房。
    ``>= today``
        过期日期同样不能传。

    抽成函数是因为这段逻辑原先在 booker 里**抄了两份**（try_book 的 Step 1 与
    ``_extract_sku_contract``），两份都只有第二道判据。同一段逻辑写两遍，改的时候
    一定只改一处。
    """
    from datetime import date as _date

    from models import is_sentinel_available_from

    today = _date.today().isoformat()
    for c in candidates:
        c = (c or "").strip()
        if not c:
            continue
        if is_sentinel_available_from(c):
            logger.warning("入住日 %s 是哨兵值，不采用", c)
            continue
        if c >= today:
            return c
    return None


class BookingHeldButIncomplete(Exception):
    """占房已成立，但后续步骤失败。

    ``create_booking`` 一返回，这套房就被账号占着了。此后任何一步失败都不改变
    这个事实——所以**不能报成普通失败**。用户需要 cart/order 号才能自己去付款
    或取消；没有它，房就一直挂在那里直到预留超时。

    刻意不做自动回滚：取消不可逆，而失败可能只是支付链接生成超时（房还好好占着，
    登录站点就能付）。把事实交出去，由用户决定。
    """

    def __init__(self, *, cart_id: str, order_number: str,
                 booking_url: str, original: str) -> None:
        self.cart_id = cart_id
        self.order_number = order_number
        self.booking_url = booking_url
        self.original = original
        super().__init__(
            "占房已成立，但后续步骤失败：" + original)


class TwoFactorRequiredError(Exception):
    """登录成功但账号开了两步验证，需要人工输入验证码，无法全自动下单。

    与 AuthError 分开：凭据是对的，只是这个账号过不了全自动。给用户的提示也不同
    （去关掉 2FA 或改用半自动），见 docs/H2S_BOOKING_OPS.md §6.5。
    """


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email[:3] + "***" if len(email) > 3 else "***"
    local, domain = email.split("@", 1)
    masked = local[:3] + "***" if len(local) > 3 else "***"
    return f"{masked}@{domain}"


# ------------------------------------------------------------------ #
# 日期格式转换
# ------------------------------------------------------------------ #

def _to_h2s_date(iso_date: str) -> str:
    """将 ISO 日期转换为 H2S API 要求的 DD-MM-YYYY 格式。"""
    if not iso_date:
        raise ValueError("iso_date 不能为空")
    try:
        return _dt.strptime(iso_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        raise ValueError(f"日期格式错误，期望 YYYY-MM-DD，实际为: {iso_date!r}") from None


# ------------------------------------------------------------------ #
# Cloudflare WAF 屏蔽检测
# ------------------------------------------------------------------ #
#
# 这里曾经有一个 ``BookingBlockedError``，声称「booker 捕获 BlockedError 后转成
# 它，让上层区分预订层屏蔽与其它 BlockedError」。**没有任何地方 raise 过它。**
#
# 后果不是「多了个没用的类」，而是三处 ``except BookingBlockedError`` 从来没有
# 触发过：prewarm 上抛的一直是裸 BlockedError，于是每次都落进后面的
# ``except Exception: ps = None``，静默降级成「回退正常登录」，
# ``_mark_h2s_login_blocked()`` 一次都没被调用过。CF 真把我们挡了的时候，
# 登录抑制窗口形同虚设——照常每轮再去撞一次。
#
# 现在上层直接 ``except BlockedError``。那层「区分」本来也不存在意义：
# prewarm 这条路上唯一的异常来源就是预订层自己。
# 需要和 CF 屏蔽分开的是 operation 未放行，那是 OperationNotAllowedError，
# 它**不继承** BlockedError，所以不会被这些 handler 接住——正是要的效果。

# ------------------------------------------------------------------ #
# 错误分类（placeOrder 业务错误识别）
# ------------------------------------------------------------------ #

def _is_booked_by_other(msg: str) -> bool:
    return "already booked by someone else" in msg.lower()


def _is_reserved_by_user(msg: str) -> bool:
    low = msg.lower()
    return (
        "another unit reserved" in low
        or "you have another" in low
        or "at the moment you have" in low
    )


# ------------------------------------------------------------------ #
# GraphQL helper
# ------------------------------------------------------------------ #

def _gql(
    fetcher: BrowserFetcher,
    query: str,
    token: Optional[str] = None,
    variables: Optional[dict] = None,
    *,
    operation_name: str = "",
) -> dict:
    """
    执行 GraphQL 查询/变更并返回 data 字段。

    Parameters
    ----------
    fetcher   : BrowserFetcher 实例
    query     : GraphQL 查询或 mutation 字符串
    token     : Bearer token，传入时附加 Authorization 头
    variables : GraphQL variables dict

    Returns
    -------
    响应 JSON 的 data 字段（dict）

    Raises
    ------
    BlockedError          HTTP 403 (CF 屏蔽)
    ScrapeNetworkError    网络错误
    RuntimeError          响应含 errors 字段时（GraphQL 层错误）

    注意
    ----
    此函数不处理 partial error（GraphQL 的 NON_NULL 传播会同时给出 errors 和
    部分 data）。需要区分「致命」与「已生效但带告警」的调用方要直接用
    ``fetcher.fetch_gql()``。
    """
    extra_headers = {}
    if token:
        extra_headers["Authorization"] = f"Bearer {token}"

    # operation_name 必须带上：H2S 白名单缺 operationName 同样 403（见
    # h2s_booking_gql 模块文档）。旧代码一处都没传——这是历史 403 的成因之一。
    data = fetcher.fetch_gql(
        query, variables=variables, operation_name=operation_name,
        extra_headers=extra_headers,
    )

    if "errors" in data:
        msgs = "; ".join(e.get("message", "") for e in data["errors"])
        raise RuntimeError(f"GraphQL 错误: {msgs}")
    return data.get("data", {})


# ------------------------------------------------------------------ #
# 登录
# ------------------------------------------------------------------ #

def login(fetcher: BrowserFetcher, email: str, password: str) -> str:
    """走 NextAuth credentials 流登录，返回后续下单要用的 Bearer accessToken。

    三步握手，全部同源、明文（``fetch_plain``，不走加密信封）：
        1. GET  /api/auth/csrf                 → csrfToken
        2. POST /api/auth/callback/credentials → 提交凭据（表单）
        3. GET  /api/auth/session              → accessToken(JWT)

    Raises
    ------
    AuthError               凭据被拒（callback 401 / session 无 token）
    TwoFactorRequiredError  账号开了 2FA，无法全自动
    ScrapeNetworkError      网络/传输失败
    RuntimeError            响应结构异常

    历史：2026-08-19 前这里是 GraphQL ``generateCustomerToken`` mutation；H2S 把
    登录整体迁到 NextAuth 后，那条 mutation 已不存在（打过去 operation_not_allowed）。
    见 docs/H2S_BOOKING_OPS.md §6。
    """
    # 1. CSRF token
    r = fetcher.fetch_plain(_AUTH_CSRF_PATH, method="GET")
    try:
        csrf = _json.loads(r["text"]).get("csrfToken")
    except _json.JSONDecodeError as e:
        raise RuntimeError(f"登录第 1 步 CSRF 响应非 JSON: {e}") from e
    if not csrf:
        raise RuntimeError("登录第 1 步未取得 csrfToken")

    # 2. 提交凭据。NextAuth callback 是 x-www-form-urlencoded；redirect=false +
    #    json=true 让它回 JSON 而不是 302，便于判成败。
    form = _urlencode({
        "email": email,
        "password": password,
        "csrfToken": csrf,
        "callbackUrl": _AUTH_CALLBACK_URL,
        "redirect": "false",
        "json": "true",
    })
    r2 = fetcher.fetch_plain(
        _AUTH_CALLBACK_PATH,
        method="POST",
        body=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # NextAuth 凭据错误：callback 回 401，或回 200 但 body 带 error。
    if r2["status"] == 401:
        raise AuthError("登录被拒：邮箱或密码错误（callback 401）")
    if r2["status"] not in (200, 302):
        raise RuntimeError(
            f"登录第 2 步异常 HTTP {r2['status']}: {r2.get('text','')[:200]}"
        )
    try:
        cb = _json.loads(r2["text"]) if r2.get("text") else {}
    except _json.JSONDecodeError:
        cb = {}
    if isinstance(cb, dict) and cb.get("error"):
        raise AuthError(f"登录被拒: {cb['error']}")

    # 3. 取 session 里的 accessToken
    r3 = fetcher.fetch_plain(_AUTH_SESSION_PATH, method="GET")
    try:
        sess = _json.loads(r3["text"])
    except _json.JSONDecodeError as e:
        raise RuntimeError(f"登录第 3 步 session 响应非 JSON: {e}") from e

    # 空 session = 凭据没通过（NextAuth 对未认证返回 {} 或无 user）
    if not sess or not sess.get("accessToken"):
        if sess.get("requires2fa") or sess.get("twoFaPending"):
            raise TwoFactorRequiredError(
                "账号开启了两步验证，无法全自动下单（需要人工输入验证码）"
            )
        raise AuthError("登录未生效：session 无 accessToken（凭据可能有误）")

    if sess.get("requires2fa") or sess.get("twoFaPending"):
        # 极少数情况下 accessToken 已发但仍标记待验证——按 2FA 处理更安全，
        # 否则拿一个未完成校验的 token 去下单会在后续步骤莫名失败。
        raise TwoFactorRequiredError(
            "登录返回 token 但仍待两步验证，无法全自动下单"
        )

    logger.debug("登录成功（NextAuth，accessToken 已取得）")
    return sess["accessToken"]


# ------------------------------------------------------------------ #
# 设置支付方式
# ------------------------------------------------------------------ #

def set_payment_method(
    fetcher: BrowserFetcher,
    token: str,
    cart_id: str,
    code: str = _PAYMENT_METHOD,
) -> None:
    data = _gql(fetcher, _GQL_SET_PAYMENT, token=token,
                variables={"cartId": cart_id, "paymentMethod": {"code": code}},
                operation_name=_OP_SET_PAYMENT)
    selected = (
        (data.get("setPaymentMethodOnCart") or {})
        .get("cart", {})
        .get("selected_payment_method", {})
        .get("code")
    )
    logger.info("支付方式已设置: %s", selected or code)


# ------------------------------------------------------------------ #
# 取消 pending 订单
# ------------------------------------------------------------------ #

# 取消预留：REST，不是 GraphQL。机制 2026-08-19 从租户门户 JS 逐字读出
# （docs/H2S_BOOKING_OPS.md §6.6），站点原文：
#     列出预留  GET  /api/rest/V1/newdashboard/contract/me?fields=items[id,sku,status,...]
#     取消一笔  POST /api/rest/V1/customer/bookingcancel/{sku}   body {}   Bearer
# 按 **SKU** 取消，不是订单号。旧代码那套 GraphQL customer{orders}+cancelOrder
# 是自写的，站点根本不这么做，必然失败——已删除。
_REST_LIST_RESERVATIONS = (
    "/api/rest/V1/newdashboard/contract/me"
    "?fields=items[id,sku,product_name,building_name,status,start_date]"
)
_REST_BOOKING_CANCEL = "/api/rest/V1/customer/bookingcancel/{sku}"
_CANCEL_STATUSES = {"pending", "pending_payment", "reserved", "processing", "reservation"}


#: 「这笔预留是我们下的」记录的 meta 键前缀。按账号分开存。
_OUR_RESERVATIONS_META = "booking:our_skus:"
#: 每个账号最多记多少条。预留是短命的，留太多只会让旧记录误伤。
_OUR_RESERVATIONS_CAP = 20


def _account_key(email: str) -> str:
    import hashlib
    return hashlib.sha1((email or "").strip().lower().encode()).hexdigest()[:16]


def _meta_storage():
    """惰性拿一个 Storage。拿不到返回 None（调用方据此 fail-safe）。

    booker 与它上面两层（``book_with_fallback`` / ``dispatch_book``）都没有
    storage 参数，而把它一路穿下来会改三个签名。这里按 crypto.py 同一个办法
    自己开一个只读/写 meta 的连接——记录的量极小，不值得为它改调用链。
    """
    try:
        from pathlib import Path

        from config import load_config
        from storage import Storage
        return Storage(Path(load_config().db_path))
    except Exception:
        logger.warning("拿不到 storage，无法记录/读取自有预留", exc_info=True)
        return None


def remember_our_reservation(email: str, sku: str) -> None:
    """记下「这个 SKU 是我们替这个账号占的」。

    ``cancel_pending_orders`` 只取消记在这里的那些——见它的 docstring。
    记不下来不影响下单，只会让将来那次取消变保守（不敢动）。
    """
    if not sku:
        return
    st = _meta_storage()
    if st is None:
        return
    import json as _j
    key = _OUR_RESERVATIONS_META + _account_key(email)
    try:
        cur = _j.loads(st.get_meta(key, "") or "[]")
        if not isinstance(cur, list):
            cur = []
    except Exception:
        cur = []
    if sku in cur:
        return
    cur.append(sku)
    try:
        st.set_meta(key, _j.dumps(cur[-_OUR_RESERVATIONS_CAP:]))
    except Exception:
        logger.warning("记录自有预留失败（下次取消会变保守）", exc_info=True)


def _our_reservations(email: str) -> set[str]:
    st = _meta_storage()
    if st is None:
        return set()
    import json as _j
    try:
        cur = _j.loads(st.get_meta(_OUR_RESERVATIONS_META + _account_key(email), "")
                       or "[]")
        return {str(x) for x in cur} if isinstance(cur, list) else set()
    except Exception:
        return set()


def cancel_pending_orders(fetcher: BrowserFetcher, token: str,
                          email: str = "") -> int:
    """取消**我们自己下的**待处理预留，返回取消成功的笔数。

    ⚠️ 判据不能只看 status
    ----------------------
    原实现按 ``status in _CANCEL_STATUSES`` 取消账号下的**全部**待处理预留。
    那里面完全可能有用户自己在站点上手动订的房——为了抢另一套，把人家看中的
    那套取消掉，而且**不可逆**。

    现在只取消 ``remember_our_reservation`` 记过的 SKU。没有记录时**一笔都不
    取消**（fail-safe）：宁可这次抢不到，也不能动用户自己的预留。记录会在进程
    重启后保留（存在 meta 里），但更早的、或别的部署下的预留仍然认不出——那种
    情况下保守是对的。

    传输层已确定（2026-08-19 逐字读自站点 module 82361，见 §6.9）：
    ``/api/rest/*`` **走加密信封**——GET 把路径塞进 ``x-enc-q``，POST 加密 body。
    所以这里用 ``fetch_rest``，不是 ``fetch_plain``（后者只对 ``/api/auth/*`` 正确）。

    ⚠️ 仍未经真实取消验证：这条路只在 reserved_conflict + cancel_enabled 时触发
    （边缘场景），且整体 try/except 兜底——取消失败只是救不回旧预留，不连累主流程。
    """
    import json as _j

    hdr = {"Authorization": f"Bearer {token}"}
    try:
        r = fetcher.fetch_rest(_REST_LIST_RESERVATIONS, method="GET", headers=hdr)
        items = (_j.loads(r["text"]) or {}).get("items") or []
    except Exception as e:
        logger.warning("查询预留列表失败（忽略，机制见 §6.6/§6.9）: %s", e)
        return 0

    ours = _our_reservations(email)
    pending = [
        (it.get("sku"), it.get("product_name") or it.get("sku"))
        for it in items
        if it.get("sku") and str(it.get("status", "")).lower() in _CANCEL_STATUSES
    ]
    to_cancel = [(sku, name) for sku, name in pending if str(sku) in ours]

    skipped = [name for sku, name in pending if str(sku) not in ours]
    if skipped:
        # **必须吵一声。** 静默跳过会让人以为「取消功能坏了」，进而去把判据放宽
        # 回原样——而那正是会误删用户手动预留的写法。
        logger.warning(
            "账号下有 %d 笔待处理预留不是我们下的，已跳过不取消：%s。"
            "如需释放请自行到站点操作", len(skipped), skipped,
        )
    if not to_cancel:
        logger.info("没有可安全取消的自有预留（待处理 %d 笔，其中自有 0 笔）",
                    len(pending))
        return 0

    logger.info("发现 %d 笔待处理预留，准备取消: %s",
                len(to_cancel), [n for _, n in to_cancel])

    cancelled = 0
    for sku, name in to_cancel:
        try:
            resp = fetcher.fetch_rest(
                _REST_BOOKING_CANCEL.format(sku=sku),
                method="POST", body="{}",
                headers={**hdr, "Content-Type": "application/json"},
            )
            if 200 <= resp.get("status", 0) < 300:
                logger.info("已取消预留 %s (%s)", name, sku)
                cancelled += 1
            else:
                logger.warning("取消预留 %s 返回 HTTP %s: %s",
                               sku, resp.get("status"), resp.get("text", "")[:200])
        except Exception as e:
            logger.warning("取消预留 %s 失败: %s", sku, e)

    return cancelled


# ------------------------------------------------------------------ #
# 加入购物车（预占位）
# ------------------------------------------------------------------ #

def create_booking(
    fetcher: BrowserFetcher,
    token: str,
    sku: str,
    contract_start_date: Optional[str],
) -> str:
    """走站点自己的占房入口 ``POST /api/booking``，返回 cart_id。

    ★ 有副作用：这一步就占住房了。

    **这是唯一的占房入口。** 曾经是 ``createEmptyCart`` + ``addNewBooking``
    两步，后者已被 H2S 摘出公开 API（实测 403，见 ``_BOOKING_API_PATH`` 上方
    注释），站点自己现在也走这条。那两个函数 2026-08-20 删除——留着但没接线
    的「fallback」只是错觉。

    请求体照抄站点（``zg`` 加密后 POST）：
        {sku, contract_startDate, challengeToken, challengeProvider}

    ``challengeToken`` 是站点自有 clearance 的 Turnstile token
    （``action:"clearance"`` / ``appearance:"interaction-only"``）。我们的
    BrowserFetcher 已经在浏览器里完成了那套 clearance，cookie 就位；这里传空串，
    由服务端按 cookie 判定。若线上回「需要验证」，说明服务端要显式 token，
    届时需从页面 ``window.turnstile`` 取。

    Raises
    ------
    AuthError    401（token 失效 / 未登录）
    RuntimeError 其余非 2xx，或响应里没有 cartId
    """
    payload = {
        "sku": sku,
        "contract_startDate": _to_h2s_date(contract_start_date) if contract_start_date else None,
        "challengeToken": "",
        "challengeProvider": "turnstile",
    }
    resp = fetcher.fetch_encrypted_json(
        _BOOKING_API_PATH,
        body=_json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    status = resp.get("status", 0)
    text = resp.get("text", "") or ""

    if status == 401:
        raise AuthError(f"占房被拒 401（登录态失效）: {text[:200]}")
    if not (200 <= status < 300):
        raise RuntimeError(f"占房失败 HTTP {status}: {text[:300]}")

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError as e:
        raise RuntimeError(f"占房响应非 JSON: {e}; 原文 {text[:200]}") from e

    cart_id = data.get("cartId")
    if not cart_id:
        raise RuntimeError(
            f"占房未返回 cartId（可能未真正占到）: {str(data)[:300]}"
        )
    if not data.get("booking"):
        # 站点前端也检查这个字段——没有它说明车建了但没占上房
        raise RuntimeError(f"占房未生效（无 booking 字段）: {str(data)[:300]}")

    logger.info("占房成功，cart_id=%s", cart_id)
    return cart_id


# ------------------------------------------------------------------ #
# 下单
# ------------------------------------------------------------------ #

def _fetch_checkout_agreements(fetcher: BrowserFetcher, token: str) -> None:
    """
    查询 checkout 协议条款（与浏览器行为对齐）。

    H2S 前端在渲染支付页 / 下单前会调用 GetCheckoutAgreements，
    某些 Magento 实例要求必须先接受协议才能 placeOrder。
    本函数仅做查询 + 日志记录，fail-open：失败不阻塞下单。
    """
    try:
        data = _gql(fetcher, _GQL_AGREEMENTS, token=token,
                    operation_name=_OP_AGREEMENTS)
        ags = data.get("checkoutAgreements") or []
        logger.debug("checkout 协议: %d 条", len(ags))
    except Exception as e:
        logger.warning("GetCheckoutAgreements 失败（非致命，继续下单）: %s", e)


def place_order(
    fetcher: BrowserFetcher,
    token: str,
    cart_id: str,
    store_id: int = _H2S_STORE_ID,
) -> str:
    """调用站点原文的 placeOrder mutation 将购物车转为正式订单，返回订单号。

    ★ 有副作用：这一步产生真实订单。
    """
    data = _gql(fetcher, _GQL_PLACE_ORDER, token=token,
                variables={"cartId": cart_id, "storeId": store_id},
                operation_name=_OP_PLACE_ORDER)
    result = data.get("placeOrder") or {}

    errors = result.get("errors") or []
    if errors:
        msgs = "; ".join(
            f"[{e.get('code','?')}] {e.get('message','')}" for e in errors
        )
        logger.warning(
            "placeOrder 业务错误 cart_id=%s store_id=%d: %s",
            cart_id, store_id, msgs,
        )
        raise RuntimeError(f"下单失败: {msgs}")

    order_number = (result.get("orderV2") or {}).get("order_number")
    if not order_number:
        raise RuntimeError("placeOrder 未返回订单号（orderV2.order_number 为空）")

    logger.info("订单已创建: #%s", order_number)
    return order_number


# ------------------------------------------------------------------ #
# 生成支付链接
# ------------------------------------------------------------------ #

def _ideal_checkout(fetcher: BrowserFetcher, token: str, order_number: str) -> str:
    """调用站点原文的 idealCheckOut mutation 生成 iDEAL 直链付款 URL。"""
    tp0 = time.monotonic()
    try:
        data = _gql(fetcher, _GQL_IDEAL, token=token,
                    variables={"order_id": order_number, "plateform": "h"},
                    operation_name=_OP_IDEAL)
    except Exception as e:
        logger.error("idealCheckOut 失败 (%.2fs): %s", time.monotonic() - tp0, e)
        raise
    pay_url = (data.get("idealCheckOut") or {}).get("redirect")
    if not pay_url:
        raise RuntimeError(
            f"idealCheckOut 未返回支付链接 (order #{order_number})"
        )
    logger.info("支付链接已生成 (%.2fs)", time.monotonic() - tp0)
    return pay_url


# ------------------------------------------------------------------ #
# 主入口
# ------------------------------------------------------------------ #

BookingPhase = Literal[
    "", "dry_run", "success", "race_lost",
    "reserved_conflict", "cancel+retry", "unknown_error",
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
]


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


def create_prewarmed_session(email: str, password: str) -> PrewarmedSession:
    """
    创建已登录的 BrowserFetcher，供 try_book() 直接复用。

    调用方负责在使用完毕后调用 ps.fetcher.close() 释放浏览器。
    """
    from config import CLOAKBROWSER_HEADLESS

    fetcher = BrowserFetcher(headless=CLOAKBROWSER_HEADLESS)
    fetcher.__enter__()
    try:
        token = login(fetcher, email, password)
    except Exception:
        fetcher.__exit__(None, None, None)
        raise
    now = time.monotonic()
    return PrewarmedSession(
        fetcher=fetcher,
        token=token,
        created_at=now,
        token_expiry=now + _TOKEN_MAX_AGE,
        email=email,
    )


def try_book(
    listing: Listing,
    email: str,
    password: str,
    *,
    dry_run: bool = False,
    cancel_enabled: bool = False,
    payment_method: str = _PAYMENT_METHOD,
    prewarmed: "PrewarmedSession | None" = None,
) -> BookingResult:
    """
    对单个 "Available to book" 房源执行完整的自动预订流程。

    流程
    ----
    createEmptyCart → addNewBooking → setPaymentMethodOnCart → placeOrder → idealCheckOut

    重试策略
    --------
    placeOrder 返回「房源已被他人预订」→ 竞争失败，立即通知用户（不重试）。
    placeOrder 返回「账号已有预留单」且 cancel_enabled=True
      → cancel_pending_orders() → 重新执行 _do_book()。
    """
    if listing.status.lower() != STATUS_AVAILABLE:
        return BookingResult(listing, False, f"状态不是 Available to book: {listing.status}")

    t0 = time.monotonic()
    t_cancel = 0.0
    t_login = 0.0
    t_sku = 0.0
    phase: BookingPhase = ""

    # ---------------------------------------------------------------- #
    # Step 1: 确定 SKU / contract_id / contract_start_date
    # ---------------------------------------------------------------- #
    if listing.sku:
        sku = listing.sku
        contract_id = listing.contract_id
        # 这道防线是**独立**于 scraper 那道的：库里可能还留着修复之前写进去的行。
        start_date = resolve_start_date(
            listing.contract_start_date, listing.available_from)
        logger.info(
            "[%s]%s SKU: %s  contract_id: %s  start_date: %s  (pre-extracted)",
            listing.name, " [DRY RUN]" if dry_run else "",
            sku, contract_id, start_date or "(不传，由服务端决定)",
        )

    # 决定 BrowserFetcher 来源：预登录复用 or 按需创建
    now = time.monotonic()
    using_prewarmed = prewarmed is not None and now < prewarmed.token_expiry
    own_fetcher = False

    if using_prewarmed:
        fetcher = prewarmed.fetcher      # type: ignore[union-attr]
        token = prewarmed.token          # type: ignore[union-attr]
        logger.debug("复用预登录 BrowserFetcher (email=%s)", _mask_email(email))
    else:
        if prewarmed is not None:
            age = now - prewarmed.created_at
            logger.warning(
                "预登录 session 已过期 (%.0f 秒前创建，上限 %d 秒)，退回正常登录",
                age, _TOKEN_MAX_AGE,
            )
            try:
                prewarmed.fetcher.close()
            except Exception:
                pass
        from config import CLOAKBROWSER_HEADLESS

        fetcher = BrowserFetcher(headless=CLOAKBROWSER_HEADLESS)
        fetcher.__enter__()
        own_fetcher = True

    try:
        # ---- Step 1 fallback ---- #
        if not listing.sku:
            t1 = time.monotonic()
            sku, contract_id, start_date = _fetch_sku_and_contract(fetcher, listing.id)
            t_sku = time.monotonic() - t1
            logger.info(
                "[%s]%s SKU: %s  contract_id: %s  start_date: %s  (%.2fs) [fallback]",
                listing.name, " [DRY RUN]" if dry_run else "",
                sku, contract_id, start_date or "(不传，由服务端决定)", t_sku,
            )

        # ---- Step 2: 登录 ---- #
        if not using_prewarmed:
            t2 = time.monotonic()
            token = login(fetcher, email, password)
            t_login = time.monotonic() - t2
            logger.info("[%s]%s 登录成功 (%.2fs)", listing.name,
                        " [DRY RUN]" if dry_run else "", t_login)

        # ---- dry_run ---- #
        if dry_run:
            total = time.monotonic() - t0
            msg = "[DRY RUN] 验证通过（SKU/登录均正常），未实际提交预订"
            logger.info(
                "[%s] %s | 耗时 total=%.1fs (sku=%.2fs login=%.2fs)",
                listing.name, msg, total, t_sku, t_login,
            )
            return BookingResult(listing, True, msg, dry_run=True, phase="dry_run")

        booking_url = f"https://www.holland2stay.com/residences/{listing.id}.html"

        # 占房成立后的凭据。写在闭包外面，_do_book 里逐步填——失败时外层要拿得到。
        held = {"cart_id": "", "order_number": ""}

        def _do_book() -> tuple[str, float, float]:
            ta = time.monotonic()

            # 建车 + 占房合成一步：走站点自己的 /api/booking。
            # 这是唯一的占房入口，没有 fallback——addNewBooking 已被摘出公开
            # API（实测 403），见 _BOOKING_API_PATH 注释。
            new_cart_id = create_booking(fetcher, token, sku, start_date)
            # **从这一行起，这套房已经被这个账号占着了。** 后面任何一步失败都不
            # 改变这个事实，所以先记下来——外层据此告诉用户「房占上了，但流程没
            # 走完」，而不是含糊地报「预订失败」。
            held["cart_id"] = str(new_cart_id or "")
            # 记下「这个 SKU 是我们占的」。cancel_pending_orders 只敢动记过的，
            # 没记录就一笔都不取消——见它的 docstring。
            remember_our_reservation(email, str(sku))
            set_payment_method(fetcher, token, new_cart_id, code=payment_method)
            _fetch_checkout_agreements(fetcher, token)
            t_add_val = time.monotonic() - ta

            tp = time.monotonic()
            order_number = place_order(fetcher, token, new_cart_id)
            held["order_number"] = str(order_number or "")
            pay_url = _ideal_checkout(fetcher, token, order_number)
            t_pay_val = time.monotonic() - tp

            logger.info("[%s] 订单 #%s 支付链接已生成 | add=%.2fs pay=%.2fs",
                        listing.name, order_number, t_add_val, t_pay_val)
            return pay_url, t_add_val, t_pay_val

        # ---- Step 3: 执行预订 ---- #
        try:
            pay_url, t_add, t_pay = _do_book()
            phase = "success"
        except RuntimeError as book_err:
            err_str = str(book_err)

            if _is_booked_by_other(err_str):
                phase = "race_lost"
                logger.warning("[%s] 竞争失败：房源已被他人预订 (%s)",
                               listing.name, err_str)
                raise RuntimeError(
                    f"房源已被他人抢先预订，竞争失败。\n\n"
                    f"💡 如房源重新开放，可尝试手动预订：\n{booking_url}"
                ) from book_err

            elif _is_reserved_by_user(err_str):
                if not cancel_enabled:
                    phase = "reserved_conflict"
                    logger.warning("[%s] 预留单冲突，原始错误: %s",
                                   listing.name, err_str)
                    raise RuntimeError(
                        "该账号尚有未完成的预留订单，请登录 Holland2Stay 手动取消后再试。\n\n"
                        f"📋 原始错误：{err_str}\n\n"
                        f"💡 手动预订入口：\n{booking_url}"
                    ) from book_err

                phase = "cancel+retry"
                logger.info("[%s] 账号已有预留单（%s），正在取消后重试...",
                            listing.name, err_str)
                tc1 = time.monotonic()
                cancelled = cancel_pending_orders(fetcher, token, email)
                t_cancel = time.monotonic() - tc1
                logger.info("[%s] 已取消 %d 笔旧订单 (%.2fs)，重新预订...",
                            listing.name, cancelled, t_cancel)
                pay_url, t_add, t_pay = _do_book()

            else:
                phase = "unknown_error"
                if held["cart_id"]:
                    # 占房成立、后续步骤挂了。**这不是「没抢到」**，用户手上有一
                    # 套占着的房。不给出 cart/order 号的话他既没法去付款，也不知
                    # 道要去取消——那是最坏的一种「失败」。
                    #
                    # 这里刻意**不自动回滚**：取消是不可逆的，而失败可能只是支付
                    # 链接生成超时（房还好好地占着，登录站点就能付）。把事实交给
                    # 用户，由他决定。
                    logger.error(
                        "[%s] 占房已成立但后续步骤失败 cart=%s order=%s: %s",
                        listing.name, held["cart_id"],
                        held["order_number"] or "(未生成)", err_str,
                    )
                    raise BookingHeldButIncomplete(
                        cart_id=held["cart_id"],
                        order_number=held["order_number"],
                        booking_url=booking_url,
                        original=err_str,
                    ) from book_err
                raise

        total = time.monotonic() - t0
        msg = (
            f"✅ 自动预订成功！\n"
            f"\n"
            f"🏠 {listing.name}\n"
            f"📅 入住：{start_date or '待定'}\n"
            f"\n"
            f"⚡ 点击链接立即付款（有时限，请尽快）：\n"
            f"\n"
            f"{pay_url}\n"
            f"\n"
            f"⚠️ 链接直达支付页面，无需登录。"
        )
        parts = (f"sku={t_sku:.2f}s login={t_login:.2f}s "
                 f"add={t_add:.2f}s pay={t_pay:.2f}s")
        if t_cancel:
            parts += f" cancel={t_cancel:.2f}s"
        logger.info(
            "[%s] 预订成功  入住:%s | 耗时 total=%.1fs (%s)",
            listing.name, start_date, total, parts,
        )
        return BookingResult(listing, True, msg, pay_url=pay_url,
                             contract_start_date=start_date or "", phase="success")

    except BookingHeldButIncomplete as held_err:
        total = time.monotonic() - t0
        logger.error(
            "[%s] ⚠️ 占房成立但流程未走完 phase=held_incomplete | listing_id=%s "
            "cart=%s order=%s | 耗时 %.1fs | %s",
            listing.name, listing.id, held_err.cart_id,
            held_err.order_number or "(未生成)", total, held_err.original,
        )
        msg = (
            f"⚠️ 房已占住，但后续步骤没走完——**请尽快自行处理**\n"
            f"\n"
            f"🏠 {listing.name}\n"
            f"🧾 购物车号：{held_err.cart_id}\n"
            + (f"📄 订单号：{held_err.order_number}\n"
               if held_err.order_number else "")
            + f"\n"
            f"这套房现在**占在你的账号下**，不是没抢到。请登录 Holland2Stay：\n"
            f"· 想要 → 完成付款\n"
            f"· 不要 → 手动取消，否则会一直占着\n"
            f"\n"
            f"{held_err.booking_url}\n"
            f"\n"
            f"📋 原始错误：{held_err.original}"
        )
        return BookingResult(
            listing, False, msg, phase="held_incomplete",
            held_cart_id=held_err.cart_id,
            held_order_number=held_err.order_number,
            contract_start_date=start_date or "",
        )

    except TwoFactorRequiredError as tfa_err:
        # 凭据对，但账号开了 2FA，全自动到此为止。和 auth_failed 分开：给用户的
        # 提示不同（去关 2FA 或改半自动），也不该像 blocked 那样熔断登录链路。
        total = time.monotonic() - t0
        logger.warning(
            "[%s]%s 🔐 需要两步验证 phase=auth_2fa | listing_id=%s email=%s | %s",
            listing.name, " [DRY RUN]" if dry_run else "",
            listing.id, _mask_email(email), tfa_err,
        )
        return BookingResult(listing, False, str(tfa_err), phase="auth_2fa")
    except AuthError as auth_err:
        total = time.monotonic() - t0
        logger.error(
            "[%s]%s 🔑 登录被拒 phase=auth_failed | listing_id=%s email=%s | %s",
            listing.name, " [DRY RUN]" if dry_run else "",
            listing.id, _mask_email(email), auth_err,
        )
        return BookingResult(listing, False, str(auth_err), phase="auth_failed")
    except OperationNotAllowedError as op_err:
        # 必须排在 BlockedError 前面（虽然两者没有继承关系，顺序仍是给读者看的：
        # 这两条分支处理的是同一个 HTTP 403 的两种成因）。
        #
        # 与 blocked 的差别全在上层：blocked 会让 monitor 暂停整条登录链路一小时
        # 并失效 prewarm 缓存（假设 session 被标记了）；operation_rejected 不该
        # 触发任何一项——session 是好的，IP 是好的，坏的是我们发的那条查询。
        total = time.monotonic() - t0
        logger.error(
            "[%s]%s ⛔ booking 的 operation 未被上游放行 phase=operation_rejected | "
            "listing_id=%s email=%s prewarmed=%s timings={total:%.2fs} | %s",
            listing.name, " [DRY RUN]" if dry_run else "",
            listing.id, _mask_email(email),
            "yes" if prewarmed else "no", total, op_err,
        )
        return BookingResult(listing, False, str(op_err), phase="operation_rejected")
    except BlockedError as block_err:
        total = time.monotonic() - t0
        logger.error(
            "[%s]%s 🚫 booking 被屏蔽 phase=blocked | listing_id=%s email=%s "
            "prewarmed=%s timings={total:%.2fs} | %s",
            listing.name, " [DRY RUN]" if dry_run else "",
            listing.id, _mask_email(email),
            "yes" if prewarmed else "no", total, block_err,
        )
        return BookingResult(listing, False, str(block_err), phase="blocked")
    except Exception as e:
        total = time.monotonic() - t0
        ctx = (
            f"listing_id={listing.id} sku={listing.sku or 'N/A'} "
            f"email={_mask_email(email)} dry_run={dry_run} prewarmed={'yes' if prewarmed else 'no'} "
            f"timings={{sku:{t_sku:.2f}s login:{t_login:.2f}s cancel:{t_cancel:.2f}s total:{total:.2f}s}}"
        )
        if phase in ("race_lost", "reserved_conflict"):
            logger.warning(
                "[%s]%s 预订失败 phase=%s | %s | %s",
                listing.name, " [DRY RUN]" if dry_run else "",
                phase, ctx, e,
            )
        else:
            logger.error(
                "[%s]%s 预订失败 phase=%s | %s | 原始错误: %s",
                listing.name, " [DRY RUN]" if dry_run else "",
                phase, ctx, e,
                exc_info=True,
            )
        return BookingResult(listing, False, str(e), phase=phase)
    finally:
        if own_fetcher:
            fetcher.__exit__(None, None, None)


def _fetch_sku_and_contract(fetcher: BrowserFetcher, url_key: str) -> tuple[str, Optional[int], Optional[str]]:
    """
    通过 url_key 查询 addNewBooking 所需的三个关键参数（sku / contract_id / 起租日）。

    用站点原文 ``GetProductDetail``（照抄品）：白名单只认这个 operation 名与它
    完整的字段集，旧的自写 ``GetProduct`` 已 403（docs/H2S_BOOKING_OPS.md §2）。
    我们只用到其中三个字段，但**一个都不能删**——删了就是全量 403。
    过滤走 variables（``$filters``），白名单不管。

    这是兜底路径：抓取侧通常已给出 sku/contract_id/起租日，只有 listing.sku 为空
    时才走到这里。它是纯公开查询，不需要登录，无副作用。
    """
    data = _gql(
        fetcher, _GQL_PRODUCT_DETAIL,
        variables={"filters": {"url_key": {"eq": url_key}}},
        operation_name=_OP_PRODUCT_DETAIL,
    )
    items = (data.get("products") or {}).get("items") or []
    if not items:
        raise RuntimeError(f"未找到房源: {url_key}")

    item = items[0]
    sku = item["sku"]

    # contract_id：新 API 返回 int，旧 API 从 selected_options[0].value 解析
    contract_id: Optional[int] = None
    toc = item.get("type_of_contract")
    if toc is not None:
        try:
            contract_id = int(toc)
        except (ValueError, TypeError):
            pass

    # 选择入住日期：优先 next_contract_startdate，其次 available_startdate
    next_start = (item.get("next_contract_startdate") or "").strip()[:10] or None
    avail_date = (item.get("available_startdate") or "").strip()[:10] or None

    # 这里直接读原始字段，连 scraper 那道哨兵过滤都绕过了——所以更需要
    # resolve_start_date 里的那道。
    start_date = resolve_start_date(next_start, avail_date)

    return sku, contract_id, start_date
