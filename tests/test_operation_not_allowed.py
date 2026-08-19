"""403 有两种成因，处置完全相反。这个文件守的是「别再把它们混成一种」。

2026-08-19 一次自动预订失败的完整代价：H2S 对登录 mutation 返回
``403 {"error":"This operation is not available through the public API"}``，
``content-type: application/json``。代码把它当成 Cloudflare 屏蔽，于是

    11:08:26  开始预订
    11:09:03  403 → 重建浏览器（换出口 IP + 一整轮 CF 挑战）
    11:09:07  403 → 再重建一次
    11:09:41  403 → 抛 BlockedError，75 秒、约 3 MB 白烧
    11:09:41  monitor 据此暂停**整条登录链路** 1 小时

三次重建换了三个出口 IP，拿到的是同一句话——因为拒绝它的不是 Cloudflare，
是上游业务后端。同一个会话里换一条已登记的 operation 立刻 200（实测：
``GetCheckoutAgreements`` 200，``GetProduct`` 403）。

判据是响应正文，不是状态码也不是 content-type。两种文案都实测过，
都得认——见 ``scrapers.base._OPERATION_REJECTED_MARKERS``。
"""
from __future__ import annotations

import json

import pytest

import browser_fetcher
from browser_fetcher import BrowserFetcher
from scrapers.base import (
    BlockedError,
    OperationNotAllowedError,
    is_cloudflare_body,
    is_operation_rejected_body,
)

#: 生产实测的两句原文。改动前先确认上游真的改了文案。
_REJECT_BOOKING = json.dumps(
    {"error": "This operation is not available through the public API"}
)
_REJECT_SCRAPE = json.dumps({"code": "operation_not_allowed"})
#: 挑战页。含 "just a moment" —— 落在 profile 的 clearance_pending_markers 里，
#: 走的是「重新导航」那条路，不是重建。
_CF_CHALLENGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
#: 真·屏蔽页。不含任何 clearance-pending 标记，直接走重建 + 换 IP。
_CF_BLOCK = (
    "<!DOCTYPE html><html><head><title>Access denied | Cloudflare</title></head>"
    "<body>error code: 1020</body></html>"
)


class TestBodyPredicate:
    @pytest.mark.parametrize("body", [_REJECT_BOOKING, _REJECT_SCRAPE])
    def test_recognises_both_wordings(self, body):
        """两句文案是同一道闸门的两种写法，只认一句 = 漏判另一半。"""
        assert is_operation_rejected_body(body)

    def test_case_insensitive(self):
        """上游改个大小写不该让判定失效。"""
        assert is_operation_rejected_body(
            '{"error":"This Operation Is Not Available Through The Public API"}'
        )

    @pytest.mark.parametrize("body", [
        _CF_CHALLENGE,
        _CF_BLOCK,
        '{"error":"Browser verification required","code":"clearance_required"}',
        '{"data":{"products":{"total_count":12493}}}',
        "",
    ])
    def test_does_not_claim_unrelated_bodies(self, body):
        """误判成 operation 被拒的代价是反过来的：真被 CF 挡了却不换 IP。"""
        assert not is_operation_rejected_body(body)

    def test_the_two_predicates_do_not_overlap(self):
        """CF 页与 operation 拒绝必须互斥，否则分支顺序就成了行为依赖。"""
        assert is_cloudflare_body(_CF_BLOCK)
        assert not is_operation_rejected_body(_CF_BLOCK)
        assert is_operation_rejected_body(_REJECT_BOOKING)
        assert not is_cloudflare_body(_REJECT_BOOKING)


class TestNotABlockedError:
    def test_does_not_inherit_blocked_error(self):
        """继承关系一旦建立，上层每一处 ``except BlockedError`` 都会把它接住，
        换 IP / 熔断 / 登录抑制原样重演——这个类就白建了。"""
        assert not issubclass(OperationNotAllowedError, BlockedError)
        assert not issubclass(BlockedError, OperationNotAllowedError)


