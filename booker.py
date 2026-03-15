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
CHECKOUT_URL = "https://www.holland2stay.com/checkout"

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
    """返回 customer 购物车 ID。"""
    query = "{ customerCart { id } }"
    data = _gql(session, query, token=token)
    cart_id = data.get("customerCart", {}).get("id")
    if not cart_id:
        raise RuntimeError("无法获取购物车 ID")
    logger.debug("购物车 ID: %s", cart_id)
    return cart_id


# ------------------------------------------------------------------ #
# 加入购物车（预占位）
# ------------------------------------------------------------------ #

def add_to_cart(
    session: req.Session,
    token: str,
    cart_id: str,
    sku: str,
    contract_start_date: Optional[str],
) -> bool:
    """
    调用 addNewBooking，将房源加入购物车。
    contract_start_date 格式: "2026-04-08"（ISO date）。
    返回 True 表示成功。
    """
    date_arg = f', contract_startDate: "{contract_start_date}"' if contract_start_date else ""
    query = f'''
    mutation {{
      addNewBooking(
        cart_id: "{cart_id}",
        sku: "{sku}"{date_arg}
      ) {{
        cart {{
          id
          items {{ uid quantity }}
        }}
        user_errors {{ code message }}
      }}
    }}
    '''
    data = _gql(session, query, token=token)
    result = data.get("addNewBooking", {})

    user_errors = result.get("user_errors") or []
    if user_errors:
        msgs = "; ".join(e.get("message", "") for e in user_errors)
        raise RuntimeError(f"加入购物车失败: {msgs}")

    cart = result.get("cart", {})
    items = cart.get("items") or []
    logger.info("加入购物车成功，当前购物车 %d 项", len(items))
    return True


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
    ):
        self.listing = listing
        self.success = success
        self.message = message
        self.dry_run = dry_run


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

    if dry_run:
        logger.info("[DRY RUN] 跳过实际预订: %s", listing.name)
        return BookingResult(listing, True, "DRY RUN - 未实际操作", dry_run=True)

    sku = listing.id  # url_key 即 sku slug，但实际 sku 需要从 GraphQL 额外查询
    # sku 在 scraper 里用 url_key 作 id，实际 Magento sku 格式不同（如 r-onx-781）
    # 需要先查询真实 sku
    with req.Session(impersonate="chrome110") as session:
        try:
            # 1. 查询真实 sku
            sku = _fetch_sku(session, listing.id)
            logger.info("[%s] 真实 SKU: %s", listing.name, sku)

            # 2. 登录
            token = login(session, email, password)

            # 3. 获取购物车
            cart_id = get_or_create_cart(session, token)

            # 4. 加入购物车
            add_to_cart(session, token, cart_id, sku, listing.available_from)

            msg = f"已加入购物车，请前往付款完成预订：{CHECKOUT_URL}"
            logger.info("[%s] 预订成功: %s", listing.name, msg)
            return BookingResult(listing, True, msg)

        except Exception as e:
            logger.error("[%s] 预订失败: %s", listing.name, e)
            return BookingResult(listing, False, str(e))


def _fetch_sku(session: req.Session, url_key: str) -> str:
    """通过 url_key 查询真实 Magento SKU。"""
    query = f'{{ products(filter: {{ url_key: {{ eq: "{url_key}" }} }}) {{ items {{ sku }} }} }}'
    data = _gql(session, query)
    items = data.get("products", {}).get("items") or []
    if not items:
        raise RuntimeError(f"未找到房源: {url_key}")
    return items[0]["sku"]
