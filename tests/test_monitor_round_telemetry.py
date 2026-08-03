"""monitor 写入轮次遥测的接线测试。

存储层本身在 test_round_stats.py 里测过了；这里测的是**接线**——run_once 真的
在每个 source 跑完后写了一行，而且写对了。

最关键的一条：**source 失败时也要有行**。「整轮全失败」恰恰是最该留痕的情形，
而那条路径会直接上抛；如果遥测攒到整轮结束才写，它就正好在这时丢失。
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
from monitor import _completeness_stats, run_once
from scrapers.base import BlockedError, RateLimitError
from storage import Storage


def _listing(lid, *, source, city):
    return Listing(
        id=lid, name=f"L {lid}", status="Available to book", price_raw="€700",
        available_from="2030-01-01", features=[], url=f"https://e.test/{lid}",
        city=city, source=source,
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


def _ok(source, tasks, multi_source):
    key = lambda c: f"{source}:{c}" if multi_source else c
    return (
        [_listing(f"{source}-{i}", source=source, city=t.city_display)
         for i, t in enumerate(tasks)],
        {key(t.city_display): True for t in tasks},
    )


def _run(tmp_path, side_effect, *, dry_run=False):
    """跑一轮，返回 (storage, 抛出的异常或 None)。storage 由调用方关闭。"""
    storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")
    raised = []

    def fake_dispatch(tasks, *, multi_source=False):
        return side_effect(tasks[0].source, tasks, multi_source)

    async def go():
        with patch("monitor.dispatch_scrape_tasks", side_effect=fake_dispatch), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None):
            try:
                await run_once(_cfg(tmp_path), storage, [], dry_run=dry_run)
            except BaseException as e:  # noqa: BLE001
                raised.append(e)

    asyncio.run(go())
    return storage, (raised[0] if raised else None)


class TestCompletenessStats:
    def test_counts(self):
        assert _completeness_stats({"a": True, "b": False, "c": True}) == (2, 3)

    def test_empty(self):
        assert _completeness_stats({}) == (0, 0)


class TestTelemetryWiring:
    def test_one_row_per_source(self, tmp_path):
        st, exc = _run(tmp_path, lambda s, t, m: _ok(s, t, m))
        try:
            assert exc is None
            rows = st.recent_round_stats()
            assert {r["source"] for r in rows} == {"holland2stay", "ourdomain", "xior"}
            assert all(r["error_type"] == "" for r in rows)
        finally:
            st.close()

    def test_all_sources_share_one_round_at(self, tmp_path):
        """同一轮的各 source 必须共用时间戳，否则面板会把一轮拆成好几组。"""
        st, _ = _run(tmp_path, lambda s, t, m: _ok(s, t, m))
        try:
            assert len({r["round_at"] for r in st.recent_round_stats()}) == 1
            assert len(st.recent_rounds_grouped()) == 1
        finally:
            st.close()

    def test_listings_and_completeness_recorded(self, tmp_path):
        st, _ = _run(tmp_path, lambda s, t, m: _ok(s, t, m))
        try:
            h2s = st.recent_round_stats(source="holland2stay")[0]
            assert h2s["listings"] == 1
            assert h2s["targets"] == 1
            assert h2s["complete"] == 1
        finally:
            st.close()

    def test_failed_source_gets_a_row_with_error(self, tmp_path):
        """失败也要留痕——否则遥测里这个 source 直接消失，和"没配它"无从区分。"""
        def side_effect(source, tasks, multi_source):
            if source == "xior":
                raise RateLimitError("429 Too Many Requests")
            return _ok(source, tasks, multi_source)

        st, exc = _run(tmp_path, side_effect)
        try:
            assert exc is None
            xior = st.recent_round_stats(source="xior")[0]
            assert xior["error_type"] == "RateLimitError"
            assert "429" in xior["error_msg"]
            assert xior["listings"] == 0
            assert xior["complete"] == 0
            assert xior["targets"] == 1
        finally:
            st.close()

    def test_rows_survive_a_total_round_failure(self, tmp_path):
        """所有 source 都挂 → run_once 上抛。遥测若攒到整轮结束才写，
        就正好在最需要它的时候丢失。"""
        def side_effect(source, tasks, multi_source):
            raise RateLimitError(f"{source} 429")

        st, exc = _run(tmp_path, side_effect)
        try:
            assert exc is not None, "全部 source 失败仍应上抛（旧契约不变）"
            rows = st.recent_round_stats()
            assert {r["source"] for r in rows} == {"holland2stay", "ourdomain", "xior"}
            assert all(r["error_type"] == "RateLimitError" for r in rows)
        finally:
            st.close()

    def test_h2s_blocked_is_recorded(self, tmp_path):
        def side_effect(source, tasks, multi_source):
            if source == "holland2stay":
                raise BlockedError("Cloudflare 403")
            return _ok(source, tasks, multi_source)

        st, _ = _run(tmp_path, side_effect)
        try:
            h2s = st.recent_round_stats(source="holland2stay")[0]
            assert h2s["error_type"] == "BlockedError"
        finally:
            st.close()

    def test_duration_is_recorded(self, tmp_path):
        st, _ = _run(tmp_path, lambda s, t, m: _ok(s, t, m))
        try:
            assert all(r["duration_ms"] >= 0 for r in st.recent_round_stats())
        finally:
            st.close()

    def test_dry_run_writes_nothing(self, tmp_path):
        """--test 模式不写库不发通知，遥测也一样。"""
        st, _ = _run(tmp_path, lambda s, t, m: _ok(s, t, m), dry_run=True)
        try:
            assert st.recent_round_stats() == []
        finally:
            st.close()

    def test_telemetry_failure_does_not_break_the_round(self, tmp_path):
        """遥测写挂了，抓取必须照常完成。观测不该把被观测的东西弄崩。"""
        storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")

        def boom(*a, **kw):
            raise RuntimeError("telemetry exploded")

        def fake_dispatch(tasks, *, multi_source=False):
            return _ok(tasks[0].source, tasks, multi_source)

        async def go():
            with patch("monitor.dispatch_scrape_tasks", side_effect=fake_dispatch), \
                 patch("mcore.prewarm.create_prewarmed_session", return_value=None), \
                 patch.object(type(storage), "record_round_stat", boom):
                return await run_once(_cfg(tmp_path), storage, [], dry_run=False)

        try:
            completeness = asyncio.run(go())
            assert completeness, "遥测异常不该影响本轮结果"
            assert storage.count_all() == 3, "房源必须照常入库"
        finally:
            storage.close()
