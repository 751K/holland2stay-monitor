"""
booker.py — 自动预订模块（CloakBrowser 版）
==============================================

对 "Available to book" 的房源执行完整的自动化预订流程，最终生成可直接支付的链接。

完整流程（try_book 内部）
--------------------------
1. _fetch_sku_and_contract() [fallback，pre-extracted 时跳过]
       通过 url_key 查询 Magento SKU + type_of_contract ID + 下一个入住日期
2. login()
       generateCustomerToken mutation → Bearer token
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

传输层
------
已从 curl_cffi → CloakBrowser（BrowserFetcher）。所有 GraphQL 请求通过
浏览器内 fetch() 发送，自动携带 CF clearance token / cookies / TLS。
Bearer token 通过 extra_headers 传递。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime as _dt
from typing import Literal, Optional

from browser_fetcher import BrowserFetcher
from models import STATUS_AVAILABLE, Listing
from scrapers.base import BlockedError

logger = logging.getLogger(__name__)


class PrewarmedSession:
    """
    预认证的 BrowserFetcher + token，供 try_book() 直接复用。

    Attributes
    ----------
    fetcher    : 已过 CF 挑战的 BrowserFetcher 实例
    token      : generateCustomerToken 返回的 Bearer token
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

class BookingBlockedError(Exception):
    """
    booker 在登录 / 下单流程中遇 H2S API 返回 403。

    BrowserFetcher 内部检测 403 时抛 BlockedError，booker 捕获后
    转为 BookingBlockedError 让上层区分「预订层屏蔽」与其他 BlockedError。
    """


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

    data = fetcher.fetch_gql(query, variables=variables, extra_headers=extra_headers)

    if "errors" in data:
        msgs = "; ".join(e.get("message", "") for e in data["errors"])
        raise RuntimeError(f"GraphQL 错误: {msgs}")
    return data.get("data", {})


# ------------------------------------------------------------------ #
# 登录
# ------------------------------------------------------------------ #

def login(fetcher: BrowserFetcher, email: str, password: str) -> str:
    """调用 generateCustomerToken mutation 登录，返回 Bearer token。"""
    query = '''
    mutation GenerateCustomerToken($email: String!, $password: String!) {
      generateCustomerToken(email: $email, password: $password) {
        token
      }
    }
    '''
    data = _gql(fetcher, query, variables={"email": email, "password": password})
    token = data.get("generateCustomerToken", {}).get("token")
    if not token:
        raise RuntimeError("登录失败：未获取到 token")
    logger.debug("登录成功")
    return token


# ------------------------------------------------------------------ #
# 购物车
# ------------------------------------------------------------------ #

def create_empty_cart(fetcher: BrowserFetcher, token: str) -> str:
    """调用 createEmptyCart mutation 创建全新空购物车，返回 cart_id。"""
    query = "mutation CreateEmptyCart { createEmptyCart }"
    data = _gql(fetcher, query, token=token)
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
    query = '''
    mutation SetPaymentMethodOnCart($cartId: String!, $paymentMethod: PaymentMethodInput!) {
      setPaymentMethodOnCart(
        input: {cart_id: $cartId, payment_method: $paymentMethod}
      ) {
        cart {
          selected_payment_method { code title }
        }
      }
    }
    '''
    data = _gql(fetcher, query, token=token,
                variables={"cartId": cart_id, "paymentMethod": {"code": code}})
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

