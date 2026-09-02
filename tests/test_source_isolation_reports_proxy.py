"""单个 source 因代理挂掉时，要**当场**报给代理池，不能等整轮全灭。

起因（2026-08-27 生产）
----------------------
跨源隔离接住某个 source 的异常之后就 return 了，而标记冷却 / 切换 / 降级的代码
住在 ``run_once`` 外层的 ``except ProxyError``——那里只有整轮所有 source 都失败
时才够得着。于是形成一个死锁式的循环：

    H2S 成功（浏览器建在好线路上，缓存 2 小时）
      → 整轮不算全灭 → 外层处理器不触发
      → 代理池永远不知道那个代理挂了
      → OurCampus / Xior 每轮现取代理，拿到没被冷却的死代理 → 402 → 循环

当天 08 时（本地）的实测：source 级隔离 **176 次**，真正报给代理池只有 **4 次**；
同一小时 holland2stay 163/167 成功，而 ourcampus 和 xior 是 **0/167**。
**H2S 的健康恰恰是把另外两家钉死的原因**——它越顺，代理池越听不到坏消息。

判据因此换掉：冷却要回答的是「这个代理还能不能用」，原先却拿「整轮有没有全灭」
来判，两者不是一回事。
"""
from __future__ import annotations

import pytest

import config
import monitor
from scrapers.base import ProxyError, ScrapeNetworkError

P1 = "http://u1-nl-111111:pw@p.webshare.io:80"
P2 = "http://u2-nl-222222:pw@p.webshare.io:80"

DEAD_402 = ("[OurCampus Amsterdam Diemen] ourcampus 抓取失败: Failed to perform, "
            "curl: (56) CONNECT tunnel failed, response 402.")


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", P1)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", P2)
    monkeypatch.delenv("SCRAPE_PROXIES_PERSONAL", raising=False)
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()
    yield
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()


class TestReportingItself:
    def test_confirmed_failure_puts_the_proxy_into_cooldown(self, pool):
        e = ProxyError(DEAD_402)
        for _ in range(config._PROXY_FAILURE_CONFIRM_THRESHOLD):
            monitor._report_source_proxy_failure("ourcampus", e)
        assert config.is_proxy_in_cooldown(P1), "报了两次仍没冷却，池子还是会把它派出去"
        assert config.get_proxy_url() == P2, "没有落到备用代理"

    def test_one_blip_does_not_cool_anything(self, pool):
        """既有的「连续两次确认」阈值挡误伤：单次抖动只留一个 mark。"""
        monitor._report_source_proxy_failure("ourcampus", ProxyError(DEAD_402))
        assert not config.is_proxy_in_cooldown(P1)
        assert config.get_proxy_url() == P1

    def test_account_level_gets_the_long_cooldown(self, pool):
        import time
        for _ in range(config._PROXY_FAILURE_CONFIRM_THRESHOLD):
            monitor._report_source_proxy_failure("ourcampus", ProxyError(DEAD_402))
        left = config._proxy_cooldown_until[P1] - time.monotonic()
        assert left > config._PROXY_COOLDOWN_SEC, "402 没走长冷却"

    def test_transient_keeps_the_short_one(self, pool):
        import time
        e = ProxyError("CONNECT tunnel failed, response 502")
        for _ in range(config._PROXY_FAILURE_CONFIRM_THRESHOLD):
            monitor._report_source_proxy_failure("ourcampus", e)
        left = config._proxy_cooldown_until[P1] - time.monotonic()
        assert left <= config._PROXY_COOLDOWN_SEC

    def test_unconfirmed_error_never_cools(self, pool):
        """普通网络抖动不是代理服务端异常，报多少次都不该冷却谁。"""
        e = ScrapeNetworkError("Connection reset by peer")
        for _ in range(5):
            monitor._report_source_proxy_failure("xior", e)
        assert not config.is_proxy_in_cooldown(P1)

    def test_never_raises(self, pool, monkeypatch):
        """上报失败不能把「隔离该 source、继续跑别的」一起带崩。

        那正是这层隔离存在的理由——2026-08-02 Xior 的 greenlet.error 曾穿透
        dispatcher，把同轮 H2S / OurDomain 的结果一起带走。
        """
        def _boom(*a, **k):
            raise RuntimeError("代理池炸了")
        monkeypatch.setattr(config, "report_proxy_failure", _boom)
        monitor._report_source_proxy_failure("ourcampus", ProxyError(DEAD_402))  # 不抛即通过