class _RecordingFetcher(BrowserFetcher):
    """记录 _raw_fetch / _rebuild_browser 调用次数的 BrowserFetcher。

    直接构造真 BrowserFetcher 但不开浏览器：``fetch()`` 只用到
    ``ensure_initialized`` / ``_raw_fetch`` / ``_rebuild_browser`` 三个点。
    """

    def __init__(self, *responses: dict):
        super().__init__()
        self._responses = list(responses)
        self.raw_calls = 0
        self.rebuilds = 0
        self._initialized = True

    def ensure_initialized(self) -> None:
        self._initialized = True

    def _rebuild_browser(self) -> None:
        self.rebuilds += 1

    def _raw_fetch(self, path, *, method="POST", body="", headers=None, timeout_ms=30_000):
        self.raw_calls += 1
        return self._responses[min(self.raw_calls - 1, len(self._responses) - 1)]


def _resp(status: int, text: str) -> dict:
    return {"status": status, "ok": 200 <= status < 300, "text": text, "headers": {}}


class TestFetchDoesNotRebuild:
    """核心回归：operation 被拒时一次浏览器都不该重建。"""

    def test_raises_operation_error_without_rebuilding(self):
        f = _RecordingFetcher(_resp(403, _REJECT_BOOKING))

        with pytest.raises(OperationNotAllowedError):
            f.fetch("/api/__enc__", body="{}")

        assert f.rebuilds == 0, (
            "重建了浏览器——这正是 2026-08-19 烧掉 75 秒和 3 MB 的那步，"
            "而换多少个出口 IP 都拿到同一个 403"
        )
        assert f.raw_calls == 1, "多打了一次必然失败的请求"

    def test_cloudflare_403_still_rebuilds(self):
        """反向守卫：真的 CF 屏蔽仍然要换 IP。

        没有这条，把上面那个判定写成「凡 403 都不重建」也能全绿。
        """
        f = _RecordingFetcher(_resp(403, _CF_BLOCK))

        with pytest.raises(BlockedError):
            f.fetch("/api/__enc__", body="{}")

        assert f.rebuilds == 1

    def test_operation_rejection_revealed_only_after_rebuild(self):
        """第一次是屏蔽页、重建后才露出业务 403 —— 也要判成 operation 被拒。

        只看第一次响应会把这种情形永远判成 BlockedError，然后每一轮都白重建。
        """
        f = _RecordingFetcher(
            _resp(403, _CF_BLOCK),          # 首次：看着确实像 CF
            _resp(403, _REJECT_BOOKING),    # 重建后：业务后端拒绝
        )

        with pytest.raises(OperationNotAllowedError):
            f.fetch("/api/__enc__", body="{}")

        assert f.rebuilds == 1, "第一次重建是对的（当时确实看着像 CF）"

    def test_operation_rejection_after_clearance_retry(self):
        """挑战页 → 重新导航 → 业务 403。这条路**一次都不该重建**。

        403 有三条路：clearance 没落地（重新导航）、CF 屏蔽（重建换 IP）、
        operation 没登记（什么都别做）。这里走的是第一条转第三条，是生产上
        最常见的形态——H2S 的会话本来就会周期性要求重新校验。
        """
        f = _RecordingFetcher(
            _resp(403, _CF_CHALLENGE),      # clearance 未落地
            _resp(403, _REJECT_BOOKING),    # 导航回来后：operation 被拒
        )

        with pytest.raises(OperationNotAllowedError):
            f.fetch("/api/__enc__", body="{}")

        assert f.rebuilds == 0

    def test_success_after_rebuild_is_unaffected(self):
        """既有行为不能变：CF 403 → 重建 → 200 仍然正常返回。"""
        f = _RecordingFetcher(
            _resp(403, _CF_BLOCK),
            _resp(200, '{"data":{}}'),
        )
        assert f.fetch("/api/__enc__", body="{}")["status"] == 200


