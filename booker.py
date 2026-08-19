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
   3a. create_empty_cart()
           createEmptyCart mutation → 全新空购物车 cart_id
   3b. add_to_cart()
           addNewBooking mutation → 将押金项加入购物车并创建预订
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

⚠️ 未经真实下单验证的部分：登录（NextAuth）与 ``GetProductDetail`` 已实测走通，
但 createEmptyCart → … → placeOrder 只静态照抄了站点原文、核对了端点与鉴权，
没有真的下过单（下单产生真实订单）。首次真实预订前应先用真实账号跑一次
「加购但不 placeOrder」收尾验证。见 docs/H2S_BOOKING_OPS.md §6.5。
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
    ADDNEWBOOKING as _GQL_ADD_BOOKING,
    CREATEEMPTYCART as _GQL_CREATE_CART,
    GETCHECKOUTAGREEMENTS as _GQL_AGREEMENTS,
    GETPRODUCTDETAIL as _GQL_PRODUCT_DETAIL,
    IDEALCHECKOUT as _GQL_IDEAL,
    PLACEORDER as _GQL_PLACE_ORDER,
    SETPAYMENTMETHODONCART as _GQL_SET_PAYMENT,
    OP_ADDNEWBOOKING as _OP_ADD_BOOKING,
    OP_CREATEEMPTYCART as _OP_CREATE_CART,
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


class AuthError(Exception):
    """登录被平台拒绝（凭据错误）。换 IP / 重试都无意义——要用户改账号密码。"""


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
    此函数不处理 partial error。add_to_cart() 因 NON_NULL 传播问题
    不使用此函数而是直接调用 fetcher.fetch_gql()。
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
# 购物车
# ------------------------------------------------------------------ #

def create_empty_cart(fetcher: BrowserFetcher, token: str) -> str:
    """调用 createEmptyCart mutation 创建全新空购物车，返回 cart_id。"""
    data = _gql(fetcher, _GQL_CREATE_CART, token=token,
                operation_name=_OP_CREATE_CART)
    cart_id = data.get("createEmptyCart")
    if not cart_id:
        raise RuntimeError("createEmptyCart 未返回购物车 ID")
    logger.debug("新购物车 ID: %s", cart_id)
    return cart_id


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


def cancel_pending_orders(fetcher: BrowserFetcher, token: str) -> int:
    """取消账号下所有待处理的预留，返回取消成功的笔数。

    ⚠️ 机制已照抄、**未经真实取消验证**：一是这条路只在 reserved_conflict +
    cancel_enabled 时触发（边缘场景），二是租户门户把 REST 路径也塞进加密信封，
    而 www 上这些端点走明文还是走信封静态读不出来——这里先按明文（``fetch_plain``）
    实现，若线上回 400/403 再改走信封。整体 try/except 兜底：取消失败只是救不回
    旧预留，不连累主流程。用真实账号验证时，比照 §6.6 核对传输层。
    """
    import json as _j

    hdr = {"Authorization": f"Bearer {token}"}
    try:
        r = fetcher.fetch_plain(_REST_LIST_RESERVATIONS, method="GET", headers=hdr)
        items = (_j.loads(r["text"]) or {}).get("items") or []
    except Exception as e:
        logger.warning("查询预留列表失败（忽略，机制见 §6.6）: %s", e)
        return 0

    to_cancel = [
        (it.get("sku"), it.get("product_name") or it.get("sku"))
        for it in items
        if it.get("sku") and str(it.get("status", "")).lower() in _CANCEL_STATUSES
    ]
    if not to_cancel:
        logger.debug("无待处理预留，无需取消")
        return 0

    logger.info("发现 %d 笔待处理预留，准备取消: %s",
                len(to_cancel), [n for _, n in to_cancel])

    cancelled = 0
    for sku, name in to_cancel:
        try:
            resp = fetcher.fetch_plain(
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

def add_to_cart(
    fetcher: BrowserFetcher,
    token: str,
    cart_id: str,
    sku: str,
    contract_start_date: Optional[str],
) -> bool:
    """
    发站点原文的 ``AddNewBooking`` mutation，把押金项加入购物车并创建预订。

    ★ 有副作用：这一步就占住房了。

    用的是照抄品（``h2s_booking_gql.ADDNEWBOOKING``），选择集是站点的
    ``cart { items {...} }``——不是我们以前自写的 ``user_errors``。白名单按
    operationName + 归一化字段集放行，选择集写错就是 403。声明了但没传的变量
    （contract_id / option_selected）按 GraphQL 规则默认 null，合法。
    """
    variables: dict = {"cart_id": cart_id, "sku": sku}
    if contract_start_date:
        variables["contract_startDate"] = _to_h2s_date(contract_start_date)

    raw = fetcher.fetch_gql(
        _GQL_ADD_BOOKING, variables=variables,
        operation_name=_OP_ADD_BOOKING,
        extra_headers={"Authorization": f"Bearer {token}"},
    )

    logger.debug("addNewBooking raw response: %s", raw)

    # GraphQL 层错误：有 errors 且没有可用 data 才算致命（NON_NULL 传播会同时
    # 带 errors + 部分 data）。
    if "errors" in raw:
        msgs = "; ".join(e.get("message", "") for e in raw["errors"])
        cart = ((raw.get("data") or {}).get("addNewBooking") or {}).get("cart")
        if not cart:
            logger.error(
                "addNewBooking GraphQL 层错误 sku=%s start=%s: %s",
                sku, contract_start_date, msgs,
            )
            raise RuntimeError(f"addNewBooking 失败: {msgs}")
        logger.warning("addNewBooking 带非致命 GraphQL 错误（已入车，忽略）: %s", msgs)

    cart = ((raw.get("data") or {}).get("addNewBooking") or {}).get("cart")
    if not cart or not (cart.get("items") or []):
        raise RuntimeError(
            f"addNewBooking 未把房源加入购物车（sku={sku}）；"
            f"响应: {str(raw)[:300]}"
        )

    logger.info("addNewBooking 成功（押金项已入购物车）")
    return True


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
        from datetime import date as _date
        candidate = listing.contract_start_date or listing.available_from
        start_date = candidate if (candidate and candidate >= _date.today().isoformat()) else None
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

        def _do_book() -> tuple[str, float, float]:
            ta = time.monotonic()

            new_cart_id = create_empty_cart(fetcher, token)
            add_to_cart(fetcher, token, new_cart_id, sku, start_date)
            set_payment_method(fetcher, token, new_cart_id, code=payment_method)
            _fetch_checkout_agreements(fetcher, token)
            t_add_val = time.monotonic() - ta

            tp = time.monotonic()
            order_number = place_order(fetcher, token, new_cart_id)
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
                cancelled = cancel_pending_orders(fetcher, token)
                t_cancel = time.monotonic() - tc1
                logger.info("[%s] 已取消 %d 笔旧订单 (%.2fs)，重新预订...",
                            listing.name, cancelled, t_cancel)
                pay_url, t_add, t_pay = _do_book()

            else:
                phase = "unknown_error"
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

    from datetime import date
    today_str = date.today().isoformat()
    candidate = next_start or avail_date
    start_date = candidate if (candidate and candidate >= today_str) else None

    return sku, contract_id, start_date
