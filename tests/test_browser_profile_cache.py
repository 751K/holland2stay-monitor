"""复用磁盘上的 profile，让挑战载荷不必每次重下。

代理流量里 56.6% 花在 Cloudflare 挑战载荷上（2026-08-04：985MB 中 558MB），
成因是每次重建浏览器都是全新的空缓存。

第一版尝试只给 `--disk-cache-dir`，实测一个字节都没省：`launch()` + `new_page()`
走的是 incognito context，HTTP 缓存只在内存里，浏览器一关即弃，缓存目录里只
留下几个索引文件。必须换成 `launch_persistent_context()`。

本地实测（2026-08-05，H2S 首页）冷 profile 3.93MB、暖 profile 0.25MB，
143 个请求命中磁盘缓存，页面照常渲染。

这里盯三件事：
1. 真的走持久化那条路，且 profile 目录稳定复用；
2. 并发安全——同一个 profile 不能被两个 Chromium 同时打开；
3. 任何一环出问题都要能退回临时 profile，不能把抓取拖停。
"""
from __future__ import annotations

import pytest

import browser_fetcher
from browser_fetcher import H2S_PROFILE, XIOR_PROFILE, BrowserFetcher


class _FakePage:
    def route(self, *a, **kw):
        pass


class _FakeContext:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pages = [_FakePage()]
        self.cookies_cleared = False
        self.closed = False

    def clear_cookies(self):
        self.cookies_cleared = True

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


@pytest.fixture
def spy(monkeypatch):
    """替掉两个 launch 入口，记录各自被调用的参数。"""
    import cloakbrowser

    made = {"persistent": [], "ephemeral": []}

    def fake_persistent(user_data_dir, **kwargs):
        ctx = _FakeContext(user_data_dir=user_data_dir, **kwargs)
        made["persistent"].append(ctx)
        return ctx

    def fake_launch(**kwargs):
        b = _FakeBrowser(**kwargs)
        made["ephemeral"].append(b)
        return b

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context", fake_persistent)
    monkeypatch.setattr(cloakbrowser, "launch", fake_launch)
    monkeypatch.setattr(browser_fetcher.platform, "system", lambda: "Linux")
    monkeypatch.delenv("BROWSER_PERSIST_PROFILE", raising=False)
    return made


class TestUsesPersistentProfile:
    def test_persistent_is_the_default(self, spy):
        BrowserFetcher(profile=H2S_PROFILE).__enter__()

        assert len(spy["persistent"]) == 1
        assert not spy["ephemeral"], "退回了临时 profile，缓存复用不到"

    def test_profile_dir_is_per_source_and_stable(self, spy):
        f1 = BrowserFetcher(profile=H2S_PROFILE)
        f1.__enter__()
        first = spy["persistent"][0].kwargs["user_data_dir"]
        f1.close()

        f2 = BrowserFetcher(profile=H2S_PROFILE)
        f2.__enter__()
        second = spy["persistent"][1].kwargs["user_data_dir"]

        assert first == second, "换了目录就等于每次都是冷启动"
        assert "holland2stay" in first

    def test_sources_do_not_share_a_profile(self, spy):
        h = BrowserFetcher(profile=H2S_PROFILE)
        h.__enter__()
        x = BrowserFetcher(profile=XIOR_PROFILE)
        x.__enter__()

        dirs = [c.kwargs["user_data_dir"] for c in spy["persistent"]]
        assert dirs[0] != dirs[1]
        assert "xior" in dirs[1]

    def test_disk_cache_is_capped(self, spy):
        """生产 VPS 磁盘已用 82%，缓存不能无限长。"""
        BrowserFetcher(profile=H2S_PROFILE).__enter__()
        args = spy["persistent"][0].kwargs["args"]
        assert any(a.startswith("--disk-cache-size=") for a in args)

    def test_proxy_still_passed_explicitly(self, spy, monkeypatch):
        """走持久化之后，代理照样得显式传——Chromium 不认环境变量。"""
        import config

        monkeypatch.setattr(
            config, "get_proxy_url", lambda source="", **kw: "http://u:p@px:80"
        )
        BrowserFetcher(profile=H2S_PROFILE).__enter__()
        assert spy["persistent"][0].kwargs["proxy"] == "http://u:p@px:80"

    def test_reuses_the_context_existing_page(self, spy):
        """持久化 context 自带一个页面，再 new_page 等于白占一份内存。"""
        f = BrowserFetcher(profile=H2S_PROFILE)
        f.__enter__()
        assert f._page is spy["persistent"][0].pages[0]


