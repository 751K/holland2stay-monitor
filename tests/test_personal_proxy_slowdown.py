"""走自家线路时主动降速——保护的是用户自己的家庭 IP。

和「代理全挂降级直连」的区别
----------------------------
那个是**被迫**：池子空了，只剩服务器原生 IP，于是限到 10 分钟一轮，怕的是把
机房 IP 打穿。这个是**主动**：家里那条线还好好的，仍然自愿慢下来。

理由不对称：商业住宅池的出口烧了换一个就是，池子里有几万个；自家 IP 烧了没得
换，而且连带影响家里所有上网。2026-08-02 的教训说明这不是杞人忧天——所有 source
挤在同一个 sticky 出口时，Xior 一轮 12 个请求就触发 429，四栋楼全挂。而家里只
有一个 IP，没法按 source 分离。

阈值 120 秒的由来：高峰实测轮次中位 26 秒（P90 75 秒，n=395），降到约 1/5 强度；
同时仍小于房源可订窗口（2026-08-25 实测中位 154 分钟、最短 4 分钟）。再慢就开始
真的漏房源了。
"""
from __future__ import annotations

import inspect

import pytest

import config
import monitor

P1 = "http://u1-nl-111111:pw@p.webshare.io:80"
P2 = "http://u2-nl-222222:pw@p.webshare.io:80"
HOME = "socks5://172.19.0.1:1080"


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", P1)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", f"{P2},{HOME}")
    monkeypatch.setenv("SCRAPE_PROXIES_PERSONAL", HOME)
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()
    yield
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()


def _cool(url: str) -> None:
    import time
    config._proxy_cooldown_until[url] = time.monotonic() + config._PROXY_COOLDOWN_SEC


class TestWhenItEngages:
    def test_quiet_while_commercial_proxies_work(self, pool):
        """商业代理还活着就不该限速——那是花钱买来给你用的。"""
        assert not config.is_personal_proxy_active()

    def test_still_quiet_on_the_backup(self, pool):
        _cool(P1)
        assert config.get_proxy_url() == P2
        assert not config.is_personal_proxy_active()

    def test_engages_once_we_fall_through_to_home(self, pool):
        _cool(P1)
        _cool(P2)
        assert config.get_proxy_url() == HOME
        assert config.is_personal_proxy_active()

    def test_disengages_when_commercial_comes_back(self, pool):
        _cool(P1)
        _cool(P2)
        assert config.is_personal_proxy_active()
        config._proxy_cooldown_until.clear()
        assert not config.is_personal_proxy_active(), "商业代理恢复后还在限速"

    def test_not_active_when_nothing_registered(self, monkeypatch, pool):
        """没登记个人线路 = 池子里都是商业代理，永远不限速。"""
        monkeypatch.delenv("SCRAPE_PROXIES_PERSONAL", raising=False)
        _cool(P1)
        _cool(P2)
        assert config.get_proxy_url() == HOME
        assert not config.is_personal_proxy_active()

    def test_not_active_when_everything_is_cooling(self, pool):
        """三个都挂 = 降级直连原生 IP，那时该由 native fallback 那条限速接手。"""
        for p in (P1, P2, HOME):
            _cool(p)
        assert config.get_proxy_url() == ""
        assert not config.is_personal_proxy_active()


class TestEndpointMatching:
    """比对忽略用户名里的 session 段和密码——sticky 改写会换掉它们。"""

    def test_matches_despite_session_rewrite(self, monkeypatch):
        commercial_personal = "http://u1-nl-111111:pw@p.webshare.io:80"
        monkeypatch.setenv("HTTPS_PROXY", commercial_personal)
        monkeypatch.delenv("SCRAPE_PROXIES_FALLBACK", raising=False)
        monkeypatch.setenv("SCRAPE_PROXIES_PERSONAL", commercial_personal)
        config._proxy_cooldown_until.clear()
        rewritten = config._with_source_session(commercial_personal, "xior")
        assert rewritten != commercial_personal, "前提不成立：这条没有被改写"
        assert config._same_proxy_endpoint(rewritten, commercial_personal)

    @pytest.mark.parametrize("other", [
        "socks5://172.19.0.1:1081",     # 端口不同
        "socks5://172.19.0.2:1080",     # 主机不同
        "http://172.19.0.1:1080",       # scheme 不同
    ])
    def test_does_not_match_a_different_endpoint(self, other):
        assert not config._same_proxy_endpoint(HOME, other)


