"""下单各步必须带**正确的** operationName——H2S 白名单按名字放行，缺了或错了都 403。

这一层守的是「调用点真的把照抄的 operation 名传下去了」。历史上 booker 的 _gql
一处都没传 operation_name，是 403 的成因之一（docs/H2S_BOOKING_OPS.md）。
"""
from __future__ import annotations

import pytest

import booker
import h2s_booking_gql as gql


class _CapturingFetcher:
    """记录每次 fetch_gql 的 (query, operation_name)，按需回预设响应。"""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def fetch_gql(self, query, variables=None, *, operation_name="",
                  extra_headers=None, timeout_ms=30_000):
        self.calls.append({"query": query, "operation_name": operation_name,
                           "variables": variables})
        return self._response


class TestOperationNamesThreaded:
    def test_fetch_sku_uses_get_product_detail(self):
        f = _CapturingFetcher({"data": {"products": {"items": [
            {"sku": "r-x-1", "type_of_contract": 21,
             "next_contract_startdate": "2099-09-01 00:00:00"}]}}})
        booker._fetch_sku_and_contract(f, "some-url-key")
        assert f.calls[0]["operation_name"] == gql.OP_GETPRODUCTDETAIL
        assert f.calls[0]["query"] is gql.GETPRODUCTDETAIL

    def test_create_empty_cart_uses_create_empty_cart_op(self):
        f = _CapturingFetcher({"data": {"createEmptyCart": "cart-123"}})
        booker.create_empty_cart(f, "tok")
        assert f.calls[0]["operation_name"] == gql.OP_CREATEEMPTYCART

    def test_set_payment_uses_set_payment_op(self):
        f = _CapturingFetcher({"data": {"setPaymentMethodOnCart": {
            "cart": {"selected_payment_method": {"code": "idealcheckout_ideal"}}}}})
        booker.set_payment_method(f, "tok", "cart-1")
        assert f.calls[0]["operation_name"] == gql.OP_SETPAYMENTMETHODONCART

    def test_place_order_uses_place_order_op(self):
        f = _CapturingFetcher({"data": {"placeOrder": {
            "orderV2": {"order_number": "H2S-1"}}}})
        booker.place_order(f, "tok", "cart-1")
        assert f.calls[0]["operation_name"] == gql.OP_PLACEORDER

    def test_ideal_checkout_uses_ideal_op(self):
        f = _CapturingFetcher({"data": {"idealCheckOut": {"redirect": "https://pay"}}})
        booker._ideal_checkout(f, "tok", "H2S-1")
        assert f.calls[0]["operation_name"] == gql.OP_IDEALCHECKOUT

    def test_add_to_cart_uses_add_booking_op(self):
        f = _CapturingFetcher({"data": {"addNewBooking": {
            "cart": {"items": [{"id": "1"}]}}}})
        booker.add_to_cart(f, "tok", "cart-1", "r-x-1", "2099-09-01")
        assert f.calls[0]["operation_name"] == gql.OP_ADDNEWBOOKING

    @pytest.mark.parametrize("call", [
        lambda f: booker.create_empty_cart(f, "tok"),
    ])
    def test_empty_operation_name_is_a_bug(self, call):
        """没有哪一步该发空 operationName——发了就是 403。"""
        f = _CapturingFetcher({"data": {"createEmptyCart": "c1"}})
        call(f)
        assert f.calls[0]["operation_name"], "operation_name 为空 = 必然 403"


class TestCancelUsesRestEnvelope:
    """取消预留必须走 ``fetch_rest``（信封），不是 ``fetch_plain``（明文）。

    /api/rest/* 在站点侧一律加密（module 82361 的 H/J）；发明文过去服务端解不开。
    曾经就是这么写错的。
    """

    class _Fetcher:
        def __init__(self):
            self.rest_calls = []
            self.plain_calls = []

        def fetch_rest(self, path, *, method="GET", body="", headers=None,
                       timeout_ms=30_000):
            self.rest_calls.append((path, method))
            import json as _j
            if method == "GET":
                return {"status": 200, "ok": True,
                        "text": _j.dumps({"items": [
                            {"sku": "r-x-1", "product_name": "X", "status": "reserved"}]}),
                        "headers": {}}
            return {"status": 200, "ok": True, "text": "{}", "headers": {}}

        def fetch_plain(self, *a, **k):
            self.plain_calls.append(a)
            raise AssertionError("/api/rest/* 走了明文——服务端解不开")

    def test_list_and_cancel_both_go_through_fetch_rest(self):
        import booker
        f = self._Fetcher()
        n = booker.cancel_pending_orders(f, "tok")
        assert n == 1
        assert f.plain_calls == []
        paths = [p for p, _ in f.rest_calls]
        assert any("newdashboard/contract/me" in p for p in paths), "没查预留列表"
        assert any("bookingcancel/r-x-1" in p for p in paths), "没按 SKU 取消"

    def test_cancel_is_post_and_list_is_get(self):
        import booker
        f = self._Fetcher()
        booker.cancel_pending_orders(f, "tok")
        methods = dict((p.split("?")[0], m) for p, m in f.rest_calls)
        assert methods["/api/rest/V1/newdashboard/contract/me"] == "GET"
        assert methods["/api/rest/V1/customer/bookingcancel/r-x-1"] == "POST"


