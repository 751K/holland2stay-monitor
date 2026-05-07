"""
booker.py — 自动预订模块
==========================
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
           注意：只请求 user_errors，不请求 cart{}（NON_NULL 传播 bug）
   3c. set_payment_method()
           setPaymentMethodOnCart mutation → code="idealcheckout_ideal"
           placeOrder 前必须调用，否则报 "payment method not available"
   3d. place_order()
           placeOrder mutation（含 store_id）→ orderV2.order_number
           ┣ 若返回「账号已有预留单」→ 按 cancel_enabled 处理
           ┗ 若返回「房源已被他人预订」→ 竞争失败，通知用户
   3d. _ideal_checkout()
           idealCheckOut mutation → redirect（直链付款 URL）
           支付域名在 account.holland2stay.com，链接无需登录可直接付款

cancel_pending_orders() 不在 add_to_cart 之前预先调用，仅在 placeOrder 返回
"another unit reserved" 且 cancel_enabled=True 时才触发，作为一次性重试。

GraphQL API
-----------
端点：https://api.holland2stay.com/graphql/（Magento 后端）
认证：generateCustomerToken 换取 Bearer token，后续请求附加 Authorization 头

对外接口
--------
- create_prewarmed_session(email, password) → PrewarmedSession
  提前创建已认证 Session，供 try_book() 复用，省去登录往返
- try_book(listing, email, password, *, dry_run, prewarmed) → BookingResult
  prewarmed 参数可选，传入时跳过 Session 创建 + 登录

依赖
----
curl_cffi.requests（绕过 Cloudflare），models.Listing
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime as _dt
from typing import Literal

import curl_cffi.requests as req

from config import CURL_IMPERSONATE
from models import STATUS_AVAILABLE, Listing

logger = logging.getLogger(__name__)


class PrewarmedSession:
    """
    预认证的 curl_cffi Session，供 try_book() 直接复用，省去重复创建 Session
    和登录（generateCustomerToken）的网络往返。

    Attributes
    ----------
    session    : 已建立 TLS 连接的 curl_cffi Session（含 Authorization cookie）
    token      : generateCustomerToken 返回的 Bearer token
    created_at : time.monotonic() 创建时刻，供调用方判断是否过期
    email      : 对应的 H2S 账号邮箱，用于校验是否匹配
    """

    __slots__ = ("session", "token", "created_at", "token_expiry", "email")

    def __init__(self, session, token: str, created_at: float, token_expiry: float, email: str):
        self.session = session
        self.token = token
        self.created_at = created_at
        self.token_expiry = token_expiry
        self.email = email


GQL_URL = "https://api.holland2stay.com/graphql/"

_BASE_HEADERS = {
    "Accept":       "application/graphql-response+json,application/json;q=0.9",
    "Content-Type": "application/json",
    "Origin":       "https://www.holland2stay.com",
    "Referer":      "https://www.holland2stay.com/",
}

# Magento store_id，从浏览器抓包确认（placeOrder 的 store_id 参数）。
# 多城市验证均为 54，视为全局常量。
_H2S_STORE_ID = 54

# setPaymentMethodOnCart 使用的支付方式代码。
# 浏览器支持三种：idealcheckout_ideal / idealcheckout_visa / idealcheckout_mastercard。
# 自动预订固定选 iDEAL：无需持卡信息，与 idealCheckOut mutation 对应。
_PAYMENT_METHOD = "idealcheckout_ideal"

# Magento token 有效期约 1 小时，设 55 分钟上限保留缓冲，
# 超时预登录 session 退回正常登录路径避免 auth 错误。
_TOKEN_MAX_AGE = 3300  # 55 分钟（秒）


# ------------------------------------------------------------------ #
# 日期格式转换
# ------------------------------------------------------------------ #

def _to_h2s_date(iso_date: str) -> str:
    """
    将 ISO 日期（YYYY-MM-DD）转换为 H2S API 要求的格式（DD-MM-YYYY）。

    H2S 的 addNewBooking mutation 要求 contract_startDate 为 DD-MM-YYYY：
      "2026-05-04" → "04-05-2026"

    传入错误格式（如 YYYY-MM-DD）时 API 会返回服务端错误，因此此转换是必须的。

    Raises
    ------
    ValueError  iso_date 为空或不符合 YYYY-MM-DD 格式时
    """
    if not iso_date:
        raise ValueError("iso_date 不能为空")
    try:
        return _dt.strptime(iso_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        raise ValueError(f"日期格式错误，期望 YYYY-MM-DD，实际为: {iso_date!r}") from None


# ------------------------------------------------------------------ #
# 错误分类（placeOrder 业务错误识别）
# ------------------------------------------------------------------ #

def _is_booked_by_other(msg: str) -> bool:
    """
    检查是否是「本房源已被他人抢先预订」错误（竞争失败，无法恢复）。

    对应 H2S 返回：
      "Sorry, the residence you have selected is already booked by someone else."
    """
    return "already booked by someone else" in msg.lower()


def _is_reserved_by_user(msg: str) -> bool:
    """
    检查是否是「该账号已有其他预留单」错误（可通过取消旧单后重试恢复）。

    对应 H2S 返回：
      "Sorry, at the moment you have another unit reserved."
    """
    low = msg.lower()
    return (
        "another unit reserved" in low
        or "you have another" in low
        or "at the moment you have" in low
    )


# ------------------------------------------------------------------ #
# GraphQL helpers
# ------------------------------------------------------------------ #

def _gql(
    session: req.Session,
    query: str,
    token: Optional[str] = None,
    variables: Optional[dict] = None,
) -> dict:
    """
    执行 GraphQL 查询/变更并返回 data 字段。

    Parameters
    ----------
    session   : curl_cffi Session（由调用方管理生命周期）
    query     : GraphQL 查询或 mutation 字符串
    token     : Bearer token，传入时附加 Authorization 头
    variables : GraphQL variables dict，由 json.dumps 序列化后传输；
                含用户输入时必须使用此参数，不得将用户数据直接拼入 query 字符串

    Returns
    -------
    响应 JSON 的 data 字段（dict）

    Raises
    ------
    requests.HTTPError    HTTP 4xx/5xx 时
    RuntimeError          响应含 errors 字段时（GraphQL 层错误）

    注意
    ----
    此函数不处理 partial error（同时含 errors 和 data 的情况）。
    add_to_cart() 因为 NON_NULL 传播问题，不使用此函数而是直接调用 session.post。
    """
    headers = dict(_BASE_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = session.post(GQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        msgs = "; ".join(e.get("message", "") for e in data["errors"])
        raise RuntimeError(f"GraphQL 错误: {msgs}")
    return data.get("data", {})


# ------------------------------------------------------------------ #
# 登录
# ------------------------------------------------------------------ #

def login(session: req.Session, email: str, password: str) -> str:
    """
    调用 generateCustomerToken mutation 登录，返回 Bearer token。

    邮箱和密码通过 GraphQL variables 传递（而非拼入 query 字符串），
    由 json.dumps 负责转义，含 "、\\、控制字符的密码均可正确处理。
    """
    query = '''
    mutation GenerateCustomerToken($email: String!, $password: String!) {
      generateCustomerToken(email: $email, password: $password) {
        token
      }
    }
    '''
    data = _gql(session, query, variables={"email": email, "password": password})
    token = data.get("generateCustomerToken", {}).get("token")
    if not token:
        raise RuntimeError("登录失败：未获取到 token")
    logger.debug("登录成功")
    return token


# ------------------------------------------------------------------ #
# 购物车
# ------------------------------------------------------------------ #

def create_empty_cart(session: req.Session, token: str) -> str:
    """
    调用 createEmptyCart mutation 创建全新空购物车，返回 cart_id。

    每次预订前创建新购物车，避免旧条目干扰（与浏览器行为一致）。
    重试 _do_book() 时也会调用本函数，保证购物车干净。
    """
    query = "mutation CreateEmptyCart { createEmptyCart }"
    data = _gql(session, query, token=token)
    cart_id = data.get("createEmptyCart")
    if not cart_id:
        raise RuntimeError("createEmptyCart 未返回购物车 ID")
    logger.debug("新购物车 ID: %s", cart_id)
    return cart_id


# ------------------------------------------------------------------ #
# 设置支付方式
# ------------------------------------------------------------------ #

def set_payment_method(
    session: req.Session,
    token: str,
    cart_id: str,
    code: str = _PAYMENT_METHOD,
) -> None:
    """
    调用 setPaymentMethodOnCart mutation，在购物车上指定支付方式。

    必须在 placeOrder 之前调用，否则 placeOrder 返回：
    "The payment method you requested is not available."

    浏览器抓包确认的三种支付代码：
      idealcheckout_ideal      → iDEAL（默认，自动预订使用）
      idealcheckout_visa       → Visa
      idealcheckout_mastercard → Mastercard
    """
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
    data = _gql(session, query, token=token,
                variables={"cartId": cart_id, "paymentMethod": {"code": code}})
    selected = (
        (data.get("setPaymentMethodOnCart") or {})
        .get("cart", {})
        .get("selected_payment_method", {})
        .get("code")
    )
    logger.info("支付方式已设置: %s", selected or code)


# ------------------------------------------------------------------ #
# 取消 pending 订单（清除 "already reserved" 锁）
# ------------------------------------------------------------------ #

def cancel_pending_orders(session: req.Session, token: str) -> int:
    """
    查询账号近 10 笔订单，通过标准 Magento cancelOrder mutation 取消所有
    pending/reserved 状态的订单。

    背景
    ----
    placeOrder 会检查账号下是否已有预留单，若有则返回：
    "Sorry, at the moment you have another unit reserved."
    在新预订前必须取消旧的 pending 订单才能成功下单。

    实现策略
    --------
    使用 Magento 标准 cancelOrder mutation。
    若平台未启用该 mutation（"not enabled for requested store"），
    则抛出 RuntimeError 明确告知调用方：此账号无法自动取消旧订单。

    Raises
    ------
    RuntimeError  平台未启用 cancelOrder 时（不可恢复，需人工处理）

    Returns
    -------
    成功取消的订单数（0 表示无待取消订单）
    """
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
        data = _gql(session, query, token=token)
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
            _gql(session, q, token=token, variables={"orderId": order_uid})
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
    session: req.Session,
    token: str,
    cart_id: str,
    sku: str,
    contract_start_date: Optional[str],
    contract_id: Optional[int] = None,
) -> bool:
    """
    调用 H2S 专用 addNewBooking mutation，将押金项加入购物车并创建预订。

    addNewBooking 是 H2S 的定制接口，负责：
    1. 将押金项（Deposit €200）加入购物车
    2. 创建预订（绑定 contract_id / contract_startDate）

    日期格式
    --------
    H2S API 要求 contract_startDate 为 DD-MM-YYYY（如 "04-05-2026"），
    不是 ISO 格式。此函数内部调用 _to_h2s_date() 完成转换。

    NON_NULL 传播绕过
    ----------------
    不请求 cart{} 字段：若 cart=null，GraphQL 会将 null 上升为顶层
    "Internal server error"，掩盖 user_errors。只查 user_errors 可避免此问题。

    mutation 签名与变量名严格对齐官方前端（从浏览器 DevTools 抓包核实）：
      $cart_id / $sku / $contract_startDate / $contract_id / $option_selected
    """
    query = '''
    mutation AddNewBooking(
      $cart_id: String!,
      $sku: String!,
      $contract_startDate: String,
      $contract_id: Int,
      $option_selected: String
    ) {
      addNewBooking(
        cart_id: $cart_id
        sku: $sku
        contract_startDate: $contract_startDate
        contract_id: $contract_id
        option_selected: $option_selected
      ) {
        user_errors { code message }
      }
    }
    '''

    variables: dict = {"cart_id": cart_id, "sku": sku}
    if contract_start_date:
        # API 要求 DD-MM-YYYY，_to_h2s_date 负责从 ISO 格式转换
        variables["contract_startDate"] = _to_h2s_date(contract_start_date)
    if contract_id is not None:
        variables["contract_id"] = contract_id

    resp = session.post(
        GQL_URL,
        json={"query": query, "variables": variables},
        headers={**_BASE_HEADERS, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()

    logger.debug("addNewBooking raw response: %s", raw)

    if "errors" in raw:
        msgs = "; ".join(e.get("message", "") for e in raw["errors"])
        if not raw.get("data"):
            logger.error("addNewBooking GraphQL 层致命错误（无 data）: %s", msgs)
            raise RuntimeError(f"addNewBooking GraphQL 错误: {msgs}")
        # NON_NULL 传播 bug：cart=null 时 Magento 将 null 上升为顶层 errors，
        # 但 data 仍存在且 user_errors 为空。代码已正确处理，无需报错。
        logger.warning("addNewBooking 非致命 GraphQL 错误（NON_NULL 传播，已忽略）: %s", msgs)

    result = (raw.get("data") or {}).get("addNewBooking") or {}
    user_errors = result.get("user_errors") or []
    if user_errors:
        msgs = "; ".join(
            f"[{e.get('code','?')}] {e.get('message','')}" for e in user_errors
        )
        logger.error("addNewBooking 业务错误: %s", msgs)
        raise RuntimeError(f"addNewBooking 失败: {msgs}")

    logger.info("addNewBooking 成功（押金项已入购物车）")
    return True


# ------------------------------------------------------------------ #
# 下单
# ------------------------------------------------------------------ #

def place_order(
    session: req.Session,
    token: str,
    cart_id: str,
    store_id: int = _H2S_STORE_ID,
) -> str:
    """
    调用 placeOrder mutation 将购物车转为正式订单，返回订单号。

    mutation 签名、响应字段与错误处理均从浏览器 DevTools 抓包核实：
    - 参数：cartId + storeId（H2S 后端要求，默认 54）
    - 成功响应：orderV2.order_number（不是 order.number）
    - 业务错误：response.errors 数组（区别于 GraphQL 层 errors）

    Raises
    ------
    RuntimeError  placeOrder 返回业务错误或未返回订单号时

    Notes
    -----
    "already booked by someone else" → _is_booked_by_other() → 竞争失败
    "another unit reserved"          → _is_reserved_by_user() → 可取消重试
    两类错误均由调用方（try_book）的 except 块识别并分别处理。
    """
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
    data = _gql(session, query, token=token,
                variables={"cartId": cart_id, "storeId": store_id})
    result = data.get("placeOrder") or {}

    # placeOrder 的业务错误通过 errors 数组返回（区别于 GraphQL 层 errors）
    # 竞争失败（already booked by someone else）和预留单冲突（another unit
    # reserved）均为预期的业务结果，不是代码缺陷，用 WARNING 级别记录；
    # 调用方 try_book() 会按阶段分类并决定重试策略
    errors = result.get("errors") or []
    if errors:
        msgs = "; ".join(
            f"[{e.get('code','?')}] {e.get('message','')}" for e in errors
        )
        logger.warning("placeOrder 业务错误: %s", msgs)
        raise RuntimeError(f"下单失败: {msgs}")

    order_number = (result.get("orderV2") or {}).get("order_number")
    if not order_number:
        raise RuntimeError("placeOrder 未返回订单号（orderV2.order_number 为空）")

    logger.info("订单已创建: #%s", order_number)
    return order_number


# ------------------------------------------------------------------ #
# 生成支付链接
# ------------------------------------------------------------------ #

def _ideal_checkout(session: req.Session, token: str, order_number: str) -> str:
    """
    调用 idealCheckOut mutation 生成 iDEAL 直链付款 URL。

    参数与变量名从浏览器 DevTools 抓包核实：
    - 变量名：order_id（snake_case），plateform（注意拼写，官方如此）
    - plateform 值："h"（浏览器实际传值，非 "web"）

    付款域名是 account.holland2stay.com（PHP 后端），
    不是 www.holland2stay.com（Next.js 前端）。
    """
    query = '''
    mutation IdealCheckOut($order_id: String!, $plateform: String) {
      idealCheckOut(order_id: $order_id, plateform: $plateform) {
        redirect
      }
    }
    '''
    tp0 = time.monotonic()
    try:
        data = _gql(session, query, token=token,
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
]


@dataclass
class BookingResult:
    """
    try_book() 的返回值，封装预订结果。

    Fields
    ------
    listing               : 被尝试预订的房源
    success               : True 表示流程全部成功（或 dry_run 验证通过）
    message               : 发送给用户的通知消息（含付款链接或失败原因）
    dry_run               : True 表示是 dry_run 模式产生的结果（未实际提交）
    pay_url               : _ideal_checkout() 返回的直链付款 URL；
                            失败时为空字符串；dry_run 时也为空字符串
    contract_start_date   : _fetch_sku_and_contract() 从 API 获取的实际合同开始日期，
                            格式 "YYYY-MM-DD"；未知时为空字符串。
    phase                 : 内部流程阶段标识，供调用方判断失败类型：
                            "success"           → 预订成功
                            "dry_run"           → dry_run 模式（未实际提交）
                            "race_lost"         → 竞争失败（房源已被他人预订）
                            "reserved_conflict" → 账号已有预留单且 cancel_enabled=False
                            "cancel+retry"      → 取消旧单后重试（cancel_enabled=True）
                            "unknown_error"     → 其他未预期错误
                            ""                  → 未进入预订阶段（如状态不符合）
    """
    listing: Listing
    success: bool
    message: str
    dry_run: bool = False
    pay_url: str = ""
    contract_start_date: str = ""
    phase: BookingPhase = ""


def create_prewarmed_session(email: str, password: str) -> PrewarmedSession:
    """
    创建已登录的 Session，供 try_book() 直接复用。

    调用方负责在使用完毕后调用 ps.session.close() 释放连接。
    登录失败时抛出异常（与原 login() 行为一致），调用方应捕获并回退到
    try_book() 的正常登录路径。

    Parameters
    ----------
    email    : Holland2Stay 账号邮箱
    password : Holland2Stay 账号密码

    Returns
    -------
    PrewarmedSession，含已认证的 session 和 token

    Raises
    ------
    RuntimeError  登录失败（token 为空）
    """
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    proxies = {"https": proxy, "http": proxy} if proxy else {}
    session = req.Session(impersonate=CURL_IMPERSONATE, proxies=proxies)
    try:
        token = login(session, email, password)
    except Exception:
        session.close()
        raise
    now = time.monotonic()
    return PrewarmedSession(
        session=session,
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

    Parameters
    ----------
    listing        : 目标房源（status 必须为 "Available to book"，否则立即返回失败）
    email          : Holland2Stay 账号邮箱
    password       : Holland2Stay 账号密码
    dry_run        : True 时只完成 SKU 查询/登录验证，不提交预订
    cancel_enabled : True 时若 placeOrder 返回 "another unit reserved"，
                     则自动取消旧订单后重试；False（默认）时直接通知用户
    payment_method : setPaymentMethodOnCart 使用的支付代码，
                     默认 "idealcheckout_ideal"（iDEAL），
                     可选 "idealcheckout_visa" / "idealcheckout_mastercard"
    prewarmed      : 预认证 Session。提供时跳过 Session 创建和登录步骤，
                     直接使用已有的 session + token。
                     注意：传入的 session 不会被 try_book 关闭；
                     关闭由调用方负责。

    Returns
    -------
    BookingResult：
    - success=True, pay_url 非空  → 预订成功，message 含直链付款 URL
    - success=True, dry_run=True  → dry_run 验证通过
    - success=False               → 任何步骤失败，message 含错误原因

    内部流程（每次 _do_book 调用）
    --------------------------------
    createEmptyCart → addNewBooking → setPaymentMethodOnCart → placeOrder → idealCheckOut

    重试策略
    --------
    placeOrder 返回「房源已被他人预订」→ 立即通知用户（竞争失败，不重试）。
    placeOrder 返回「账号已有预留单」且 cancel_enabled=True
      → cancel_pending_orders() 取消旧单 → 重新执行 _do_book()（含新购物车）。
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

    # 决定 Session 来源：预登录复用 or 按需创建
    own_session = False
    if prewarmed is not None and time.monotonic() < prewarmed.token_expiry:
        session = prewarmed.session
        token = prewarmed.token
        logger.debug("复用预登录 session (email=%s)", email)
    else:
        if prewarmed is not None:
            age = time.monotonic() - prewarmed.created_at
            logger.warning(
                "预登录 session 已过期 (%.0f 秒前创建，上限 %d 秒)，退回正常登录",
                age, _TOKEN_MAX_AGE,
            )
            prewarmed.session.close()
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        proxies = {"https": proxy, "http": proxy} if proxy else {}
        session = req.Session(impersonate=CURL_IMPERSONATE, proxies=proxies)
        own_session = True

    try:
        # ---- Step 1 fallback: 没有预提取 SKU 时通过 API 查询 ---- #
        if not listing.sku:
            t1 = time.monotonic()
            sku, contract_id, start_date = _fetch_sku_and_contract(session, listing.id)
            t_sku = time.monotonic() - t1
            logger.info(
                "[%s]%s SKU: %s  contract_id: %s  start_date: %s  (%.2fs) [fallback]",
                listing.name, " [DRY RUN]" if dry_run else "",
                sku, contract_id, start_date or "(不传，由服务端决定)", t_sku,
            )

        # ---- Step 2: 登录（预登录 session 已跳过） ---- #
        if prewarmed is None:
            t2 = time.monotonic()
            token = login(session, email, password)
            t_login = time.monotonic() - t2
            logger.info("[%s]%s 登录成功 (%.2fs)", listing.name,
                        " [DRY RUN]" if dry_run else "", t_login)

        # ---- dry_run：验证凭据即止，不提交任何预订 ---- #
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
            """
            createEmptyCart → addNewBooking → setPaymentMethodOnCart
            → placeOrder → idealCheckOut。
            每次调用都创建新购物车，保证重试路径也能干净执行。
            """
            ta = time.monotonic()

            # 3a. 新建购物车
            new_cart_id = create_empty_cart(session, token)

            # 3b. 加入购物车（addNewBooking）
            add_to_cart(session, token, new_cart_id, sku, start_date, contract_id)

            # 3c. 设置支付方式（placeOrder 前必须调用，否则报 "payment not available"）
            set_payment_method(session, token, new_cart_id, code=payment_method)
            t_add_val = time.monotonic() - ta

            # 3d. 下单 → 获取订单号
            tp = time.monotonic()
            order_number = place_order(session, token, new_cart_id)

            # 3e. 生成支付链接
            pay_url = _ideal_checkout(session, token, order_number)
            t_pay_val = time.monotonic() - tp

            logger.info("[%s] 订单 #%s 支付链接已生成 | add=%.2fs pay=%.2fs",
                        listing.name, order_number, t_add_val, t_pay_val)
            return pay_url, t_add_val, t_pay_val

        # ---- Step 3: 执行预订（含错误分类重试） ---- #
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

                # cancel_enabled=True：取消旧单后重试整个 _do_book（含新购物车）
                phase = "cancel+retry"
                logger.info("[%s] 账号已有预留单（%s），正在取消后重试...",
                            listing.name, err_str)
                tc1 = time.monotonic()
                cancelled = cancel_pending_orders(session, token)
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

    except Exception as e:
        total = time.monotonic() - t0
        # race_lost / reserved_conflict 是预期业务结果，不打 traceback；
        # unknown_error 等才是需要排查的异常
        if phase in ("race_lost", "reserved_conflict"):
            logger.warning(
                "[%s]%s 预订失败 (%s) | 耗时 total=%.1fs | %s",
                listing.name, " [DRY RUN]" if dry_run else "",
                phase, total, e,
            )
        else:
            logger.error(
                "[%s]%s 预订失败 (%s) | 耗时 total=%.1fs | 原始错误: %s",
                listing.name, " [DRY RUN]" if dry_run else "",
                phase, total, e,
                exc_info=True,
            )
        return BookingResult(listing, False, str(e), phase=phase)
    finally:
        if own_session:
            session.close()


def _fetch_sku_and_contract(session: req.Session, url_key: str) -> tuple[str, Optional[int], Optional[str]]:
    """
    通过 url_key 查询 addNewBooking 所需的三个关键参数。

    Parameters
    ----------
    session  : curl_cffi Session（无需鉴权，公开接口）
    url_key  : 房源 URL slug，即 Listing.id，e.g. "kastanjelaan-1-108"

    Returns
    -------
    (sku, contract_id, start_date)

    sku           : Magento 内部 SKU，addNewBooking 的主要参数
    contract_id   : type_of_contract 属性的 value（int）；
                    不传会导致 addNewBooking 返回 Internal server error
    start_date    : 下一个可用入住日期，格式 "YYYY-MM-DD"；
                    优先取 next_contract_startdate，其次取 available_startdate；
                    若日期早于今日则置为 None（传过期日期服务端会报错）
                    注意：add_to_cart() 内部会调用 _to_h2s_date() 转为 DD-MM-YYYY

    Raises
    ------
    RuntimeError 未找到该 url_key 对应的房源时
    """
    query = '''
    query GetProduct($urlKey: String!) {
      products(filter: {
        category_uid: { eq: "Nw==" }
        url_key: { eq: $urlKey }
      }) {
        items {
          sku
          custom_attributesV2 {
            items {
              code
              ... on AttributeValue { value }
              ... on AttributeSelectedOptions {
                selected_options { label value }
              }
            }
          }
        }
      }
    }
    '''
    data = _gql(session, query, variables={"urlKey": url_key})
    items = data.get("products", {}).get("items") or []
    if not items:
        raise RuntimeError(f"未找到房源: {url_key}")

    item = items[0]
    sku = item["sku"]

    contract_id: Optional[int] = None
    next_start_date: Optional[str] = None
    avail_date: Optional[str] = None

    for attr in item.get("custom_attributesV2", {}).get("items", []):
        code = attr.get("code", "")
        if code == "type_of_contract":
            opts = attr.get("selected_options") or []
            if opts:
                try:
                    contract_id = int(opts[0]["value"])
                except (KeyError, ValueError, TypeError):
                    pass
        elif code == "next_contract_startdate":
            raw = (attr.get("value") or "").strip()[:10]  # "YYYY-MM-DD"
            if raw:
                next_start_date = raw
        elif code == "available_startdate":
            raw = (attr.get("value") or "").strip()[:10]
            if raw:
                avail_date = raw

    # 选择入住日期：优先 next_contract_startdate，其次 available_startdate
    # 过去的日期不传（传过期日期服务端会报错）
    from datetime import date
    today_str = date.today().isoformat()
    candidate = next_start_date or avail_date
    start_date = candidate if (candidate and candidate >= today_str) else None

    return sku, contract_id, start_date