class TestWiredIntoTheIsolationBranch:
    """接线断了，上面那些用例一条都不会红——这正是这个 bug 当初的形态。

    ⚠️ ``_isolation_blocks`` 返回**全部**隔离分支，不是第一个。

    上一版用的是 ``src.index("整体抓取失败，已隔离该 source")``——取第一处。可是
    那句日志在 ``run_once`` 里有两份：跨源隔离的 ``_dispatch_isolated`` 一份，
    Holland2Stay 走独立熔断路径又抄了一份。``index`` 命中的永远是前者，于是
    **H2S 那份从 2026-08-17 写下起就没有 `_report_source_proxy_failure`，
    而这组用例全绿**。

    这正是本文件开头描述的那个死锁里最关键的一环：H2S 是最不容易失败的 source
    （浏览器建在好线路上、会话缓存两小时），它每成功一轮整轮就不算全灭，外层的
    `except ProxyError` 就够不着。它自己失败时又不上报——环就闭合了。
    """

    def _isolation_blocks(self) -> list[str]:
        import inspect
        import re
        src = inspect.getsource(monitor.run_once)
        return [src[m.start():m.start() + 1600]
                for m in re.finditer("整体抓取失败，已隔离该 source", src)]

    def test_there_is_more_than_one_such_branch(self):
        """前提本身要钉住：只剩一处时说明有人合并了，上面那些「每一处」就该改写。"""
        assert len(self._isolation_blocks()) >= 2

    def test_every_isolation_branch_reports_to_the_pool(self):
        for i, block in enumerate(self._isolation_blocks()):
            assert "_report_source_proxy_failure" in block, (
                f"第 {i + 1} 处隔离分支又变回「接住就 return」了——代理池永远听不到坏消息")

    def test_every_branch_is_guarded_by_is_proxy_error(self):
        """只有代理造成的失败才报。403 / 平台维护 / 解析错误跟代理无关，
        拿它们去冷却代理会把好线路误伤下线。"""
        for i, block in enumerate(self._isolation_blocks()):
            assert "is_proxy_error(e)" in block, f"第 {i + 1} 处没有守卫"

    def test_reported_before_the_telemetry_write(self):
        """顺序无所谓对错，但要稳定：遥测那行会 return，报告写在它后面就永远不执行。"""
        for i, block in enumerate(self._isolation_blocks()):
            assert (block.index("_report_source_proxy_failure")
                    < block.index("_record_source_round")), f"第 {i + 1} 处顺序反了"

    def test_not_in_dry_run(self):
        """dry_run 不该改动全局代理状态。"""
        for i, block in enumerate(self._isolation_blocks()):
            assert "if not dry_run and is_proxy_error(e):" in block, f"第 {i + 1} 处"


class TestTheScenarioThatCausedIt:
    def test_a_healthy_source_no_longer_masks_a_dead_proxy(self, pool):
        """重放 2026-08-27 08 时：H2S 一直成功，OurCampus 每轮 402。

        修复前：整轮不算全灭 → 外层处理器不触发 → 死代理永不冷却 → 循环 167 轮。
        修复后：OurCampus 自己那两轮就把它冷却掉，第三轮起全员改用备用代理。
        """
        e = ProxyError(DEAD_402)
        used = []
        for _ in range(4):
            used.append(config.get_proxy_url())     # OurCampus 每轮现取
            monitor._report_source_proxy_failure("ourcampus", e)

        assert used[0] == P1 and used[1] == P1, "前两轮用主代理（攒确认）"
        assert used[2] == P2, "第三轮就该换掉，而不是继续撞 167 轮"
        assert used[3] == P2
