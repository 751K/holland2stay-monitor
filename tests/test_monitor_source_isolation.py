"""monitor 逐 source 隔离测试。

背景（2026-08-03 生产事故）
---------------------------
``dispatch_scrape_tasks`` 内部已经按 task 隔离，但它在「本次调用的任务**全部**
失败」时仍会上抛。而 monitor 是**按 source 分开调用**它的，于是那个判定退化成
了「单个 source 全失败」——等于跨 source 的保护完全失效。

实测后果：Xior 四栋楼连续 429 → RateLimitError 逃出整个 dispatch →
  1. 同轮 OurDomain 已抓到的结果被丢弃
  2. H2S 排在最后，根本没被执行
  3. 每个用户都收到「监控将暂停 5 分钟」
  4. 全局冷却 + 自适应间隔翻倍
24 小时内发生 3 次。

本文件锁住修复后的契约：一个 source 塌了，其余 source 的结果照常入库；只有
**所有** source 都失败时才上抛，让 main_loop 照旧冷却。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from config import (
    AvailabilityFilter,
    CityFilter,
    Config,
    OurDomainCityFilter,
    XiorCityFilter,
)
from models import Listing
from monitor import _pick_round_failure, run_once
from notifier import BaseNotifier
from scrapers.base import (
    BlockedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
)
from storage import Storage


# ── 脚手架 ──────────────────────────────────────────────────────────

def _listing(lid: str, *, source: str, city: str) -> Listing:
    return Listing(
        id=lid,
        name=f"Listing {lid}",
        status="Available to book",
        price_raw="€700",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{lid}",
        city=city,
        source=source,
    )


class _Notifier(BaseNotifier):
    has_channels = True

    def __init__(self) -> None:
        self.errors: list[str] = []

    async def _send(self, text):
        return True

    async def send_error(self, msg):
        self.errors.append(msg)
        return True

    async def send_new_listing(self, listing):
        return True

    async def close(self):
        pass


def _key(source: str, city: str, multi_source: bool) -> str:
    """与 ``scrapers._completeness_key`` 同构：多源时带 source 前缀。"""
    return f"{source}:{city}" if multi_source else city


def _ok(source: str, tasks: list, multi_source: bool):
    """一个 source 全部抓成功时 dispatcher 的返回形状。"""
    return (
        [
            _listing(f"{source}-{i}", source=source, city=t.city_display)
            for i, t in enumerate(tasks)
        ],
        {_key(source, t.city_display, multi_source): True for t in tasks},
    )


def _cfg(tmp_path) -> Config:
    return Config(
        check_interval=300,
        cities=[CityFilter(name="Eindhoven", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=Path(tmp_path) / "test.db",
        log_level="WARNING",
        sources=["holland2stay", "ourdomain", "xior"],
        ourdomain_cities=[OurDomainCityFilter(name="Amsterdam Diemen", key="diemen")],
        xior_cities=[XiorCityFilter(name="Amsterdam Naritaweg", key="p0196102")],
    )


def _run(tmp_path, dispatch_side_effect):
    """跑一轮 run_once，dispatch 按 source 分派到 side_effect。

    Returns (completeness, notifier, storage, 抛出的异常或 None)
    """
    storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")
    notifier = _Notifier()

    def fake_dispatch(tasks, *, multi_source=False):
        return dispatch_side_effect(tasks[0].source, tasks, multi_source)

    raised: list[BaseException] = []
    result: dict = {}

    async def go():
        with patch("monitor.dispatch_scrape_tasks", side_effect=fake_dispatch), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None):
            try:
                result["completeness"] = await run_once(
                    _cfg(tmp_path), storage, [], dry_run=False,
                )
            except BaseException as e:  # noqa: BLE001 - 测试要捕获全部
                raised.append(e)

    try:
        asyncio.run(go())
    finally:
        storage.close()
    return result.get("completeness"), notifier, raised[0] if raised else None


# ── 逐 source 隔离 ──────────────────────────────────────────────────

class TestSourceIsolation:
    def test_xior_429_does_not_kill_other_sources(self, tmp_path):
        """复现 2026-08-03：Xior 全员 429，H2S / OurDomain 结果必须保住。"""
        def side_effect(source, tasks, multi_source):
            if source == "xior":
                raise RateLimitError("Xior 返回 429 Too Many Requests")
            return _ok(source, tasks, multi_source)

        completeness, _, exc = _run(tmp_path, side_effect)

        assert exc is None, f"Xior 的 429 不该逃逸整轮，实际抛出 {exc!r}"
        # H2S + OurDomain 完整；Xior 标 ✗ 而不是从统计里消失。
        # 三个 source 同轮 → key 必须带 source 前缀（防同名城市互相覆盖）
        assert completeness == {
            "holland2stay:Eindhoven": True,
            "ourdomain:Amsterdam Diemen": True,
            "xior:Amsterdam Naritaweg": False,
        }

    def test_ourdomain_403_does_not_kill_other_sources(self, tmp_path):
        """OurDomain 只有 1 个 task，任何 403 都满足「全失败」——最易触发的一条。"""
        def side_effect(source, tasks, multi_source):
            if source == "ourdomain":
                raise BlockedError("OurDomain Cloudflare WAF 屏蔽（HTTP 403）")
            return _ok(source, tasks, multi_source)

        completeness, _, exc = _run(tmp_path, side_effect)

        assert exc is None
        assert completeness["holland2stay:Eindhoven"] is True
        assert completeness["ourdomain:Amsterdam Diemen"] is False

    def test_failed_source_listings_are_not_stored(self, tmp_path):
        """隔离掉的 source 不该留下半截数据，但成功的 source 必须入库。"""
        def side_effect(source, tasks, multi_source):
            if source == "xior":
                raise ScrapeNetworkError("boom")
            return _ok(source, tasks, multi_source)

        storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")

        def fake_dispatch(tasks, *, multi_source=False):
            return side_effect(tasks[0].source, tasks, multi_source)

        async def go():
            with patch("monitor.dispatch_scrape_tasks", side_effect=fake_dispatch), \
                 patch("mcore.prewarm.create_prewarmed_session", return_value=None):
                await run_once(_cfg(tmp_path), storage, [], dry_run=False)

        try:
            asyncio.run(go())
            sources = sorted(storage.get_distinct_sources())
            assert sources == ["holland2stay", "ourdomain"]
        finally:
            storage.close()

    def test_all_sources_failing_still_raises(self, tmp_path):
        """全塌了就得上抛——否则 main_loop 不冷却，会原速空转刷站。"""
        def side_effect(source, tasks, multi_source):
            raise ScrapeNetworkError(f"{source} down")

        _, _, exc = _run(tmp_path, side_effect)

        assert isinstance(exc, ScrapeNetworkError)

    def test_partial_success_suppresses_raise(self, tmp_path):
        """只要有一个 source 成功，就不该走冷却路径。"""
        def side_effect(source, tasks, multi_source):
            if source == "holland2stay":
                return _ok(source, tasks, multi_source)
            raise BlockedError(f"{source} blocked")

        completeness, _, exc = _run(tmp_path, side_effect)

        assert exc is None
        assert completeness["holland2stay:Eindhoven"] is True


# ── 全失败时挑哪个异常上抛 ──────────────────────────────────────────

class TestPickRoundFailure:
    def test_proxy_error_wins(self):
        """代理故障有明确修复动作，且全员失败时它大概率是共同根因。"""
        proxy = ProxyError("tunnel failed 502")
        chosen = _pick_round_failure([
            ("xior", RateLimitError("429")),
            ("ourdomain", proxy),
            ("holland2stay", BlockedError("403")),
        ])
        assert chosen is proxy

    def test_maintenance_beats_blocked(self):
        maint = UpstreamMaintenanceError("H2S 平台维护中")
        chosen = _pick_round_failure([
            ("ourdomain", BlockedError("403")),
            ("holland2stay", maint),
        ])
        assert chosen is maint

    def test_blocked_beats_ratelimit(self):
        blocked = BlockedError("403")
        chosen = _pick_round_failure([
            ("xior", RateLimitError("429")),
            ("ourdomain", blocked),
        ])
        assert chosen is blocked

    def test_proxy_error_matched_before_network_parent(self):
        """ProxyError 是 ScrapeNetworkError 子类，顺序写反就永远匹配不到。"""
        proxy = ProxyError("tunnel failed 502")
        chosen = _pick_round_failure([
            ("xior", ScrapeNetworkError("timeout")),
            ("ourdomain", proxy),
        ])
        assert chosen is proxy

    def test_unclassified_falls_back_to_first(self):
        first = RuntimeError("something odd")
        chosen = _pick_round_failure([("xior", first), ("ourdomain", ValueError("x"))])
        assert chosen is first


# ── completeness key 的 source 前缀（P2）───────────────────────────

class TestCompletenessKeyPrefix:
    """多源同轮时 key 必须带 source 前缀，否则同名城市会互相覆盖。

    ``_completeness_key`` 原本靠 ``len(by_source) <= 1`` 判断要不要加前缀，
    但 monitor 从 v1.9.9 起按 source 分开调 dispatcher，每次调用里 ``by_source``
    恒为 1——前缀永远加不上。生产日志里三个 source 同时开着却是
    ``Amsterdam Diemen=✓, Eindhoven=✓`` 这种裸城市名。

    连带后果：``_mark_stale_listings_for_complete_cities`` 全部走
    ``complete_cities`` 分支，``mark_stale_listings`` 用的是不带 source 条件的
    ``city IN (...)``——为多源隔离准备的 ``source_city_pairs`` 在生产里永远是空的。
    """

    def test_multi_source_round_prefixes_keys(self, tmp_path):
        def side_effect(source, tasks, multi_source):
            assert multi_source is True, "三个 source 同轮，必须告诉 dispatcher 是多源"
            return _ok(source, tasks, multi_source)

        completeness, _, exc = _run(tmp_path, side_effect)

        assert exc is None
        # 显式列全，避免「completeness 是空 dict」时 all() 空真通过
        assert completeness == {
            "holland2stay:Eindhoven": True,
            "ourdomain:Amsterdam Diemen": True,
            "xior:Amsterdam Naritaweg": True,
        }

    def test_single_source_round_keeps_bare_city(self, tmp_path):
        """单源部署保持裸城市名——日志更干净，也兼容旧行为。"""
        cfg = _cfg(tmp_path)
        cfg.sources = ["holland2stay"]
        cfg.ourdomain_cities = []
        cfg.xior_cities = []

        storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")
        seen: list[bool] = []

        def fake_dispatch(tasks, *, multi_source=False):
            seen.append(multi_source)
            return _ok(tasks[0].source, tasks, multi_source)

        async def go():
            with patch("monitor.dispatch_scrape_tasks", side_effect=fake_dispatch), \
                 patch("mcore.prewarm.create_prewarmed_session", return_value=None):
                return await run_once(cfg, storage, [], dry_run=False)

        try:
            completeness = asyncio.run(go())
        finally:
            storage.close()

        assert seen == [False]
        assert completeness == {"Eindhoven": True}

    def test_isolated_failure_key_matches_success_key(self, tmp_path):
        """失败 source 补的 key 必须和成功路径同构，否则 stale 收敛认不出来。"""
        def side_effect(source, tasks, multi_source):
            if source == "xior":
                raise BlockedError("blocked")
            return _ok(source, tasks, multi_source)

        completeness, _, exc = _run(tmp_path, side_effect)

        assert exc is None
        assert completeness["xior:Amsterdam Naritaweg"] is False