class TestCookiesAreNotCarriedOver:
    def test_cookies_cleared_on_open(self, spy):
        """clearance 绑出口 IP，而 rotating_proxy 意味着下次多半换了 IP。

        带着上一个 IP 的 cf_clearance 去请求，CF 只会当作可疑并重新挑战——
        要复用的只是磁盘缓存里那些静态资源。
        """
        BrowserFetcher(profile=H2S_PROFILE).__enter__()
        assert spy["persistent"][0].cookies_cleared

    def test_clear_cookies_failure_does_not_abort(self, spy, monkeypatch):
        import cloakbrowser

        def boom_ctx(user_data_dir, **kwargs):
            ctx = _FakeContext(user_data_dir=user_data_dir, **kwargs)
            ctx.clear_cookies = lambda: (_ for _ in ()).throw(RuntimeError("清不掉"))
            spy["persistent"].append(ctx)
            return ctx

        monkeypatch.setattr(cloakbrowser, "launch_persistent_context", boom_ctx)
        f = BrowserFetcher(profile=H2S_PROFILE)
        f.__enter__()
        assert f._page is not None


class TestSlotLocking:
    def test_second_fetcher_gets_a_different_slot(self, spy):
        """H2S 的 scraper 与 booker 同用一个 source、跑在不同线程上。
        一个 profile 目录只能被一个 Chromium 打开，必须分到不同槽位。"""
        a = BrowserFetcher(profile=H2S_PROFILE)
        a.__enter__()
        b = BrowserFetcher(profile=H2S_PROFILE)
        b.__enter__()

        dirs = [c.kwargs["user_data_dir"] for c in spy["persistent"]]
        assert dirs[0] != dirs[1], "两个实例拿到同一个 profile，Chromium 会锁冲突"

    def test_slot_is_released_on_close(self, spy):
        a = BrowserFetcher(profile=H2S_PROFILE)
        a.__enter__()
        first = spy["persistent"][0].kwargs["user_data_dir"]
        a.close()

        b = BrowserFetcher(profile=H2S_PROFILE)
        b.__enter__()
        assert spy["persistent"][1].kwargs["user_data_dir"] == first, \
            "槽位没释放，下一个实例被迫换目录，缓存也就白暖了"

    def test_falls_back_to_ephemeral_when_all_slots_taken(self, spy, monkeypatch):
        monkeypatch.setattr(browser_fetcher, "_PROFILE_SLOTS", 2)
        held = [BrowserFetcher(profile=H2S_PROFILE) for _ in range(2)]
        for f in held:
            f.__enter__()

        BrowserFetcher(profile=H2S_PROFILE).__enter__()

        assert len(spy["persistent"]) == 2
        assert len(spy["ephemeral"]) == 1, "槽位占满时应退回临时 profile 而不是报错"

    def test_lock_survives_until_browser_closed(self, spy):
        """锁必须比浏览器活得久：提前放锁，别人拿到目录时 Chromium 还没退出。"""
        f = BrowserFetcher(profile=H2S_PROFILE)
        f.__enter__()
        assert f._profile_lock is not None

        f.close()
        assert f._profile_lock is None
        assert spy["persistent"][0].closed


class TestDegradesGracefully:
    def test_persistent_launch_failure_falls_back(self, spy, monkeypatch):
        """profile 损坏、磁盘满、锁冲突都不该让抓取停摆。"""
        import cloakbrowser

        monkeypatch.setattr(
            cloakbrowser, "launch_persistent_context",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("profile 打不开")),
        )
        f = BrowserFetcher(profile=H2S_PROFILE)
        f.__enter__()

        assert len(spy["ephemeral"]) == 1
        assert f._page is not None

    def test_failed_persistent_launch_releases_the_slot(self, spy, monkeypatch):
        """启动失败还占着槽位，重试几次就把槽位耗光了。"""
        import cloakbrowser

        monkeypatch.setattr(browser_fetcher, "_PROFILE_SLOTS", 1)
        attempts = {"n": 0}

        def flaky(user_data_dir, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("打不开")
            ctx = _FakeContext(user_data_dir=user_data_dir, **kwargs)
            spy["persistent"].append(ctx)
            return ctx

        monkeypatch.setattr(cloakbrowser, "launch_persistent_context", flaky)

        BrowserFetcher(profile=H2S_PROFILE).__enter__()  # 失败，退回临时

        # 唯一那个槽位必须已经放回去，否则下一个实例永远走不了持久化
        f = BrowserFetcher(profile=H2S_PROFILE)
        f.__enter__()
        assert f._profile_lock is not None
        assert len(spy["persistent"]) == 1

    def test_can_be_switched_off(self, spy, monkeypatch):
        monkeypatch.setenv("BROWSER_PERSIST_PROFILE", "0")
        BrowserFetcher(profile=H2S_PROFILE).__enter__()

        assert not spy["persistent"]
        assert len(spy["ephemeral"]) == 1

    def test_rebuild_keeps_using_persistent_profile(self, spy):
        """403 换 IP 重建时也要复用 profile，否则每次换 IP 都是冷启动。"""
        f = BrowserFetcher(profile=H2S_PROFILE)
        f.__enter__()
        f._rebuild_browser()

        assert len(spy["persistent"]) == 2
        dirs = [c.kwargs["user_data_dir"] for c in spy["persistent"]]
        assert dirs[0] == dirs[1]
