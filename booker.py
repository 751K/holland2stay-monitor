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
4. cancel_pending_orders()
       查询近 10 笔订单，取消所有 pending/reserved 状态的订单，
       避免 "you have another unit reserved" 错误
5. add_to_cart()
       addNewBooking mutation → 将押金项（Deposit €200）加入购物车
       注意：只请求 user_errors，不请求 cart{}（NON_NULL 传播 bug，见函数注释）
6. place_order_and_pay()
       setPaymentMethodOnCart → placeOrder → idealCheckOut → 返回直链付款 URL
       支付域名在 account.holland2stay.com（不是 www），链接无需登录即可直接付款

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
    query = f'mutation {{ truncateCart(cart_id: "{cart_id}") {{ status }} }}'
    try:
        data = _gql(session, query, token=token)
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
    查询账号近 10 笔订单，取消所有 pending/reserved 状态的订单。

    背景
    ----
    Holland2Stay 的 placeOrder 会检查账号下是否已有预留单，若有则返回：
    "Sorry, at the moment you have another unit reserved."
    在新预订前必须取消旧的 pending 订单才能成功下单。

    实现策略
    --------
    1. 先内省 GraphQL schema，找出所有含 "cancel" 的 mutation 名称
    2. 对每个候选 mutation 依次尝试（先不带 reason，失败后补 reason 重试）
    3. 任一 mutation 成功即停止尝试下一个
    4. 所有尝试均失败时只记 warning，不中断预订流程

    注意
    ----
    标准 Magento cancelOrder mutation 在此平台被禁用
    （"Order cancellation is not enabled for requested store"）。
    通过 schema 内省动态发现可用 mutation 以适应平台定制。
    内省结果未缓存，每次预订都会额外发一次 schema 查询请求。

    Returns
    -------
    成功取消的订单数（0 表示无需取消或取消失败）
    """
    # 查询近 10 笔订单，筛选出待取消的状态
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
    # 需要取消的状态（大小写不敏感）
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

    # 先内省 schema，找出实际可用的取消/预订相关 mutation
    try:
        intro = _gql(session, '{ __schema { mutationType { fields { name } } } }', token=token)
        all_mutations = [f["name"] for f in (intro.get("__schema", {}).get("mutationType") or {}).get("fields", [])]
        relevant = [m for m in all_mutations if any(kw in m.lower() for kw in ("cancel", "booking", "order", "reserv"))]
        logger.debug("Schema 相关 mutation: %s", relevant)
    except Exception:
        relevant = []

    # 优先尝试 Holland2Stay 自定义取消 mutation，降级到标准 cancelOrder
    cancel_mutations = [m for m in relevant if "cancel" in m.lower()]
    logger.debug("候选取消 mutation: %s", cancel_mutations)

    cancelled = 0
    for order_uid, order_number in to_cancel:
        success = False
        for mut in cancel_mutations:
            try:
                # 先尝试只传 order_id（Holland2Stay 自定义 mutation 可能不需要 reason）
                q = f'mutation {{ {mut}(input: {{ order_id: "{order_uid}" }}) {{ error errorV2 {{ message code }} }} }}'
                result = _gql(session, q, token=token)
                cancel_result = result.get(mut) or {}
                err = cancel_result.get("error") or (cancel_result.get("errorV2") or {}).get("message")
                if err:
                    logger.debug("%s #%s 失败（忽略，尝试下一个）: %s", mut, order_number, err)
                    continue
                logger.info("已取消订单 #%s (via %s)", order_number, mut)
                cancelled += 1
                success = True
                break
            except Exception as e:
                err_str = str(e)
                # 如果缺少 reason 字段，补上重试
                if "reason" in err_str.lower():
                    try:
                        q = f'mutation {{ {mut}(input: {{ order_id: "{order_uid}", reason: "Replaced by new booking" }}) {{ error errorV2 {{ message code }} }} }}'
                        result = _gql(session, q, token=token)
                        cancel_result = result.get(mut) or {}
                        err2 = cancel_result.get("error") or (cancel_result.get("errorV2") or {}).get("message")
                        if not err2:
                            logger.info("已取消订单 #%s (via %s + reason)", order_number, mut)
                            cancelled += 1
                            success = True
                            break
                    except Exception:
                        pass
                logger.debug("%s #%s 异常（尝试下一个）: %s", mut, order_number, e)
        if not success:
            logger.warning("订单 #%s 无法自动取消（所有 mutation 均失败），继续尝试预订", order_number)

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
    date_arg = f', contract_startDate: "{contract_start_date}"' if contract_start_date else ""
    cid_arg  = f', contract_id: {contract_id}' if contract_id is not None else ""
    query = f'''
    mutation {{
      addNewBooking(
        cart_id: "{cart_id}",
        sku: "{sku}"{date_arg}{cid_arg}
      ) {{
        user_errors {{ code message }}
      }}
    }}
    '''
    resp = session.post(
        GQL_URL,
        json={"query": query},
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
    auth_header = {**_BASE_HEADERS, "Authorization": f"Bearer {token}"}

    # 1. 设置支付方式
    q = f'''
    mutation {{
      setPaymentMethodOnCart(input: {{
        cart_id: "{cart_id}",
        payment_method: {{ code: "{payment_method}" }}
      }}) {{
        cart {{ selected_payment_method {{ code }} }}
      }}
    }}
    '''
    _gql(session, q, token=token)
    logger.debug("支付方式已设置: %s", payment_method)

    # 2. 下单
    q = f'''
    mutation {{
      placeOrder(input: {{ cart_id: "{cart_id}" }}) {{
        errors {{ message code }}
        orderV2 {{ number status }}
      }}
    }}
    '''
    data = _gql(session, q, token=token)
    order_result = (data.get("placeOrder") or {})
    errs = order_result.get("errors") or []
    if errs:
        msgs = "; ".join(e.get("message","") for e in errs)
        raise RuntimeError(f"下单失败: {msgs}")
    order_number = (order_result.get("orderV2") or {}).get("number")
    if not order_number:
        raise RuntimeError("下单失败：未获取到订单号")
    logger.info("订单创建成功: #%s", order_number)

    # 3. 生成 iDEAL/idealcheckout 支付跳转链接
    q = f'''
    mutation {{
      idealCheckOut(order_id: "{order_number}", plateform: "web") {{
        redirect
      }}
    }}
    '''
    data = _gql(session, q, token=token)
    pay_url = (data.get("idealCheckOut") or {}).get("redirect")
    if not pay_url:
        raise RuntimeError(f"未能获取支付链接 (order #{order_number})")
    logger.debug("支付链接已生成（见通知消息）")
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
) -> BookingResult:
    """
    对单个 "Available to book" 房源执行完整的自动预订流程。

    Parameters
    ----------
    listing  : 目标房源（status 必须为 "Available to book"，否则立即返回失败）
    email    : Holland2Stay 账号邮箱
    password : Holland2Stay 账号密码
    dry_run  : True 时只完成 SKU 查询/登录/购物车验证，不提交预订，
               用于配置验证（AutoBookConfig.dry_run）

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

    异常处理
    --------
    所有内部异常均被捕获并转化为 BookingResult(success=False)，不向上传播。
    """
    if listing.status.lower() not in ("available to book",):
        return BookingResult(listing, False, f"状态不是 Available to book: {listing.status}")

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    proxies = {"https": proxy, "http": proxy} if proxy else {}
    with req.Session(impersonate="chrome110", proxies=proxies) as session:
        try:
            # 1. 查询真实 SKU + contract_id + 入住日期
            sku, contract_id, start_date = _fetch_sku_and_contract(session, listing.id)
            logger.info(
                "[%s]%s SKU: %s  contract_id: %s  start_date: %s",
                listing.name, " [DRY RUN]" if dry_run else "",
                sku, contract_id, start_date or "(不传，由服务端决定)",
            )

            # 2. 登录验证账号
            logger.debug("[%s] 登录中...", listing.name)
            token = login(session, email, password)
            logger.info("[%s]%s 登录成功", listing.name, " [DRY RUN]" if dry_run else "")

            # 3. 获取购物车 ID
            logger.debug("[%s] 获取购物车...", listing.name)
            cart_id = get_or_create_cart(session, token)
            logger.info("[%s]%s 购物车 ID: %s", listing.name, " [DRY RUN]" if dry_run else "", cart_id)

            # 4. 加入购物车（dry_run 时跳过此步）
            if dry_run:
                msg = f"[DRY RUN] 验证通过（SKU/登录/购物车均正常），未实际提交预订"
                logger.info("[%s] %s", listing.name, msg)
                return BookingResult(listing, True, msg, dry_run=True)

            # 4b. 取消账号下所有 pending 订单，避免 "already reserved" 冲突
            logger.debug("[%s] 检查并取消 pending 订单...", listing.name)
            cancel_pending_orders(session, token)

            logger.debug("[%s] 加入购物车 (contract_id=%s, start_date=%s)...", listing.name, contract_id, start_date)
            add_to_cart(session, token, cart_id, sku, start_date, contract_id)

            # 5. 下单并生成直接支付链接
            logger.debug("[%s] 生成支付链接...", listing.name)
            pay_url = place_order_and_pay(session, token, cart_id)

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
            logger.info("[%s] 预订成功  入住:%s（付款链接已发送通知）", listing.name, start_date)
            return BookingResult(listing, True, msg, pay_url=pay_url,
                                 contract_start_date=start_date or "")

        except Exception as e:
            logger.error("[%s]%s 预订失败: %s", listing.name, " [DRY RUN]" if dry_run else "", e)
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
    query = f'''
    {{
      products(filter: {{
        category_uid: {{ eq: "Nw==" }}
        url_key: {{ eq: "{url_key}" }}
      }}) {{
        items {{
          sku
          custom_attributesV2 {{
            items {{
              code
              ... on AttributeValue {{ value }}
              ... on AttributeSelectedOptions {{
                selected_options {{ label value }}
              }}
            }}
          }}
        }}
      }}
    }}
    '''
    data = _gql(session, query)
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