class TestTheFloorItself:
    def test_default_is_two_minutes(self):
        assert monitor._PERSONAL_PROXY_MIN_INTERVAL == 120

    def test_overridable(self, monkeypatch):
        monkeypatch.setenv("PERSONAL_PROXY_MIN_INTERVAL", "300")
        assert monitor._env_int_positive("PERSONAL_PROXY_MIN_INTERVAL", 120) == 300

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
    def test_bad_values_fall_back_to_the_safe_side(self, monkeypatch, bad):
        """手滑写错的阈值不该让监控起不来，回落到默认（慢的那一侧）。"""
        monkeypatch.setenv("PERSONAL_PROXY_MIN_INTERVAL", bad)
        assert monitor._env_int_positive("PERSONAL_PROXY_MIN_INTERVAL", 120) == 120

    def test_floor_is_below_the_shortest_observed_booking_window(self):
        """4 分钟是 2026-08-25 实测最短的可订窗口。降速不能慢到连它都错过。"""
        assert monitor._PERSONAL_PROXY_MIN_INTERVAL < 4 * 60


class TestWiredIntoTheLoop:
    """算式接线断了，上面那些用例一条都不会红。"""

    def _src(self) -> str:
        return inspect.getsource(monitor.main_loop)

    def test_interval_is_raised(self):
        src = self._src()
        assert "max(effective_interval, _PERSONAL_PROXY_MIN_INTERVAL)" in src

    def test_clamped_again_after_jitter(self):
        """jitter_ratio 生产值是 0.4——负抖动能把 120 秒打到 72 秒。"""
        src = self._src()
        jitter = src.index("actual = apply_jitter(effective_interval")
        clamp = src.index("max(actual, _PERSONAL_PROXY_MIN_INTERVAL)")
        assert jitter < clamp

    def test_the_post_jitter_clamp_is_guarded_by_the_right_condition(self):
        """光比位置不够。

        变异测试确认过：把守卫改成 ``if False:``，那行 max 还在原位，只比下标
        的断言照样绿——降速却已经被抖动漏穿了。这里走 AST，直接认那个 if 的
        条件是不是 ``personal_proxy_active``。
        """
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(self._src()))
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            body = "".join(ast.unparse(st) for st in node.body)
            if "max(actual, _PERSONAL_PROXY_MIN_INTERVAL)" not in body:
                continue
            if ast.unparse(node.test) == "personal_proxy_active":
                guarded = True
        assert guarded, "抖动后的下限夹取没有被 personal_proxy_active 守住"

    def test_jitter_really_can_go_below_the_floor(self):
        """上一条守的前提：抖动确实会把 120 拉到 120 以下，否则那道夹取是白写的。"""
        from mcore.interval import apply_jitter
        lows = [apply_jitter(120, 0.4) for _ in range(200)]
        assert min(lows) < 120, "抖动从不下探，这条防线就没有存在的理由"
        assert all(max(v, 120) >= 120 for v in lows)

    def test_uses_the_active_proxy_not_mere_configuration(self):
        """判据必须是 is_personal_proxy_active()（当前在用哪个），
        不是「有没有配过个人线路」——后者会让商业代理正常时也限速。"""
        assert "is_personal_proxy_active()" in self._src()

    def test_arithmetic_reproduced(self):
        """把循环里那两行原样复算：高峰 60 秒基准会被抬到 120。"""
        effective = 60
        effective = max(effective, monitor._PERSONAL_PROXY_MIN_INTERVAL)
        assert effective == 120

    def test_never_speeds_things_up(self):
        """峰外基准 300 秒本来就比下限慢，不能被拉快到 120。"""
        effective = 300
        effective = max(effective, monitor._PERSONAL_PROXY_MIN_INTERVAL)
        assert effective == 300
