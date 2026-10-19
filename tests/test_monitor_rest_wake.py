"""monitor 侧的 REST 唤醒接线。

- ``run_once(only_sources=...)`` 只抓指定 source，completeness key 形态不变
- ``multi_source`` 按配置算：节流或唤醒轮让本轮只剩 H2S 时，前缀不能消失
- ``_wait_reload_or_wake`` 的优先级
- main_loop 真的跑起来：REST 报新 id → 下一轮只抓 H2S → 跑完回到原计划
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import mcore.h2s_rest as hr
import monitor
from config import (
    AvailabilityFilter,
    CityFilter,
    Config,
    OurDomainCityFilter,
    XiorCityFilter,
)
from models import Listing
from monitor import run_once
from storage import Storage


def _listing(lid, *, source, city):
    return Listing(
        id=lid, name=f"L {lid}", status="Available to book", price_raw="€700",
        available_from="2030-01-01", features=[], url=f"https://e.test/{lid}",
        city=city, source=source,
    )


def _cfg(tmp_path, **kw) -> Config:
    base = dict(
        check_interval=300,
        cities=[CityFilter(name="Eindhoven", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=Path(tmp_path) / "test.db",
        log_level="WARNING",
        sources=["holland2stay", "ourdomain", "xior"],
        ourdomain_cities=[OurDomainCityFilter(name="Amsterdam Diemen", key="diemen")],
        xior_cities=[XiorCityFilter(name="Amsterdam Naritaweg", key="p0196102")],
    )
    base.update(kw)
    return Config(**base)


def _ok(source, tasks, multi_source):
    key = (lambda c: f"{source}:{c}") if multi_source else (lambda c: c)
    return (
        [_listing(f"{source}-{i}", source=source, city=t.city_display)
         for i, t in enumerate(tasks)],
        {key(t.city_display): True for t in tasks},
    )


def _run(tmp_path, cfg, **kw):
    storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")
    calls: list[tuple[str, bool]] = []

    def fake_dispatch(tasks, *, multi_source=False):
        calls.append((tasks[0].source, multi_source))
        return _ok(tasks[0].source, tasks, multi_source)

    async def go():
        with patch("mcore.scrape_round.dispatch_scrape_tasks", side_effect=fake_dispatch), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None):
            return await run_once(cfg, storage, [], **kw)

    try:
        completeness = asyncio.run(go())
    finally:
        storage.close()
    return calls, completeness


class TestOnlySources:
    def test_only_h2s_is_dispatched(self, tmp_path):
        calls, _ = _run(tmp_path, _cfg(tmp_path),
                        only_sources=frozenset({"holland2stay"}))
        assert [s for s, _ in calls] == ["holland2stay"]

    def test_key_keeps_source_prefix(self, tmp_path):
        """只抓 H2S，但配置是多源：key 必须仍带前缀，否则会跨 source 收敛。"""
        calls, completeness = _run(tmp_path, _cfg(tmp_path),
                                   only_sources=frozenset({"holland2stay"}))
        assert calls == [("holland2stay", True)]
        assert completeness == {"holland2stay:Eindhoven": True}

    def test_unconfigured_source_skips_round(self, tmp_path):
        calls, completeness = _run(tmp_path, _cfg(tmp_path, sources=["xior"]),
                                   only_sources=frozenset({"holland2stay"}))
        assert calls == [] and completeness == {}

    def test_default_dispatches_everything(self, tmp_path):
        calls, _ = _run(tmp_path, _cfg(tmp_path))
        assert sorted(s for s, _ in calls) == ["holland2stay", "ourdomain", "xior"]

    def test_single_source_config_keeps_bare_keys(self, tmp_path):
        """单源部署（生产现状）的 key 形态不能被这次改动改掉。"""
        calls, completeness = _run(tmp_path, _cfg(tmp_path, sources=["holland2stay"]),
                                   only_sources=frozenset({"holland2stay"}))
        assert calls == [("holland2stay", False)]
        assert completeness == {"Eindhoven": True}


class TestMultiSourceFromConfig:
    def test_throttled_sources_do_not_drop_prefix(self, tmp_path):
        """节流跳过 Xior / OurDomain，本轮只剩 H2S——前缀照样要在。

        改之前 multi_source 按筛剩的任务算，这里会得到裸 key「Eindhoven」。
        """
        cfg = _cfg(tmp_path, source_min_intervals={"xior": 3600, "ourdomain": 3600})
        st = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")
        now = str(time.time())
        st.set_meta(monitor._SOURCE_LAST_SCRAPE_PREFIX + "xior", now)
        st.set_meta(monitor._SOURCE_LAST_SCRAPE_PREFIX + "ourdomain", now)
        st.close()
        calls, completeness = _run(tmp_path, cfg)
        assert calls == [("holland2stay", True)]
        assert completeness == {"holland2stay:Eindhoven": True}


# --------------------------------------------------------------------------- #
# _wait_reload_or_wake
# --------------------------------------------------------------------------- #
class TestWaitReloadOrWake:
    def _go(self, coro_fn):
        async def main():
            monitor._reload_event = asyncio.Event()
            monitor._h2s_wake_event = asyncio.Event()
            try:
                return await coro_fn()
            finally:
                monitor._reload_event = None
                monitor._h2s_wake_event = None
        return asyncio.run(main())

    def test_timeout(self):
        assert self._go(lambda: monitor._wait_reload_or_wake(0.01)) == "timeout"

    def test_wake(self):
        async def f():
            asyncio.get_running_loop().call_later(0.01, monitor._h2s_wake_event.set)
            return await monitor._wait_reload_or_wake(1.0)
        assert self._go(f) == "wake"

    def test_reload(self):
        async def f():
            asyncio.get_running_loop().call_later(0.01, monitor._reload_event.set)
            return await monitor._wait_reload_or_wake(1.0)
        assert self._go(f) == "reload"

    def test_reload_wins_when_both_set(self):
        async def f():
            monitor._h2s_wake_event.set()
            monitor._reload_event.set()
            return await monitor._wait_reload_or_wake(1.0)
        assert self._go(f) == "reload"

    def test_already_set_returns_immediately(self):
        async def f():
            monitor._h2s_wake_event.set()
            t0 = time.monotonic()
            r = await monitor._wait_reload_or_wake(5.0)
            return r, time.monotonic() - t0
        r, dt = self._go(f)
        assert r == "wake" and dt < 0.5


    def test_set_event_still_yields_to_other_tasks(self):
        """调用方忘了 clear 时每圈都立刻返回——必须仍然让出，不能饿死别的协程。"""
        async def f():
            monitor._h2s_wake_event.set()
            ran = []

            async def other():
                ran.append(1)
            asyncio.ensure_future(other())
            for _ in range(3):
                await monitor._wait_reload_or_wake(1.0)
            return list(ran)      # 拷贝：asyncio.run 收尾时还会把挂起的任务跑完
        assert self._go(f) == [1]


# --------------------------------------------------------------------------- #
# main_loop 真跑
# --------------------------------------------------------------------------- #
class TestMainLoopWiring:
    def test_wake_runs_h2s_only_round_then_resumes_plan(self, tmp_path, monkeypatch, caplog):
        """第 1 轮正常；轮中 REST 报新 id → 第 2 轮只抓 H2S；库里仍没有这条 →
        补抓定时器 → 第 3 轮也只抓 H2S。第 2 轮之后的等待要回到原计划。"""
        monkeypatch.setattr(monitor, "_REST_WAKE_MIN_GAP", 0.0)
        monkeypatch.setattr(hr, "RETRY_DELAYS", (0.05, 0.05))
        monkeypatch.setattr(monitor, "get_interval", lambda cfg: (300, False))

        holder: dict = {}
        real_cls = monitor.RestTrigger

        def make_trigger(wake):
            t = real_cls(wake)

            async def idle(get_cfg):
                await asyncio.Event().wait()
            t.run = idle
            holder["t"] = t
            return t

        monkeypatch.setattr(monitor, "RestTrigger", make_trigger)

        seen: list = []

        async def fake_run_once(cfg, storage, user_notifiers, *, only_sources=None, **kw):
            seen.append(only_sources)
            if len(seen) == 1:
                holder["t"].note_new([{"id": "a-1", "name": "A 1", "price": 900}])
            if len(seen) == 3:
                raise asyncio.CancelledError
            return {}

        async def no_alerts(*a, **k):
            return None

        monkeypatch.setattr(monitor, "run_once", fake_run_once)
        monkeypatch.setattr(monitor, "_dispatch_watchdog_alerts", no_alerts)
        monkeypatch.setattr(monitor._geocode, "geocode_missing", lambda st: (0, 0))

        storage = Storage(Path(tmp_path) / "test.db", timezone_str="UTC")
        cfg = _cfg(tmp_path, sources=["holland2stay"])

        async def go():
            await asyncio.wait_for(monitor.main_loop(cfg, storage, []), timeout=10)

        try:
            with caplog.at_level(logging.INFO, logger="monitor"):
                with pytest.raises(asyncio.CancelledError):
                    asyncio.run(go())
        finally:
            storage.close()
            monitor._reload_event = None
            monitor._h2s_wake_event = None

        assert seen == [None, monitor._REST_WAKE_SOURCES, monitor._REST_WAKE_SOURCES]
        assert "REST 唤醒·只抓 H2S" in caplog.text
        assert "回到原计划" in caplog.text