def cancel_pending_orders(fetcher: BrowserFetcher, token: str) -> int:
    query = '''
    {
      customer {
        orders(pageSize: 10, currentPage: 1) {
          items {
            id
            number
            status
          }
        }
      }
    }
    '''
    try:
        data = _gql(fetcher, query, token=token)
    except Exception as e:
        logger.warning("查询订单列表失败（忽略）: %s", e)
        return 0

    items = (data.get("customer") or {}).get("orders", {}).get("items") or []
    CANCEL_STATUSES = {"pending", "pending_payment", "reserved", "processing"}
    to_cancel = [
        (o["id"], o["number"])
        for o in items
        if o.get("status", "").lower() in CANCEL_STATUSES
    ]

    if not to_cancel:
        logger.debug("无 pending 订单，无需取消")
        return 0

    logger.info("发现 %d 笔 pending 订单，准备取消: %s", len(to_cancel), [n for _, n in to_cancel])

    cancelled = 0
    cancel_disabled = False
    for order_uid, order_number in to_cancel:
        try:
            q = '''
            mutation CancelOrder($orderId: String!) {
              cancelOrder(input: { order_id: $orderId }) {
                order { id status }
              }
            }
            '''
            _gql(fetcher, q, token=token, variables={"orderId": order_uid})
            logger.info("已取消订单 #%s", order_number)
            cancelled += 1
        except Exception as e:
            err_str = str(e)
            if "not enabled" in err_str.lower():
                cancel_disabled = True
                logger.warning("cancelOrder 未启用，无法取消订单 #%s: %s", order_number, err_str)
            else:
                logger.warning("取消订单 #%s 失败: %s", order_number, e)

    if cancel_disabled and cancelled == 0:
        raise RuntimeError(
            "当前账号有旧预留单且平台未启用订单取消功能，无法自动取消。\n"
            "请登录 Holland2Stay 手动取消旧订单后再试。"
        )

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
    调用 H2S 专用 addNewBooking mutation，将押金项加入购物车并创建预订。

    与浏览器行为对齐：仅传 cart_id + sku + contract_startDate。
    NON_NULL 传播绕过：不请求 cart{} 字段，只查 user_errors。
    """
    query = '''
    mutation AddNewBooking(
      $cart_id: String!,
      $sku: String!,
      $contract_startDate: String
    ) {
      addNewBooking(
        cart_id: $cart_id
        sku: $sku
        contract_startDate: $contract_startDate
      ) {
        user_errors { code message }
      }
    }
    '''

    variables: dict = {"cart_id": cart_id, "sku": sku}
    if contract_start_date:
        variables["contract_startDate"] = _to_h2s_date(contract_start_date)

    raw = fetcher.fetch_gql(
        query, variables=variables,
        extra_headers={"Authorization": f"Bearer {token}"},
    )

    logger.debug("addNewBooking raw response: %s", raw)

    if "errors" in raw:
        msgs = "; ".join(e.get("message", "") for e in raw["errors"])
        if not raw.get("data"):
            logger.error(
                "addNewBooking GraphQL 层致命错误 sku=%s start=%s: %s",
                sku, contract_start_date, msgs,
            )
            raise RuntimeError(f"addNewBooking GraphQL 错误: {msgs}")
        logger.warning("addNewBooking 非致命 GraphQL 错误（NON_NULL 传播，已忽略）: %s", msgs)

    result = (raw.get("data") or {}).get("addNewBooking") or {}
    user_errors = result.get("user_errors") or []
    if user_errors:
        msgs = "; ".join(
            f"[{e.get('code','?')}] {e.get('message','')}" for e in user_errors
        )
        logger.error(
            "addNewBooking 业务错误 sku=%s start=%s: %s",
            sku, contract_start_date, msgs,
        )
        raise RuntimeError(f"addNewBooking 失败: {msgs}")

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
    query = '''
    query GetCheckoutAgreements {
      checkoutAgreements {
        name
        content
        checkbox_text
        mode
        __typename
      }
    }
    '''
    try:
        data = _gql(fetcher, query, token=token)
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
    """调用 placeOrder mutation 将购物车转为正式订单，返回订单号。"""
    query = '''
    mutation PlaceOrder($cartId: String!, $storeId: Int) {
      placeOrder(input: {cart_id: $cartId, store_id: $storeId}) {
        orderV2 {
          order_number
        }
        errors {
          message
          code
        }
      }
    }
    '''
    data = _gql(fetcher, query, token=token,
                variables={"cartId": cart_id, "storeId": store_id})
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
    """调用 idealCheckOut mutation 生成 iDEAL 直链付款 URL。"""
    query = '''
    mutation IdealCheckOut($order_id: String!, $plateform: String) {
      idealCheckOut(order_id: $order_id, plateform: $plateform) {
        redirect
      }
    }
    '''
    tp0 = time.monotonic()
    try:
        data = _gql(fetcher, query, token=token,
                    variables={"order_id": order_number, "plateform": "h"})
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
    "unsupported",
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
    通过 url_key 查询 addNewBooking 所需的三个关键参数。

    新 API 使用扁平字段（不再有 custom_attributesV2）。
    """
    query = '''
    query GetProduct($urlKey: String!) {
      products(filter: {
        category_uid: { eq: "Nw==" }
        url_key: { eq: $urlKey }
      }) {
        items {
          sku
          type_of_contract
          next_contract_startdate
          available_startdate
        }
      }
    }
    '''
    data = _gql(fetcher, query, variables={"urlKey": url_key})
    items = data.get("products", {}).get("items") or []
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
