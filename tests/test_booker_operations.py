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

    @pytest.mark.parametrize("call,resp", [
        (lambda f: booker.set_payment_method(f, "tok", "c1"),
         {"data": {"setPaymentMethodOnCart": {"cart": {
             "selected_payment_method": {"code": "idealcheckout_ideal"}}}}}),
        (lambda f: booker.place_order(f, "tok", "c1"),
         {"data": {"placeOrder": {"orderV2": {"order_number": "H2S-1"}}}}),
        (lambda f: booker._ideal_checkout(f, "tok", "H2S-1"),
         {"data": {"idealCheckOut": {"redirect": "https://pay"}}}),
    ])
    def test_empty_operation_name_is_a_bug(self, call, resp):
        """没有哪一步该发空 operationName——发了就是 403。"""
        f = _CapturingFetcher(resp)
        call(f)
        assert f.calls[0]["operation_name"], "operation_name 为空 = 必然 403"


class TestNoUnwiredBookingFallback:
    """占房只有一条路径，不留「保留着以防万一」的死函数。

    ``create_empty_cart`` / ``add_to_cart`` 曾以「addNewBooking 是否真被后端
    拒绝还没验证，先留着」的名义保留。但没有任何代码路径会调它们——留着但
    没接线的 fallback 只是错觉，而这个错觉正是 ``BookingBlockedError`` 那个
    bug 藏了三个月的机制（测试钉着死代码，让它看起来是活的）。

    2026-08-20 删除。要找原文去 git 历史，或看 ``h2s_booking_gql``——那份是
    **站点报文的照抄记录**，不是我们的调用清单，两个 operation 仍留在里面。
    """

    def test_dead_cart_helpers_are_gone(self):
        for name in ("create_empty_cart", "add_to_cart"):
            assert not hasattr(booker, name), (
                f"booker.{name} 又回来了——它没有任何调用者，"
                "留着只会让「有 fallback」的错觉重演"
            )

    def test_transcript_module_still_keeps_the_operations(self):
        """照抄记录不删：它记的是站点发过什么，与我们调不调无关。"""
        assert gql.ADDNEWBOOKING and gql.OP_ADDNEWBOOKING == "AddNewBooking"
        assert gql.CREATEEMPTYCART and gql.OP_CREATEEMPTYCART == "CreateEmptyCart"


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

    @pytest.fixture
    def ours(self, monkeypatch):
        """把 r-x-1 标成「我们下的」——否则现在一笔都不会取消，见下面那组用例。"""
        import booker
        monkeypatch.setattr(booker, "_our_reservations", lambda _email: {"r-x-1"})

    def test_list_and_cancel_both_go_through_fetch_rest(self, ours):
        import booker
        f = self._Fetcher()
        n = booker.cancel_pending_orders(f, "tok", "a@b.c")
        assert n == 1
        assert f.plain_calls == []
        paths = [p for p, _ in f.rest_calls]
        assert any("newdashboard/contract/me" in p for p in paths), "没查预留列表"
        assert any("bookingcancel/r-x-1" in p for p in paths), "没按 SKU 取消"

    def test_cancel_is_post_and_list_is_get(self, ours):
        import booker
        f = self._Fetcher()
        booker.cancel_pending_orders(f, "tok", "a@b.c")
        methods = dict((p.split("?")[0], m) for p, m in f.rest_calls)
        assert methods["/api/rest/V1/newdashboard/contract/me"] == "GET"
        assert methods["/api/rest/V1/customer/bookingcancel/r-x-1"] == "POST"


class TestCancelOnlyTouchesOurOwnReservations:
    """**绝不能取消用户自己手动订的房。**

    原实现只按 ``status in _CANCEL_STATUSES`` 过滤，会把账号下所有待处理预留一起
    取消——为了抢另一套，把用户看中的那套删掉，而且不可逆。
    """

    def _fetcher(self):
        return TestCancelUsesRestEnvelope._Fetcher()

    def test_unknown_reservation_is_not_cancelled(self, monkeypatch):
        """没有记录 = 不知道是不是我们的 = 一笔都不动（fail-safe）。"""
        import booker
        monkeypatch.setattr(booker, "_our_reservations", lambda _e: set())
        f = self._fetcher()
        assert booker.cancel_pending_orders(f, "tok", "a@b.c") == 0
        assert not any("bookingcancel" in p for p, _ in f.rest_calls), (
            "取消了一笔不属于我们的预留")

    def test_skipping_is_reported(self, monkeypatch, caplog):
        """静默跳过会让人以为「取消坏了」，进而把判据放宽回原样。"""
        import logging

        import booker
        monkeypatch.setattr(booker, "_our_reservations", lambda _e: set())
        with caplog.at_level(logging.WARNING):
            booker.cancel_pending_orders(self._fetcher(), "tok", "a@b.c")
        assert "不是我们下的" in caplog.text

    def test_only_ours_among_several(self, monkeypatch):
        """混合场景：只动记过的那一笔。"""
        import json as _j

        import booker

        class _F(TestCancelUsesRestEnvelope._Fetcher):
            def fetch_rest(self, path, *, method="GET", body="", headers=None,
                           timeout_ms=30_000):
                self.rest_calls.append((path, method))
                if method == "GET":
                    return {"status": 200, "ok": True, "headers": {},
                            "text": _j.dumps({"items": [
                                {"sku": "ours", "product_name": "A",
                                 "status": "reserved"},
                                {"sku": "manual", "product_name": "B",
                                 "status": "reserved"},
                            ]})}
                return {"status": 200, "ok": True, "text": "{}", "headers": {}}

        monkeypatch.setattr(booker, "_our_reservations", lambda _e: {"ours"})
        f = _F()
        assert booker.cancel_pending_orders(f, "tok", "a@b.c") == 1
        cancelled = [p for p, _ in f.rest_calls if "bookingcancel" in p]
        assert any("ours" in p for p in cancelled)
        assert not any("manual" in p for p in cancelled), "动了用户手动订的那笔"

    def test_recording_round_trips(self, tmp_path, monkeypatch):
        """记录要能存下来并读回——否则重启后就再也认不出自己的预留。"""
        import booker
        from storage import Storage

        st = Storage(tmp_path / "t.db")
        monkeypatch.setattr(booker, "_meta_storage", lambda: st)
        booker.remember_our_reservation("a@b.c", "sku-1")
        booker.remember_our_reservation("a@b.c", "sku-2")
        assert booker._our_reservations("a@b.c") == {"sku-1", "sku-2"}
        # 换个账号读不到
        assert booker._our_reservations("other@b.c") == set()


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
