from __future__ import annotations

"""
Holland2Stay 自动预订模块
=========================
针对 "Available to book" 的房源，自动完成以下步骤：
  1. 登录账号，获取 Bearer token
  2. 获取购物车 ID
  3. 调用 addNewBooking 将房源加入购物车（相当于"锁定"位置）
  4. 发送 iMessage 通知，附带付款链接，由用户手动完成支付

注意：placeOrder（下单/支付）步骤不会自动执行，需要用户手动操作。
"""

import logging
from typing import Optional

import curl_cffi.requests as req

from models import Listing

logger = logging.getLogger(__name__)

GQL_URL = "https://api.holland2stay.com/graphql/"
# 购物车通过 API 加入，绑定到账号服务端。浏览器需要先登录才能看到购物车。
# 直接打开 /checkout 会显示登录框，登录后自动跳转付款页
CHECKOUT_URL = "https://www.holland2stay.com/checkout"
CART_URL     = CHECKOUT_URL          # 两个 URL 相同，登录后自动显示购物车

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.holland2stay.com",
    "Referer": "https://www.holland2stay.com/",
}


# ------------------------------------------------------------------ #
# GraphQL helpers
# ------------------------------------------------------------------ #

def _gql(session: req.Session, query: str, token: Optional[str] = None) -> dict:
    headers = dict(_BASE_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = session.post(GQL_URL, json={"query": query}, headers=headers, timeout=30)
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
    """返回 customer token。"""
    escaped_email = email.replace('"', '\\"')
    escaped_pw = password.replace('"', '\\"').replace('\\', '\\\\')
    query = f'''
    mutation {{
      generateCustomerToken(email: "{escaped_email}", password: "{escaped_pw}") {{
        token
      }}
    }}
    '''
    data = _gql(session, query)
    token = data.get("generateCustomerToken", {}).get("token")
    if not token:
        raise RuntimeError("登录失败：未获取到 token")
    logger.debug("登录成功")
    return token


# ------------------------------------------------------------------ #
# 获取购物车
# ------------------------------------------------------------------ #

def get_or_create_cart(session: req.Session, token: str) -> str:
    """返回 customer 购物车 ID，同时清空购物车内的旧预订项目。"""
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
    """清空购物车内所有条目（TruncateCartOutput 只有 status: Boolean）。"""
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
    查询账号下所有 pending/reserved 状态的订单并逐一取消。
    返回成功取消的订单数。这是避免 "you have another unit reserved" 错误的必要步骤。
    """
    # 查询近 10 笔订单，筛选出待取消的状态
    query = '''
    {
      customer {
        orders(pageSize: 10, currentPage: 1) {
          items {
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
    to_cancel = [o["number"] for o in items if o.get("status", "").lower() in CANCEL_STATUSES]

    if not to_cancel:
        logger.debug("无 pending 订单，无需取消")
        return 0

    logger.info("发现 %d 笔 pending 订单，准备取消: %s", len(to_cancel), to_cancel)
    cancelled = 0
    for order_number in to_cancel:
        try:
            q = f'mutation {{ cancelOrder(input: {{ order_id: "{order_number}" }}) {{ error errorV2 {{ message code }} }} }}'
            result = _gql(session, q, token=token)
            cancel_result = result.get("cancelOrder") or {}
            err = cancel_result.get("error") or (cancel_result.get("errorV2") or {}).get("message")
            if err:
                logger.warning("取消订单 #%s 失败: %s", order_number, err)
            else:
                logger.info("已取消订单 #%s", order_number)
                cancelled += 1
        except Exception as e:
            logger.warning("取消订单 #%s 异常（忽略）: %s", order_number, e)

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
    调用 addNewBooking，将房源加入购物车。
    contract_start_date 格式: "2026-04-08"（ISO date，必须是未来日期）。
    contract_id: 合同类型 ID（来自 type_of_contract 属性）。

    关键设计：
    - 响应只请求 user_errors，不请求 cart{}。
      因为 AddProductsToCartOutput.cart 是 NON_NULL——若服务端处理异常导致 cart=null，
      GraphQL 会把它上升为顶层 "Internal server error"，掩盖真正的错误原因。
      只请求 user_errors 可以绕过这个问题，拿到可读的错误描述。
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
    完成结账流程，返回直接可用的支付跳转 URL。
    流程：setPaymentMethodOnCart → placeOrder → idealCheckOut → redirect URL
    支付域名在 account.holland2stay.com（不是 www），所以不能用前端路由。
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
    logger.info("支付链接: %s", pay_url)
    return pay_url


# ------------------------------------------------------------------ #
# 主入口
# ------------------------------------------------------------------ #

class BookingResult:
    def __init__(
        self,
        listing: Listing,
        success: bool,
        message: str,
        dry_run: bool = False,
        pay_url: str = "",
    ):
        self.listing = listing
        self.success = success
        self.message = message
        self.dry_run = dry_run
        self.pay_url = pay_url


def try_book(
    listing: Listing,
    email: str,
    password: str,
    *,
    dry_run: bool = False,
) -> BookingResult:
    """
    对单个 "Available to book" 房源执行自动预订流程。
    dry_run=True 时只登录验证，不实际加入购物车。
    """
    if listing.status.lower() not in ("available to book",):
        return BookingResult(listing, False, f"状态不是 Available to book: {listing.status}")

    with req.Session(impersonate="chrome110") as session:
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
            logger.info("[%s] 预订成功  pay_url=%s  入住:%s", listing.name, pay_url, start_date)
            return BookingResult(listing, True, msg, pay_url=pay_url)

        except Exception as e:
            logger.error("[%s]%s 预订失败: %s", listing.name, " [DRY RUN]" if dry_run else "", e)
            return BookingResult(listing, False, str(e))


def _fetch_sku_and_contract(session: req.Session, url_key: str) -> tuple[str, Optional[int], Optional[str]]:
    """
    通过 url_key 查询真实 Magento SKU、合同类型 ID 及下一个可用入住日期。
    返回 (sku, contract_id, next_start_date)。
    - contract_id: 来自 type_of_contract.value，不传会导致 Internal server error
    - next_start_date: 优先取 next_contract_startdate（格式 "YYYY-MM-DD"），
                       其次取 available_startdate；过去的日期会被置为 None
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