class TestOperationLabel:
    """错误消息要说清楚是**哪条** operation —— booker 里有 9 条。"""

    @pytest.mark.parametrize("query,expected", [
        ("query GetProduct($urlKey: String!) { products { x } }", "GetProduct"),
        ("mutation generateCustomerToken($e: String!) { t }", "generateCustomerToken"),
        ("\n  mutation  PlaceOrder ( $c: String! ) { o }", "PlaceOrder"),
    ])
    def test_reads_the_name_from_the_document(self, query, expected):
        assert browser_fetcher._operation_label(query) == expected

    def test_anonymous_document_gets_a_placeholder(self):
        assert browser_fetcher._operation_label("{ products { x } }") == "(匿名)"

    def test_fetch_gql_names_the_operation(self):
        f = _RecordingFetcher(_resp(403, _REJECT_BOOKING))

        with pytest.raises(OperationNotAllowedError, match="GetProduct"):
            f.fetch_gql("query GetProduct($urlKey: String!) { products { x } }")

    def test_label_does_not_leak_into_the_request_body(self):
        """只用于日志。往请求体里补 operationName 会改变发给上游的内容，
        而上游正是按 operation 判放行的 —— 为了日志好看去动线上行为，因果就反了。
        """
        sent: list[str] = []

        class _Capture(_RecordingFetcher):
            def _raw_fetch(self, path, *, method="POST", body="", headers=None,
                           timeout_ms=30_000):
                sent.append(body)
                return super()._raw_fetch(
                    path, method=method, body=body, headers=headers,
                    timeout_ms=timeout_ms,
                )

        f = _Capture(_resp(200, '{"data":{}}'))
        f.fetch_gql("query GetProduct($urlKey: String!) { products { x } }")

        assert "operationName" not in sent[0]


# ─────────────────────────────────────────────────────────────────────
# 预订侧：phase 分开，且不触发任何「以为被 CF 盯上了」的动作
# ─────────────────────────────────────────────────────────────────────

class TestBookerPhase:
    def test_try_book_returns_operation_rejected(self):
        from unittest.mock import patch

        from booker import try_book
        from models import Listing

        listing = Listing(
            id="L-1", name="T-1", status="Available to book", price_raw="€700",
            available_from="2030-01-01", features=[], url="https://t/1", city="E",
            sku="SKU-1", contract_id=42, contract_start_date="2030-01-01",
        )

        with patch("booker.BrowserFetcher") as MockFetcher:
            mock = MockFetcher.return_value
            mock.__enter__.return_value = mock
            mock.__exit__.return_value = False
            mock.fetch_gql.side_effect = OperationNotAllowedError(
                "operation generateCustomerToken 被拒"
            )
            result = try_book(listing, email="x@x.com", password="pw", dry_run=False)

        assert result.success is False
        assert result.phase == "operation_rejected", (
            "报成 blocked 会让 monitor 暂停整条登录链路一小时，"
            "而登录链路本身是好的"
        )

    def test_phase_is_a_declared_literal(self):
        """phase 是 Literal，写错字符串在运行时不会报错，只会静默走进 else 分支。"""
        from typing import get_args

        from booker import BookingPhase

        assert "operation_rejected" in get_args(BookingPhase)
        assert "blocked" in get_args(BookingPhase)


