"""
booker.py — 自动预订模块
==========================
对 "Available to book" 的房源执行完整的自动化预订流程，最终生成可直接支付的链接。

完整流程（try_book 内部）
--------------------------
1. _fetch_sku_and_contract()
       通过 url_key 查询 Magento SKU + type_of_contract ID + 下一个入住日期
2. login()
       generateCustomerToken mutation → Bearer token
3. get_or_create_cart()
       customerCart query → cart_id；若购物车有旧条目则 truncateCart 清空
4. add_to_cart()
       addNewBooking mutation → 将押金项（Deposit €200）加入购物车
       注意：只请求 user_errors，不请求 cart{}（NON_NULL 传播 bug，见函数注释）
5. place_order_and_pay()
       setPaymentMethodOnCart → placeOrder → idealCheckOut → 返回直链付款 URL
       支付域名在 account.holland2stay.com（不是 www），链接无需登录即可直接付款
       ┣ 若 placeOrder 返回「账号已有预留单」错误：
       ┃   调用 cancel_pending_orders() 取消旧单，然后重试 place_order_and_pay()
       ┗ 若 placeOrder 返回「房源已被他人预订」：竞争失败，通知用户含手动预订链接

注意：cancel_pending_orders() 不再在 add_to_cart 之前预先调用。
该操作涉及 2-5 次额外 HTTP 往返（查询订单 + schema 内省 + 取消 mutation），
会给关键路径增加 5-15 秒，显著降低在高竞争房源中的成功率。

GraphQL API
-----------
端点：https://api.holland2stay.com/graphql/（Magento 后端）
认证：generateCustomerToken 换取 Bearer token，后续请求附加 Authorization 头

对外接口
--------
- try_book(listing, email, password, *, dry_run) → BookingResult

依赖
----
curl_cffi.requests（绕过 Cloudflare），models.Listing
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import curl_cffi.requests as req

from models import Listing

logger = logging.getLogger(__name__)


GQL_URL = "https://api.holland2stay.com/graphql/"

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.holland2stay.com",
    "Referer": "https://www.holland2stay.com/",
}


# ------------------------------------------------------------------ #
# 错误分类（placeOrder / addNewBooking 业务错误识别）
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

    Parameters
    ----------
    session  : curl_cffi Session
    email    : Holland2Stay 账号邮箱
    password : Holland2Stay 账号密码

    Returns
    -------
    Bearer token 字符串，用于后续所有需要鉴权的 GraphQL 请求

    Raises
    ------
    RuntimeError 登录失败或响应中无 token

    注意
    ----
    邮箱和密码通过 GraphQL variables 传递（而非拼入 query 字符串），
    由 json.dumps 负责转义，含 "、\、控制字符的密码均可正确处理。
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
# 获取购物车
# ------------------------------------------------------------------ #

def get_or_create_cart(session: req.Session, token: str) -> str:
    """
    获取当前账号的购物车 ID，并清空购物车内的旧条目。

    流程
    ----
    1. customerCart query 获取 cart_id 和现有条目（itemsV2）
    2. 若有旧条目 → 调用 _truncate_cart() 清空，避免重复预订错误

    Returns
    -------
    cart_id 字符串（Magento 购物车唯一标识）

    Raises
    ------
    RuntimeError 无法获取 cart_id 时
    """
    # 先取购物车 ID 和当前条目（用于清理）
    query = '''
    {
      customerCart {
        id
        itemsV2 { items { uid } }
      }
    }
    '''
    data = _gql(session, query, token=token)
    cart_info = data.get("customerCart", {})
    cart_id = cart_info.get("id")
    if not cart_id:
        raise RuntimeError("无法获取购物车 ID")

    # 清空旧条目，避免 "cart already has booking" 类错误
    items = (cart_info.get("itemsV2") or {}).get("items") or []
    if items:
        logger.debug("购物车有 %d 个旧项目，正在清空...", len(items))
        _truncate_cart(session, token, cart_id)

    logger.debug("购物车 ID: %s", cart_id)
    return cart_id


def _truncate_cart(session: req.Session, token: str, cart_id: str) -> None:
    """
    清空购物车内所有条目。

    使用 truncateCart mutation，该 mutation 的返回类型 TruncateCartOutput
    只有 `status: Boolean` 字段（不含 cart{}，因此不会触发 NON_NULL 传播问题）。

    失败不抛出异常，只记录 warning，不阻断后续预订流程。
    """
    query = 'mutation TruncateCart($cartId: String!) { truncateCart(cart_id: $cartId) { status } }'
    try:
        data = _gql(session, query, token=token, variables={"cartId": cart_id})
        ok = (data.get("truncateCart") or {}).get("status")
        logger.debug("购物车已清空 status=%s", ok)
    except Exception as e:
        # 清空失败不阻断流程，记录警告即可
        logger.warning("清空购物车失败（忽略）: %s", e)


# ------------------------------------------------------------------ #
# 取消 pending 订单（清除"already reserved"锁）
# ------------------------------------------------------------------ #

def cancel_pending_orders(session: req.Session, token: str) -> int:
    """
    查询账号近 10 笔订单，通过标准 Magento cancelOrder mutation 取消所有
    pending/reserved 状态的订单。

    背景
    ----
    Holland2Stay 的 placeOrder 会检查账号下是否已有预留单，若有则返回：
    "Sorry, at the moment you have another unit reserved."
    在新预订前必须取消旧的 pending 订单才能成功下单。

    实现策略
    --------
    使用 Magento 标准 cancelOrder mutation（不再内省 schema）。
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
    调用 addNewBooking mutation，将押金项加入购物车（相当于锁定预订位置）。

    Parameters
    ----------
    session             : curl_cffi Session
    token               : Bearer token
    cart_id             : get_or_create_cart() 返回的购物车 ID
    sku                 : 房源的 Magento SKU（由 _fetch_sku_and_contract 获取）
    contract_start_date : 入住日期，格式 "YYYY-MM-DD"，必须是未来日期；
                          None 时不传，由服务端决定（可能导致 Internal server error）
    contract_id         : 合同类型 ID，来自 type_of_contract 属性；
                          None 时不传，可能导致 Internal server error

    Returns
    -------
    True（成功）

    Raises
    ------
    RuntimeError 含 user_errors 时（如 "cart already has booking"）

    关键设计：NON_NULL 传播绕过
    ---------------------------
    此函数不使用 _gql() 而是直接调用 session.post()。
    原因：AddProductsToCartOutput.cart 字段是 NON_NULL 类型。
    若请求 cart{} 且服务端处理失败（cart=null），GraphQL 会将 null 上升为
    顶层 "Internal server error"，掩盖 user_errors 中的真实错误原因。
    只请求 user_errors 可绕过此问题，直接获得可读的业务错误信息。

    注意
    ----
    addNewBooking 实际上往购物车加入的是押金项（Deposit €200），
    不是房源本身。后续需调用 placeOrder 才能真正产生订单。
    """
    # 所有用户数据（cart_id、sku、日期、合同 ID）通过 GraphQL variables 传递，
    # 不插入 mutation 字符串，防止注入。
    # optional 参数需要条件性地出现在 mutation 签名和参数列表中，
    # 因此用 f-string 控制"有哪些参数"，但参数的值始终来自 variables dict。
    var_decls = "$cartId: String!, $sku: String!"
    arg_uses  = "cart_id: $cartId, sku: $sku"
    variables: dict = {"cartId": cart_id, "sku": sku}

    if contract_start_date:
        var_decls += ", $startDate: String!"
        arg_uses  += ", contract_startDate: $startDate"
        variables["startDate"] = contract_start_date

    if contract_id is not None:
        var_decls += ", $contractId: Int!"
        arg_uses  += ", contract_id: $contractId"
        variables["contractId"] = contract_id

    query = f'''
    mutation AddNewBooking({var_decls}) {{
      addNewBooking({arg_uses}) {{
        user_errors {{ code message }}
      }}
    }}
    '''
    resp = session.post(
        GQL_URL,
        json={"query": query, "variables": variables},
        headers={**_BASE_HEADERS, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()

    # 详细记录原始响应（仅 DEBUG 级别）
    logger.debug("addNewBooking raw response: %s", raw)

    # GraphQL 层错误（比如字段不存在、类型错误）
    if "errors" in raw:
        msgs = "; ".join(e.get("message", "") for e in raw["errors"])
        # 若同时含有 data，说明是 partial error，先看 user_errors
        if not raw.get("data"):
            raise RuntimeError(f"GraphQL 错误: {msgs}")

    result = (raw.get("data") or {}).get("addNewBooking") or {}
    user_errors = result.get("user_errors") or []
    if user_errors:
        msgs = "; ".join(
            f"[{e.get('code','?')}] {e.get('message','')}" for e in user_errors
        )
        raise RuntimeError(f"加入购物车失败: {msgs}")

    logger.info("加入购物车成功")
    return True


# ------------------------------------------------------------------ #
# 下单 + 生成支付链接
# ------------------------------------------------------------------ #

def place_order_and_pay(
    session: req.Session,
    token: str,
    cart_id: str,
    payment_method: str = "idealcheckout_ideal",
) -> str:
    """
    执行完整的结账流程，返回可直接跳转的 iDEAL 支付链接。

    流程
    ----
    1. setPaymentMethodOnCart：设置支付方式为 idealcheckout_ideal
    2. placeOrder：将购物车转化为正式订单，获取 order_number
    3. idealCheckOut：根据 order_number 生成支付链接

    Parameters
    ----------
    session        : curl_cffi Session
    token          : Bearer token
    cart_id        : 含押金项的购物车 ID
    payment_method : 支付方式代码，默认 "idealcheckout_ideal"（iDEAL 银行转账）

    Returns
    -------
    支付链接，形如：
    https://account.holland2stay.com/idealcheckout/setup.php?order_id=XXX&order_code=YYY
    该链接无需登录即可访问，有效期有限，需尽快完成支付。

    Raises
    ------
    RuntimeError 任何步骤失败时（含下单失败、支付链接获取失败）

    注意
    ----
    支付域名是 account.holland2stay.com（后端 PHP），
    不是 www.holland2stay.com（Next.js 前端），两者是完全不同的系统。
    """
    # 1. 设置支付方式（cart_id、payment_method 均通过 variables 传递）
    tp0 = time.monotonic()
    q = '''
    mutation SetPayment($cartId: String!, $code: String!) {
      setPaymentMethodOnCart(input: {
        cart_id: $cartId,
        payment_method: { code: $code }
      }) {
        cart { selected_payment_method { code } }
      }
    }
    '''
    _gql(session, q, token=token, variables={"cartId": cart_id, "code": payment_method})
    t_setpay = time.monotonic() - tp0
    logger.debug("支付方式已设置: %s  (%.2fs)", payment_method, t_setpay)

    # 2. 下单（cart_id 通过 variable 传递）
    tp1 = time.monotonic()
    q = '''
    mutation PlaceOrder($cartId: String!) {
      placeOrder(input: { cart_id: $cartId }) {
        errors { message code }
        orderV2 { number status }
      }
    }
    '''
    data = _gql(session, q, token=token, variables={"cartId": cart_id})
    order_result = (data.get("placeOrder") or {})
    errs = order_result.get("errors") or []
    if errs:
        msgs = "; ".join(e.get("message","") for e in errs)
        raise RuntimeError(f"下单失败: {msgs}")
    order_number = (order_result.get("orderV2") or {}).get("number")
    if not order_number:
        raise RuntimeError("下单失败：未获取到订单号")
    t_place = time.monotonic() - tp1
    logger.info("订单创建成功: #%s  (%.2fs)", order_number, t_place)

    # 3. 生成 iDEAL/idealcheckout 支付跳转链接（order_number 通过 variable 传递）
    # plateform: "web" 是 H2S API 要求的常量字符串，直接内联
    tp2 = time.monotonic()
    q = '''
    mutation IdealCheckOut($orderId: String!) {
      idealCheckOut(order_id: $orderId, plateform: "web") {
        redirect
      }
    }
    '''
    data = _gql(session, q, token=token, variables={"orderId": order_number})
    pay_url = (data.get("idealCheckOut") or {}).get("redirect")
    if not pay_url:
        raise RuntimeError(f"未能获取支付链接 (order #{order_number})")
    t_ideal = time.monotonic() - tp2
    logger.info(
        "支付链接已生成 | pay 子步骤: setpay=%.2fs place=%.2fs ideal=%.2fs",
        t_setpay, t_place, t_ideal,
    )
    return pay_url


# ------------------------------------------------------------------ #
# 主入口
# ------------------------------------------------------------------ #

class BookingResult:
    """
    try_book() 的返回值，封装预订结果。

    Attributes
    ----------
    listing               : 被尝试预订的房源
    success               : True 表示流程全部成功（或 dry_run 验证通过）
    message               : 发送给用户的通知消息（含付款链接或失败原因）
    dry_run               : True 表示是 dry_run 模式产生的结果（未实际提交）
    pay_url               : place_order_and_pay() 返回的直链付款 URL；
                            失败时为空字符串；dry_run 时也为空字符串
    contract_start_date   : _fetch_sku_and_contract() 从 API 获取的实际合同开始日期，
                            格式 "YYYY-MM-DD"；未知时为空字符串。
                            此值可能与抓取时记录的 Listing.available_from 不同——
                            API 在预订时返回 next_contract_startdate，
                            而 available_from 是监控轮询时抓取到的快照，
                            两者在时间差内可能出现不一致。
    """
    def __init__(
        self,
        listing: Listing,
        success: bool,
        message: str,
        dry_run: bool = False,
        pay_url: str = "",
        contract_start_date: str = "",
    ):
        self.listing = listing
        self.success = success
        self.message = message
        self.dry_run = dry_run
        self.pay_url = pay_url
        self.contract_start_date = contract_start_date


def try_book(
    listing: Listing,
    email: str,
    password: str,
    *,
    dry_run: bool = False,
    cancel_enabled: bool = False,
) -> BookingResult:
    """
    对单个 "Available to book" 房源执行完整的自动预订流程。

    Parameters
    ----------
    listing        : 目标房源（status 必须为 "Available to book"，否则立即返回失败）
    email          : Holland2Stay 账号邮箱
    password       : Holland2Stay 账号密码
    dry_run        : True 时只完成 SKU 查询/登录/购物车验证，不提交预订
    cancel_enabled : True 时若 placeOrder 返回 "another unit reserved"，
                     则自动取消旧订单后重试；False（默认）时直接通知用户

    Returns
    -------
    BookingResult：
    - success=True, pay_url 非空  → 预订成功，message 含直链付款 URL
    - success=True, dry_run=True  → dry_run 验证通过
    - success=False               → 任何步骤失败，message 含错误原因

    调用方式
    --------
    由 monitor.py 的 run_once() 通过 run_in_executor 在线程池中调用
    （此函数是同步的，使用 curl_cffi 同步 HTTP，不能直接在事件循环中 await）。

    下单策略
    --------
    首次直接尝试 placeOrder；若返回「房源已被他人预订」则立即通知用户（竞争失败）。
    若返回「账号已有预留单」且 cancel_enabled=True → 取消旧单后重试一次。
    若返回「账号已有预留单」且 cancel_enabled=False → 直接通知用户（人工介入）。

    异常处理
    --------
    所有内部异常均被捕获并转化为 BookingResult(success=False)，不向上传播。
    """
    if listing.status.lower() not in ("available to book",):
        return BookingResult(listing, False, f"状态不是 Available to book: {listing.status}")

    t0 = time.monotonic()
    t_cancel = 0.0
    phase = ""

    # ---------------------------------------------------------------- #
    # Step 1: 确定 SKU / contract_id / contract_start_date
    # ---------------------------------------------------------------- #
    # 方案 1（前置提取）：scraper 已在抓取时提取 sku/contract_id/contract_start_date，
    # 直接使用 Listing 上的字段，省去一次独立 HTTP 查询（~0.5-1.5s）。
    # 旧数据回退：若 Listing 无 sku（旧版抓取或 DB 回填），降级到 _fetch_sku_and_contract()。
    # ---------------------------------------------------------------- #
    if listing.sku:
        t_sku = 0.0
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

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    proxies = {"https": proxy, "http": proxy} if proxy else {}
    with req.Session(impersonate="chrome110", proxies=proxies) as session:
        try:
            if not listing.sku:
                t1 = time.monotonic()
                sku, contract_id, start_date = _fetch_sku_and_contract(session, listing.id)
                t_sku = time.monotonic() - t1
                logger.info(
                    "[%s]%s SKU: %s  contract_id: %s  start_date: %s  (%.2fs) [fallback]",
                    listing.name, " [DRY RUN]" if dry_run else "",
                    sku, contract_id, start_date or "(不传，由服务端决定)", t_sku,
                )

            # 2. 登录验证账号
            t2 = time.monotonic()
            token = login(session, email, password)
            t_login = time.monotonic() - t2
            logger.info("[%s]%s 登录成功 (%.2fs)", listing.name, " [DRY RUN]" if dry_run else "", t_login)

            # 3. 获取购物车 ID
            t3 = time.monotonic()
            cart_id = get_or_create_cart(session, token)
            t_cart = time.monotonic() - t3
            logger.info("[%s]%s 购物车 ID: %s  (%.2fs)", listing.name, " [DRY RUN]" if dry_run else "", cart_id, t_cart)

            # 4. 加入购物车（dry_run 时跳过此步）
            if dry_run:
                total = time.monotonic() - t0
                msg = "[DRY RUN] 验证通过（SKU/登录/购物车均正常），未实际提交预订"
                logger.info(
                    "[%s] %s | 耗时 total=%.1fs (sku=%.2fs login=%.2fs cart=%.2fs)",
                    listing.name, msg, total, t_sku, t_login, t_cart,
                )
                return BookingResult(listing, True, msg, dry_run=True)

            logger.debug("[%s] 加入购物车 (contract_id=%s, start_date=%s)...", listing.name, contract_id, start_date)
            t4 = time.monotonic()
            add_to_cart(session, token, cart_id, sku, start_date, contract_id)
            t_add = time.monotonic() - t4

            # 5. 下单并生成直接支付链接
            logger.debug("[%s] 尝试下单...", listing.name)
            booking_url = f"https://www.holland2stay.com/residences/{listing.id}.html"
            t5 = time.monotonic()
            try:
                pay_url = place_order_and_pay(session, token, cart_id)
                t_pay = time.monotonic() - t5
                phase = "success"
            except RuntimeError as order_err:
                t_pay = time.monotonic() - t5
                err_str = str(order_err)

                if _is_booked_by_other(err_str):
                    phase = "race_lost"
                    logger.warning(
                        "[%s] 竞争失败：房源已被他人预订 (%s)", listing.name, err_str
                    )
                    raise RuntimeError(
                        f"房源已被他人抢先预订，竞争失败。\n\n"
                        f"💡 如房源重新开放，可尝试手动预订：\n{booking_url}"
                    ) from order_err

                elif _is_reserved_by_user(err_str):
                    if not cancel_enabled:
                        phase = "reserved_conflict"
                        logger.info(
                            "[%s] 账号已有预留单且 cancel_enabled=false，直接通知用户",
                            listing.name,
                        )
                        raise RuntimeError(
                            "该账号尚有未完成的预留订单，请登录 Holland2Stay 手动取消后再试。\n\n"
                            f"💡 手动预订入口：\n{booking_url}"
                        ) from order_err

                    # cancel_enabled=True：取消旧单后重试一次
                    phase = "cancel+retry"
                    logger.info(
                        "[%s] 账号已有预留单（%s），正在取消后重试...",
                        listing.name, err_str,
                    )
                    tc1 = time.monotonic()
                    cancelled = cancel_pending_orders(session, token)
                    t_cancel = time.monotonic() - tc1
                    logger.info("[%s] 已取消 %d 笔旧订单 (%.2fs)，重新下单...", listing.name, cancelled, t_cancel)
                    t5 = time.monotonic()
                    pay_url = place_order_and_pay(session, token, cart_id)
                    t_pay = time.monotonic() - t5

                else:
                    phase = "unknown_order_error"
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
            parts = f"sku={t_sku:.2f}s login={t_login:.2f}s cart={t_cart:.2f}s add={t_add:.2f}s pay={t_pay:.2f}s"
            if t_cancel:
                parts += f" cancel={t_cancel:.2f}s"
            logger.info(
                "[%s] 预订成功  入住:%s | 耗时 total=%.1fs (%s)",
                listing.name, start_date, total, parts,
            )
            return BookingResult(listing, True, msg, pay_url=pay_url,
                                 contract_start_date=start_date or "")

        except Exception as e:
            total = time.monotonic() - t0
            logger.error(
                "[%s]%s 预订失败 (%s) | 耗时 total=%.1fs",
                listing.name, " [DRY RUN]" if dry_run else "", phase, total,
            )
            return BookingResult(listing, False, str(e))


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
    # 过去的日期不传（传过去日期服务端会 Internal server error）
    from datetime import date
    today_str = date.today().isoformat()
    candidate = next_start_date or avail_date
    start_date = candidate if (candidate and candidate >= today_str) else None

    return sku, contract_id, start_date
