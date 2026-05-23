"""
OurDomain TLS 指纹智能轮换 + 同 session 内 403 重试测试。

背景
----
SecureRC (Cloudflare) 做 per-fingerprint 跟踪。旧实现每次都从配置顺序
第一个开始试，导致 chrome131 / chrome124 反复被烧。新实现：
1. 进程级状态：成功用过的标 last_good_at，失败的标 cooldown_until
2. 排序：last_good → fresh → cooldown
3. 同 session 内 403 先在原 session 重试一次（吃 cf_clearance cookie）

契约
----
- 上一轮成功的指纹下一轮排首位
- cooldown 中的指纹排到队尾
- 全部冷却时不抛异常，按原序兜底用
- _get_text 收 CF 403 时同 session 重试一次再放弃
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import scrapers.ourdomain as od


def setup_function(func):
    od._FINGERPRINT_STATE.clear()


def teardown_function(func):
    od._FINGERPRINT_STATE.clear()


def _reset_state():
    od._FINGERPRINT_STATE.clear()


# ─── 指纹状态管理 ────────────────────────────────────────────────


class TestFingerprintState:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_initial_state_empty(self):
        with patch.dict(
            "os.environ",
            {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124,safari17_0"},
            clear=False,
        ):
            order = od._impersonate_attempts()
        assert order == ["chrome131", "chrome124", "safari17_0"]

    def test_last_good_moves_to_front(self):
        """成功用过的指纹下次排第一。"""
        od._mark_fingerprint_good("safari17_0")
        with patch.dict(
            "os.environ",
            {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124,safari17_0,edge101"},
            clear=False,
        ):
            order = od._impersonate_attempts()
        assert order[0] == "safari17_0", f"last_good 应该排首位，实际 {order}"

    def test_cooldown_pushes_to_back(self):
        """403 失败的指纹排到尾部。"""
        od._mark_fingerprint_blocked("chrome131")
        with patch.dict(
            "os.environ",
            {
                "OURDOMAIN_IMPERSONATES": "chrome131,chrome124,safari17_0",
                "OURDOMAIN_WAF_RETRIES": "3",
            },
            clear=False,
        ):
            order = od._impersonate_attempts()
        assert order[-1] == "chrome131", f"cooldown 应该排末位，实际 {order}"
        assert order[0] in ("chrome124", "safari17_0")

    def test_most_recent_good_wins_when_multiple(self):
        """两个都成功过时，更新的（last_good_at 更大）排前。"""
        od._mark_fingerprint_good("chrome131")
        time.sleep(0.01)
        od._mark_fingerprint_good("safari17_0")
        with patch.dict(
            "os.environ",
            {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124,safari17_0"},
            clear=False,
        ):
            order = od._impersonate_attempts()
        assert order[0] == "safari17_0"
        assert order[1] == "chrome131"

    def test_all_cooldown_still_returns_list(self):
        """全部冷却时仍然返回（按原顺序兜底）。"""
        od._mark_fingerprint_blocked("chrome131")
        od._mark_fingerprint_blocked("chrome124")
        with patch.dict(
            "os.environ",
            {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124", "OURDOMAIN_WAF_RETRIES": "2"},
            clear=False,
        ):
            order = od._impersonate_attempts()
        assert len(order) == 2
        assert set(order) == {"chrome131", "chrome124"}

    def test_good_marks_clear_cooldown(self):
        """成功之后冷却被清掉，下次还能正常排前。"""
        od._mark_fingerprint_blocked("chrome131")
        assert od._is_in_cooldown("chrome131") is True
        od._mark_fingerprint_good("chrome131")
        assert od._is_in_cooldown("chrome131") is False


# ─── 同 session 内 403 重试 ──────────────────────────────────────


def _fake_resp(status, body="", ok=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.ok = (200 <= status < 300) if ok is None else ok

    def raise_for_status():
        if not resp.ok:
            raise Exception(f"HTTP {status}")
    resp.raise_for_status = raise_for_status
    return resp


CF_CHALLENGE = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
    '</head><body></body></html>'
)


class TestInSessionRetry:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_first_403_then_200_in_same_session_succeeds(self):
        """CF 403 → 同 session 重试 → 200。不抛 BlockedError。"""
        from scrapers.ourdomain import _get_text

        session = MagicMock()
        session.get.side_effect = [
            _fake_resp(403, CF_CHALLENGE),
            _fake_resp(200, "<html>real content</html>"),
        ]

        with patch("scrapers.ourdomain.time.sleep"):
            result = _get_text(session, "https://x/page")

        assert "real content" in result
        assert session.get.call_count == 2

    def test_two_consecutive_403_raises_blocked(self):
        """两次都 CF 403 → 放弃，抛 BlockedError 让上层切指纹。"""
        from scrapers.ourdomain import _get_text

        session = MagicMock()
        session.get.return_value = _fake_resp(403, CF_CHALLENGE)

        with patch("scrapers.ourdomain.time.sleep"):
            with pytest.raises(od.BlockedError):
                _get_text(session, "https://x/page")

        # 必须重试过一次（共 2 次 GET）
        assert session.get.call_count == 2

    def test_non_cf_403_does_not_retry(self):
        """非 Cloudflare 的硬 403 不重试（重试无意义）。"""
        from scrapers.ourdomain import _get_text

        session = MagicMock()
        # 简单 JSON 403，不是 CF 页面
        session.get.return_value = _fake_resp(403, '{"error":"forbidden"}')

        with patch("scrapers.ourdomain.time.sleep"):
            with pytest.raises(od.BlockedError):
                _get_text(session, "https://x/page")

        assert session.get.call_count == 1, "非 CF 403 不应该重试"

    def test_normal_200_unchanged(self):
        """回归：正常 200 没有副作用。"""
        from scrapers.ourdomain import _get_text

        session = MagicMock()
        session.get.return_value = _fake_resp(200, "<html>ok</html>")

        with patch("scrapers.ourdomain.time.sleep"):
            result = _get_text(session, "https://x/page")

        assert result == "<html>ok</html>"
        assert session.get.call_count == 1


# ─── scrape() 集成：成功 → mark good ─────────────────────────────


class TestScrapeMarksFingerprint:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_successful_scrape_marks_fingerprint_good(self):
        """完整 scrape() 成功后，使用的指纹被标 good。"""
        from scrapers.ourdomain import OurDomainScraper
        from scrapers.base import ScrapeTask

        scraper = OurDomainScraper()

        # 跑通：mock _scrape_once 直接返空结果
        with patch.object(
            scraper, "_scrape_once", return_value=({}, True, {}),
        ), patch.dict(
            "os.environ",
            {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124"},
            clear=False,
        ):
            scraper.scrape(ScrapeTask(
                source="ourdomain", city_key="diemen", city_display="Diemen",
            ))

        # chrome131 应该被标 good
        assert od._FINGERPRINT_STATE.get("chrome131", {}).get("last_good_at", 0) > 0

    def test_blocked_scrape_marks_fingerprint_cooldown(self):
        """单个指纹被 block 后，该指纹进入 cooldown，其余继续尝试。"""
        from scrapers.ourdomain import OurDomainScraper
        from scrapers.base import ScrapeTask

        scraper = OurDomainScraper()

        # 第一次（chrome131）抛 Blocked，第二次（chrome124）通过
        call_count = {"n": 0}

        def fake_once(**kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise od.BlockedError("cf challenge")
            return ({}, True, {})

        with patch.object(scraper, "_scrape_once", side_effect=fake_once), \
             patch.dict(
                 "os.environ",
                 {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124"},
                 clear=False,
             ):
            scraper.scrape(ScrapeTask(
                source="ourdomain", city_key="diemen", city_display="Diemen",
            ))

        # chrome131 应该在 cooldown 中
        assert od._is_in_cooldown("chrome131") is True
        # chrome124 应该被标 good
        assert od._FINGERPRINT_STATE.get("chrome124", {}).get("last_good_at", 0) > 0

    def test_subsequent_scrape_prefers_last_good(self):
        """第二次 scrape() 时，上次成功的指纹排在第一位。"""
        from scrapers.ourdomain import OurDomainScraper
        from scrapers.base import ScrapeTask

        scraper = OurDomainScraper()

        impersonates_used: list[str] = []

        def fake_once(*, impersonate, **kw):
            impersonates_used.append(impersonate)
            # chrome131 失败，chrome124 成功
            if impersonate == "chrome131":
                raise od.BlockedError("burned")
            return ({}, True, {})

        with patch.object(scraper, "_scrape_once", side_effect=fake_once), \
             patch.dict(
                 "os.environ",
                 {"OURDOMAIN_IMPERSONATES": "chrome131,chrome124"},
                 clear=False,
             ):
            # 第一次：chrome131 烧 → chrome124 成功
            scraper.scrape(ScrapeTask(
                source="ourdomain", city_key="diemen", city_display="Diemen",
            ))
            # 第二次：应该从 chrome124 起步
            scraper.scrape(ScrapeTask(
                source="ourdomain", city_key="diemen", city_display="Diemen",
            ))

        # 第二次的第一次尝试应该是 chrome124（不是 chrome131）
        assert impersonates_used[0] == "chrome131"
        assert impersonates_used[1] == "chrome124"
        # 关键：第二次的第一个尝试
        assert impersonates_used[2] == "chrome124", (
            f"第二次应该优先 chrome124，实际顺序: {impersonates_used}"
        )
