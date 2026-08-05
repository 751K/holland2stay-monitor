"""被 CF 挡住时要换出口 IP，而不是在同一个 IP 上重试三次。

2026-08-03 生产事故的完整链条：出口 IP 被 CF 盯上 → 挑战连续 3 次 90s 全部
超时 → source 熔断退避 30 分钟。三次尝试跑在同一个浏览器上，也就是同一个
出口 IP——等于把同一次失败重复了三遍，唯一的效果是多烧了 4 分半钟。

``rotating_proxy`` 那次修的是「新建浏览器时换 IP」，但没人在失败时新建浏览器：
``ensure_initialized`` 原地重导航，``fetch`` 的 403 分支也只是把
``_initialized`` 置 False。换 IP 的能力有了，触发它的路径没有。

这里盯的就是那条路径。
"""
from __future__ import annotations

from types import MethodType

import pytest

import browser_fetcher
from browser_fetcher import H2S_PROFILE, SiteProfile, BrowserFetcher
from scrapers.base import BlockedError


class _StubBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def new_page(self):
        return object()


def _stub_launch(fetcher: BrowserFetcher) -> list[str]:
    """把 ``_launch`` 换成计数器，返回记录每次拿到的代理串的列表。"""
    proxies: list[str] = []
    counter = {"n": 0}

    def _launch(self):
        counter["n"] += 1
        self._proxy_url = f"http://user:pw@exit{counter['n']}.example:80"
        self._browser = _StubBrowser()
        self._page = object()
        proxies.append(self._proxy_url)

    fetcher._launch = MethodType(_launch, fetcher)
    return proxies


class TestRebuildRotatesExit:
    def test_rebuild_closes_old_and_launches_new(self):
        f = BrowserFetcher(profile=H2S_PROFILE)
        proxies = _stub_launch(f)
        f._launch()
        old_browser = f._browser

        assert f._rebuild_browser() is True

        assert old_browser.closed, "旧浏览器没关，出口 IP 和内存都留着"
        assert len(proxies) == 2
        assert proxies[0] != proxies[1], "重建后还是同一个出口，等于没换"

    def test_sticky_profile_does_not_rebuild(self):
        """固定 IP 的 profile 重建后拿到的是同一个出口，白付一次冷启动。"""
        sticky = SiteProfile(
            name="Sticky", source="sticky",
            challenge_url="https://sticky.example/",
            rotating_proxy=False,
        )
        f = BrowserFetcher(profile=sticky)
        proxies = _stub_launch(f)
        f._launch()

        assert f._rebuild_browser() is False
        assert len(proxies) == 1

    def test_rebuild_without_browser_is_a_noop(self):
        """没 __enter__ 就调用时，凭空 launch 一个不叫「重建」。"""
        f = BrowserFetcher(profile=H2S_PROFILE)
        proxies = _stub_launch(f)

        assert f._rebuild_browser() is False
        assert proxies == []


class TestInitRetriesOnFreshExit:
    def test_each_failed_attempt_gets_a_new_exit(self, monkeypatch):
        f = BrowserFetcher(profile=H2S_PROFILE)
        proxies = _stub_launch(f)
        f._launch()

        seen: list[str] = []

        def _always_blocked(self, attempt):
            seen.append(self._proxy_url)
            raise BlockedError("CF 挑战 90s 内未解开")

        f._navigate_and_verify = MethodType(_always_blocked, f)

        with pytest.raises(BlockedError):
            f.ensure_initialized()

        assert len(seen) == 3, "重试次数变了"
        assert len(set(seen)) == 3, f"三次尝试跑在同一个出口 IP 上: {seen}"

    def test_no_rebuild_after_the_last_attempt(self):
        """最后一次失败后就要抛错了，再建一个浏览器纯属浪费。"""
        f = BrowserFetcher(profile=H2S_PROFILE)
        proxies = _stub_launch(f)
        f._launch()
        f._navigate_and_verify = MethodType(
            lambda self, attempt: (_ for _ in ()).throw(BlockedError("挡住了")), f
        )

        with pytest.raises(BlockedError):
            f.ensure_initialized()

        # 首次 launch + 前两次失败各重建一次 = 3
        assert len(proxies) == 3

    def test_success_on_second_exit_is_not_reported_as_failure(self):
        f = BrowserFetcher(profile=H2S_PROFILE)
        _stub_launch(f)
        f._launch()
        calls = {"n": 0}

        def _navigate(self, attempt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BlockedError("第一个 IP 过不去")
            return (1.0, 1.0)

        f._navigate_and_verify = MethodType(_navigate, f)

        f.ensure_initialized()
        assert f.is_initialized


class TestFetch403RotatesExit:
    def test_403_rebuilds_browser_before_re_challenging(self):
        """403 不是 clearance 过期时，原地重跑挑战换不掉被挡的那个 IP。"""
        f = BrowserFetcher(profile=H2S_PROFILE)
        _stub_launch(f)
        f._launch()
        f._initialized = True

        rebuilds = {"n": 0}
        real_rebuild = f._rebuild_browser

        def _counting_rebuild(self):
            rebuilds["n"] += 1
            return real_rebuild()

        f._rebuild_browser = MethodType(_counting_rebuild, f)
        f.ensure_initialized = MethodType(
            lambda self: setattr(self, "_initialized", True), f
        )

        responses = [
            {"status": 403, "ok": False, "text": "Forbidden", "headers": {}},
            {"status": 200, "ok": True, "text": "{}", "headers": {}},
        ]
        f._raw_fetch = MethodType(
            lambda self, *a, **kw: responses.pop(0), f
        )

        result = f.fetch("/api/graphql")

        assert result["status"] == 200
        assert rebuilds["n"] == 1, "403 之后没换 IP"

    def test_clearance_expiry_does_not_rotate(self):
        """clearance 过期是瞬时的，重新导航就好；换 IP 反而丢掉已有会话。"""
        import json

        f = BrowserFetcher(profile=H2S_PROFILE)
        _stub_launch(f)
        f._launch()
        f._initialized = True

        rebuilds = {"n": 0}
        f._rebuild_browser = MethodType(
            lambda self: rebuilds.__setitem__("n", rebuilds["n"] + 1) or False, f
        )
        f.ensure_initialized = MethodType(
            lambda self: setattr(self, "_initialized", True), f
        )

        responses = [
            {
                "status": 403, "ok": False,
                "text": json.dumps({"code": "clearance_required"}),
                "headers": {},
            },
            {"status": 200, "ok": True, "text": "{}", "headers": {}},
        ]
        f._raw_fetch = MethodType(lambda self, *a, **kw: responses.pop(0), f)

        f.fetch("/api/graphql")
        assert rebuilds["n"] == 0, "clearance 过期被当成了 IP 被封"