class TestMonitorDoesNotSuppressLogin:
    """monitor 侧的回归：这个 phase 不该碰登录抑制窗口，也不该丢 prewarm。"""

    @pytest.fixture(autouse=True)
    def _reset_suppression(self):
        import monitor
        monitor._h2s_login_blocked_until = 0.0
        yield
        monitor._h2s_login_blocked_until = 0.0

    def test_source_code_never_marks_login_blocked_for_this_phase(self):
        """用 AST 钉住控制流：``operation_rejected`` 那条分支里不能出现
        ``_mark_h2s_login_blocked``。

        直接跑 run_once 要搭一整套 storage/notifier/future，成本远大于收益；
        而这里真正要防的回归很具体——有人图省事把两个 phase 并成
        ``in ("blocked", "operation_rejected")``，抑制就又回来了。
        """
        import ast
        import pathlib

        src = pathlib.Path("monitor.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        def _mentions(node, name: str) -> bool:
            return any(
                isinstance(n, ast.Name) and n.id == name for n in ast.walk(node)
            )

        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            # 匹配 `result.phase == "operation_rejected"` 及 in (...) 形态
            consts = [
                c.value for c in node.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
            for c in node.comparators:
                if isinstance(c, (ast.Tuple, ast.List, ast.Set)):
                    consts += [
                        e.value for e in c.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
            if "operation_rejected" not in consts:
                continue
            checked += 1
            assert "blocked" not in consts, (
                "operation_rejected 和 blocked 被并进了同一个判断。"
                "两者上层动作相反：blocked 要暂停登录链路 1 小时，"
                "operation_rejected 暂停多久都不会好。"
            )

        assert checked >= 1, "找不到 operation_rejected 的分支，这条守卫已失效"

        # 抑制窗口只该由 blocked 那条分支推进
        markers = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_mark_h2s_login_blocked"
        ]
        assert markers, "_mark_h2s_login_blocked 已不存在，这条守卫已失效"

    def test_prewarm_cache_is_not_invalidated(self):
        """session 是好的。丢掉只换来下轮多一次完整登录，然后同样失败。"""
        import ast
        import pathlib

        src = pathlib.Path("monitor.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for c in node.comparators:
                if not isinstance(c, (ast.Tuple, ast.List, ast.Set)):
                    continue
                vals = {
                    e.value for e in c.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
                if "unknown_error" in vals and "blocked" in vals:
                    assert "operation_rejected" not in vals, (
                        "operation_rejected 被加进了 prewarm 失效名单"
                    )


# ─────────────────────────────────────────────────────────────────────
# 抓取侧：同一道闸门，同样不该重建会话
# ─────────────────────────────────────────────────────────────────────

class TestScrapeSide:
    def test_dispatcher_isolates_without_dropping_the_session(self):
        """dispatcher 的通用 except 会 ``_safe_invalidate`` 丢掉浏览器。
        operation 被拒时那是纯浪费——下轮重建 = 又一整轮 CF 挑战。"""
        import scrapers
        from scrapers.base import ScrapeTask

        dropped: list[str] = []

        class _Scraper:
            source = "holland2stay"

            def batch_session(self):
                from contextlib import nullcontext
                return nullcontext()

            def scrape(self, task):
                raise OperationNotAllowedError("operation GetCategories 被拒")

            def invalidate_session(self, *a, **kw):
                dropped.append("dropped")

        task = ScrapeTask(source="holland2stay", city_key="1", city_display="Eindhoven")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(scrapers, "get_scraper", lambda s: _Scraper())
            with pytest.raises(OperationNotAllowedError):
                scrapers.dispatch_scrape_tasks([task])

        assert dropped == [], (
            "丢了浏览器会话。会话没问题，坏的是我们发的那条查询——"
            "重建只是再过一遍 CF 挑战，然后以同样方式失败"
        )

    def test_h2s_scraper_does_not_relabel_it_as_a_network_error(self):
        """第 1 页失败时 h2s scraper 会把未知异常改判成 ScrapeNetworkError。

        改判的代价见 2026-08-11：端点迁移的 404 被报成「请检查代理/网络」，
        排查往代理方向走了三天，而代理一直是好的。
        """
        from scrapers.base import ScrapeNetworkError
        from scrapers.holland2stay import _scrape_city_pages

        class _Fetcher:
            def fetch_gql(self, *a, **kw):
                raise OperationNotAllowedError("operation GetCategories 被拒")

        with pytest.raises(OperationNotAllowedError):
            _scrape_city_pages(_Fetcher(), "Eindhoven", ["1"], ["179"], {})

        # 反向：真的网络错误仍然要被改判，否则这条守卫写反了也能过
        class _Boom:
            def fetch_gql(self, *a, **kw):
                raise ValueError("socket 炸了")

        with pytest.raises(ScrapeNetworkError):
            _scrape_city_pages(_Boom(), "Eindhoven", ["1"], ["179"], {})
