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
