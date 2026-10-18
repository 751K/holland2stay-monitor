"""mcore/h2s_rest.py —— REST 触发器：什么时候轮询、什么时候叫醒、怎么核对。

不发请求：watcher 用替身，时钟可控，补抓定时器只记录不真排。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import mcore.h2s_rest as hr
from mcore.h2s_rest import RestTrigger, h2s_city_ids, rest_interval


class _Cfg:
    def __init__(self, tasks):
        self._tasks = tasks

    def scrape_tasks_v2(self):
        return list(self._tasks)


def _task(source, key):
    return SimpleNamespace(source=source, city_key=key, city_display=str(key))


H2S_EHV = _task("holland2stay", "29")


class _Watcher:
    instances: list["_Watcher"] = []

    def __init__(self, city_ids):
        self.city_ids = city_ids
        self.results: list[list[dict]] = []
        self.last_error = None
        self.closed = False
        self.polls = 0
        _Watcher.instances.append(self)

    def poll(self):
        self.polls += 1
        return self.results.pop(0) if self.results else []

    def close(self):
        self.closed = True


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Storage:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def get_listing(self, lid):
        return self.rows.get(lid)


@pytest.fixture
def env(monkeypatch):
    _Watcher.instances.clear()
    monkeypatch.setenv(hr.ENABLED_ENV, "1")
    monkeypatch.delenv(hr.INTERVAL_ENV, raising=False)
    state = {"in_window": True}
    monkeypatch.setattr(hr, "in_rest_window", lambda now=None: state["in_window"])
    clock = _Clock()
    scheduled: list[float] = []
    trig = RestTrigger(asyncio.Event(), watcher_factory=_Watcher, clock=clock,
                       schedule=lambda d, fn: scheduled.append(d))
    return SimpleNamespace(trig=trig, clock=clock, scheduled=scheduled,
                           state=state, cfg=_Cfg([H2S_EHV]))


def _tick(env, cfg=None):
    return asyncio.run(env.trig.tick(cfg or env.cfg))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class TestConfig:
    def test_city_ids_follow_h2s_tasks_only(self):
        cfg = _Cfg([_task("holland2stay", "29"), _task("xior", "p01"),
                    _task("holland2stay", "25"), _task("holland2stay", "29")])
        assert h2s_city_ids(cfg) == (25, 29)

    def test_non_numeric_city_key_is_skipped(self):
        assert h2s_city_ids(_Cfg([_task("holland2stay", "abc")])) == ()

    @pytest.mark.parametrize("raw,want", [
        (None, 3.0), ("", 3.0), ("5", 5.0), ("0.2", hr.MIN_INTERVAL), ("x", 3.0),
    ])
    def test_interval(self, monkeypatch, raw, want):
        if raw is None:
            monkeypatch.delenv(hr.INTERVAL_ENV, raising=False)
        else:
            monkeypatch.setenv(hr.INTERVAL_ENV, raw)
        assert rest_interval() == want

    @pytest.mark.parametrize("raw,want", [
        ("1", True), ("true", True), ("on", True), ("0", False), ("", False), (None, False),
    ])
    def test_enabled(self, monkeypatch, raw, want):
        if raw is None:
            monkeypatch.delenv(hr.ENABLED_ENV, raising=False)
        else:
            monkeypatch.setenv(hr.ENABLED_ENV, raw)
        assert hr.rest_enabled() is want


# --------------------------------------------------------------------------- #
# 时间窗口（与 PEAK_* 脱钩）
# --------------------------------------------------------------------------- #
AMS = ZoneInfo("Europe/Amsterdam")


def _at(weekday_date: str, hhmm: str) -> datetime:
    """``_at("2026-10-19", "09:00")``——2026-10-19 是周一。"""
    return datetime.fromisoformat(f"{weekday_date}T{hhmm}:00").replace(tzinfo=AMS)


MON, SAT = "2026-10-19", "2026-10-24"


class TestWindow:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for k in (hr.START_ENV, hr.END_ENV, hr.WEEKDAYS_ENV):
            monkeypatch.delenv(k, raising=False)

    @pytest.mark.parametrize("hhmm,want", [
        ("08:29", False), ("08:30", True), ("11:00", True), ("16:30", True),
        ("17:45", True), ("17:46", False), ("00:18", False),
    ])
    def test_default_is_weekday_0830_to_1745(self, hhmm, want):
        assert hr.in_rest_window(_at(MON, hhmm)) is want

    def test_default_skips_weekend(self):
        assert not hr.in_rest_window(_at(SAT, "10:00"))

    def test_weekend_allowed_when_not_weekdays_only(self, monkeypatch):
        monkeypatch.setenv(hr.WEEKDAYS_ENV, "false")
        assert hr.in_rest_window(_at(SAT, "10:00"))

    def test_custom_window(self, monkeypatch):
        monkeypatch.setenv(hr.START_ENV, "10:00")
        monkeypatch.setenv(hr.END_ENV, "12:15")
        assert not hr.in_rest_window(_at(MON, "09:59"))
        assert hr.in_rest_window(_at(MON, "12:15"))
        assert not hr.in_rest_window(_at(MON, "12:16"))

    def test_window_crossing_midnight(self, monkeypatch):
        monkeypatch.setenv(hr.START_ENV, "23:30")
        monkeypatch.setenv(hr.END_ENV, "00:30")
        assert hr.in_rest_window(_at(MON, "23:45"))
        assert hr.in_rest_window(_at(MON, "00:20"))
        assert not hr.in_rest_window(_at(MON, "01:00"))

    def test_does_not_follow_peak_settings(self, monkeypatch):
        """PEAK_* 怎么改都不影响 REST 的窗口。"""
        monkeypatch.setenv("PEAK_START", "08:30")
        monkeypatch.setenv("PEAK_END", "10:00")
        monkeypatch.setenv("PEAK_WEEKDAYS_ONLY", "false")
        assert hr.in_rest_window(_at(MON, "16:30"))
        assert not hr.in_rest_window(_at(SAT, "09:00"))

    @pytest.mark.parametrize("bad", ["8.30", "25:00", "abc", "08:61"])
    def test_bad_time_falls_back_to_default(self, monkeypatch, caplog, bad):
        monkeypatch.setenv(hr.START_ENV, bad)
        with caplog.at_level(logging.WARNING, logger="mcore.h2s_rest"):
            assert hr.rest_window()[0] == 8 * 60 + 30
        assert "HH:MM" in caplog.text

    def test_converts_utc_to_amsterdam(self):
        # 07:00 UTC = 09:00 CEST（10 月 19 日仍是夏令时）
        assert hr.in_rest_window(datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc))
        # 16:00 UTC = 18:00 CEST，窗口外
        assert not hr.in_rest_window(datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# 什么时候轮询
# --------------------------------------------------------------------------- #
class TestGating:
    def test_polls_in_window(self, env):
        delay = _tick(env)
        [w] = _Watcher.instances
        assert w.polls == 1 and w.city_ids == (29,)
        assert delay == pytest.approx(3.0)

    def test_disabled_sends_nothing(self, env, monkeypatch):
        monkeypatch.setenv(hr.ENABLED_ENV, "0")
        assert _tick(env) == hr.IDLE_CHECK_SEC
        assert _Watcher.instances == []

    def test_outside_window_sends_nothing(self, env):
        env.state["in_window"] = False
        assert _tick(env) == hr.IDLE_CHECK_SEC
        assert _Watcher.instances == []

    def test_no_h2s_city_sends_nothing(self, env):
        assert _tick(env, _Cfg([_task("xior", "p01")])) == hr.IDLE_CHECK_SEC
        assert _Watcher.instances == []

    def test_leaving_window_drops_baseline(self, env):
        _tick(env)
        [w] = _Watcher.instances
        env.state["in_window"] = False
        _tick(env)
        assert w.closed
        env.state["in_window"] = True
        _tick(env)
        assert len(_Watcher.instances) == 2      # 新窗口，新基线

    def test_disabling_mid_window_drops_watcher(self, env, monkeypatch):
        _tick(env)
        monkeypatch.setenv(hr.ENABLED_ENV, "0")
        _tick(env)
        assert _Watcher.instances[0].closed

    def test_city_change_rebuilds_watcher(self, env):
        _tick(env)
        _tick(env, _Cfg([H2S_EHV, _task("holland2stay", "25")]))
        assert _Watcher.instances[0].closed
        assert _Watcher.instances[1].city_ids == (25, 29)

    def test_same_cities_reuse_watcher(self, env):
        _tick(env)
        _tick(env)
        assert len(_Watcher.instances) == 1
        assert _Watcher.instances[0].polls == 2


# --------------------------------------------------------------------------- #
# 叫醒
# --------------------------------------------------------------------------- #
class TestWake:
    def test_new_ids_set_wake_and_become_due(self, env):
        _tick(env)
        _Watcher.instances[0].results.append([{"id": "a-1", "name": "A 1", "price": 900}])
        _tick(env)
        assert env.trig.wake.is_set()
        assert env.trig.has_due()
        assert env.trig.begin_round() == ["a-1"]

    def test_nothing_new_does_not_wake(self, env):
        _tick(env)
        _tick(env)
        assert not env.trig.wake.is_set()
        assert not env.trig.has_due()

    def test_already_pending_id_does_not_rewake(self, env):
        env.trig.note_new([{"id": "a-1"}])
        env.trig.wake.clear()
        env.trig.note_new([{"id": "a-1"}])
        assert not env.trig.wake.is_set()

    def test_begin_round_does_not_remove(self, env):
        env.trig.note_new([{"id": "a-1"}])
        env.trig.begin_round()
        assert "a-1" in env.trig.pending      # 本轮抛异常时留给下一轮


# --------------------------------------------------------------------------- #
# 核对与补抓
# --------------------------------------------------------------------------- #
def _round(env, ids, rows, *, started=None):
    st = _Storage()
    st.rows.update(rows)
    env.trig.end_round(ids, st, started or datetime.now(timezone.utc))


def _row(status="Available to book", *, ago=0.0):
    return {"status": status,
            "last_seen": (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat()}


class TestVerify:
    def test_seen_this_round_is_closed(self, env, caplog):
        env.trig.note_new([{"id": "a-1", "name": "A 1"}])
        with caplog.at_level(logging.INFO, logger="mcore.h2s_rest"):
            _round(env, ["a-1"], {"a-1": _row()})
        assert env.trig.pending == {}
        assert env.scheduled == []
        assert "REST 发现 → 主抓取入库" in caplog.text

    def test_missing_row_schedules_retry(self, env):
        env.trig.note_new([{"id": "a-1"}])
        _round(env, ["a-1"], {})
        assert env.scheduled == [hr.RETRY_DELAYS[0]]
        assert not env.trig.has_due()
        env.clock.t += hr.RETRY_DELAYS[0]
        assert env.trig.begin_round() == ["a-1"]

    def test_stale_row_is_not_seen(self, env):
        """库里早有这条（上次放出时写的），但这轮没刷新：不算看到。"""
        env.trig.note_new([{"id": "a-1"}])
        started = datetime.now(timezone.utc)
        _round(env, ["a-1"], {"a-1": _row(ago=3600)}, started=started)
        assert "a-1" in env.trig.pending

    def test_non_available_status_is_not_seen(self, env):
        env.trig.note_new([{"id": "a-1"}])
        _round(env, ["a-1"], {"a-1": _row("Reserved")})
        assert "a-1" in env.trig.pending

    def test_gives_up_after_all_retries(self, env, caplog):
        env.trig.note_new([{"id": "a-1", "name": "A 1"}])
        with caplog.at_level(logging.WARNING, logger="mcore.h2s_rest"):
            for _ in range(len(hr.RETRY_DELAYS) + 1):
                _round(env, ["a-1"], {})
        assert env.trig.pending == {}
        assert env.scheduled == list(hr.RETRY_DELAYS)
        assert "放弃补抓" in caplog.text

    def test_only_listed_ids_are_checked(self, env):
        env.trig.note_new([{"id": "a-1"}, {"id": "a-2"}])
        _round(env, ["a-1"], {"a-1": _row()})
        assert list(env.trig.pending) == ["a-2"]

    def test_storage_error_counts_as_not_seen(self, env):
        class _Boom:
            def get_listing(self, lid):
                raise RuntimeError("db locked")
        env.trig.note_new([{"id": "a-1"}])
        env.trig.end_round(["a-1"], _Boom(), datetime.now(timezone.utc))
        assert env.trig.pending["a-1"]["attempts"] == 1

    def test_one_timer_per_distinct_delay(self, env):
        env.trig.note_new([{"id": "a-1"}, {"id": "a-2"}])
        _round(env, ["a-1", "a-2"], {})
        assert env.scheduled == [hr.RETRY_DELAYS[0]]


# --------------------------------------------------------------------------- #
# 失败汇总
# --------------------------------------------------------------------------- #
class TestFailureLogging:
    def test_first_failure_and_recovery_are_logged_once(self, env, caplog):
        _tick(env)
        w = _Watcher.instances[0]
        w.last_error = "HTTP 500"
        with caplog.at_level(logging.INFO, logger="mcore.h2s_rest"):
            for _ in range(5):
                _tick(env)
            w.last_error = None
            _tick(env)
        assert caplog.text.count("查询失败") == 1
        assert "查询恢复（之前连续失败 5 次）" in caplog.text
        assert env.trig.fail_streak == 0

    def test_long_streak_reports_periodically(self, env, caplog):
        _tick(env)
        _Watcher.instances[0].last_error = "HTTP 500"
        with caplog.at_level(logging.WARNING, logger="mcore.h2s_rest"):
            for _ in range(hr.FAIL_LOG_EVERY):
                _tick(env)
        assert caplog.text.count("查询失败") == 2


# --------------------------------------------------------------------------- #
# run() 不能死
# --------------------------------------------------------------------------- #
def test_run_survives_tick_exception(env, monkeypatch):
    calls = {"n": 0}

    async def boom(cfg):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("bad")

    monkeypatch.setattr(env.trig, "tick", boom)
    monkeypatch.setattr(hr, "IDLE_CHECK_SEC", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(env.trig.run(lambda: env.cfg))
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# 出口
# --------------------------------------------------------------------------- #
class TestRestProxy:
    def test_rotating_session_from_pool(self, monkeypatch):
        import config
        calls = []
        monkeypatch.setattr(config, "is_personal_proxy_active", lambda: False)
        monkeypatch.setattr(config, "get_proxy_url",
                            lambda source="", rotating=False: calls.append((source, rotating)) or "http://x")
        assert hr.rest_proxy() == "http://x"
        assert calls == [(hr.REST_PROXY_SOURCE, True)]

    def test_personal_line_pauses(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "is_personal_proxy_active", lambda: True)
        monkeypatch.setattr(config, "get_proxy_url", lambda *a, **k: "socks5://home:1080")
        assert hr.rest_proxy() == ""

    def test_all_cooling_means_empty(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "is_personal_proxy_active", lambda: False)
        monkeypatch.setattr(config, "get_proxy_url", lambda *a, **k: "")
        assert hr.rest_proxy() == ""

    def test_default_watcher_is_wired_to_rest_proxy(self, monkeypatch):
        import scrapers.holland2stay_rest as rest
        seen = {}

        class _W:
            def __init__(self, city_ids, **kw):
                seen.update(kw)
        monkeypatch.setattr(rest, "RestWatcher", _W)
        RestTrigger(asyncio.Event())._new_watcher((29,))
        assert seen.get("proxy") is hr.rest_proxy