class TestCreateBooking:
    """占房走 ``POST /api/booking``，不是 GraphQL addNewBooking。

    2026-08-19 实测：同一 clearance 窗口内 createEmptyCart 200、
    addNewBooking **403 not available through the public API**、其余 5 步 200。
    即 H2S 把占房摘出了公开 API，只能走站点自己的服务端代理。
    """

    class _F:
        def __init__(self, status=200, text='{"cartId":"CART-1","booking":{"id":9}}'):
            self.status, self.text = status, text
            self.calls = []

        def fetch_encrypted_json(self, path, *, body, headers=None, timeout_ms=30_000):
            self.calls.append({"path": path, "body": body, "headers": headers or {}})
            return {"status": self.status, "ok": True, "text": self.text, "headers": {}}

        def fetch_gql(self, *a, **k):
            raise AssertionError("占房不该再走 GraphQL —— addNewBooking 已被 403")

    def test_posts_to_api_booking_and_returns_cart_id(self):
        import booker
        f = self._F()
        assert booker.create_booking(f, "tok", "r-x-1", "2026-09-03") == "CART-1"
        assert f.calls[0]["path"] == "/api/booking"

    def test_payload_matches_the_site(self):
        import json
        import booker
        f = self._F()
        booker.create_booking(f, "tok", "r-x-1", "2026-09-03")
        body = json.loads(f.calls[0]["body"])
        assert body["sku"] == "r-x-1"
        # 日期必须是 DD-MM-YYYY（站点原文 getDate-getMonth+1-getFullYear）
        assert body["contract_startDate"] == "03-09-2026", (
            "日期格式不是 DD-MM-YYYY —— 站点就是这么拼的"
        )
        assert "challengeToken" in body and "challengeProvider" in body

    def test_sends_bearer(self):
        import booker
        f = self._F()
        booker.create_booking(f, "the-jwt", "r-x-1", None)
        assert f.calls[0]["headers"].get("Authorization") == "Bearer the-jwt"

    def test_401_is_auth_error(self):
        import pytest as _p
        import booker
        f = self._F(status=401, text='{"error":"Unauthorized"}')
        with _p.raises(booker.AuthError):
            booker.create_booking(f, "stale", "r-x-1", None)

    def test_missing_cart_id_raises(self):
        """没 cartId = 没占到房。绝不能当成功往下走 placeOrder。"""
        import pytest as _p
        import booker
        f = self._F(text='{"ok":true}')
        with _p.raises(RuntimeError, match="cartId"):
            booker.create_booking(f, "tok", "r-x-1", None)

    def test_missing_booking_field_raises(self):
        """站点前端也检查 booking 字段——车建了但没占上房，同样是失败。"""
        import pytest as _p
        import booker
        f = self._F(text='{"cartId":"CART-1"}')
        with _p.raises(RuntimeError):
            booker.create_booking(f, "tok", "r-x-1", None)

    def test_do_book_uses_create_booking_not_add_to_cart(self):
        """回归：_do_book 里不能再出现 create_empty_cart / add_to_cart 的调用。"""
        import ast
        import pathlib
        src = pathlib.Path("booker.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_do_book":
                called = {
                    n.func.id for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                assert "create_booking" in called, "_do_book 没走新的占房入口"
                assert "add_to_cart" not in called, (
                    "_do_book 还在调 add_to_cart —— addNewBooking 已被 403"
                )
                assert "create_empty_cart" not in called, (
                    "/api/booking 已经代建车，不该再单独建一次"
                )
                return
        raise AssertionError("找不到 _do_book，这条守卫已失效")
