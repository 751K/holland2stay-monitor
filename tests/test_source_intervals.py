"""按 source 节流：控制「同一个 target 多久被打一次」。

和分片解决的**不是同一个问题**——分片管「每轮抓几个 target」。2026-08-04
生产实测把两者混为一谈的代价：

Xior 从 30 栋缩到 4 栋后，分片 3/轮 等于每栋楼几乎每轮都被抓；而高峰时段轮次
间隔只有 60–90 秒，于是单栋楼的请求频率从「每 10–15 分钟一次」涨到「每 60–90
秒一次」，约 10 倍，直接撞进限流：持续 429、退避 30s+60s，单轮从 40 秒拖到
270 秒。

**楼栋数变少反而更容易被限流**，因为限流按单个 target 被打的频率算——30 栋
轮着抓时每栋自然稀疏，4 栋轮着抓就全挤在一起了。分片再怎么调都救不了，它压根
不控制频率。
"""
from __future__ import annotations

import pytest

from monitor import _SOURCE_LAST_SCRAPE_PREFIX, _apply_source_intervals


class _Task:
    def __init__(self, source, city="c"):
        self.source = source
        self.city_display = city


class _Cfg:
    def __init__(self, intervals):
        self.source_min_intervals = intervals


class _Meta:
    """只实现 get_meta / set_meta 的假 storage。"""

    def __init__(self, initial=None, fail_read=False, fail_write=False):
        self.data = dict(initial or {})
        self.fail_read = fail_read
        self.fail_write = fail_write

    def get_meta(self, key, default=""):
        if self.fail_read:
            raise RuntimeError("meta 读挂了")
        return self.data.get(key, default)

    def set_meta(self, key, value):
        if self.fail_write:
            raise RuntimeError("meta 写挂了")
        self.data[key] = value


def _tasks():
    return [_Task("xior", "A"), _Task("xior", "B"), _Task("holland2stay", "E")]


class TestThrottling:
    def test_skips_a_source_scraped_too_recently(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1100)
        assert [t.source for t in out] == ["holland2stay"], "xior 应被整体跳过"

    def test_runs_once_the_gap_has_passed(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1700)
        assert sorted({t.source for t in out}) == ["holland2stay", "xior"]

    def test_other_sources_are_untouched(self):
        """节流是逐 source 的——H2S 靠高频轮次出房源，不能被 Xior 拖累。"""
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1001)
        assert [t.city_display for t in out] == ["E"]

    def test_first_ever_round_is_not_skipped(self):
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), _Meta(), now=50)
        assert len(out) == 3

    def test_zero_disables_throttling(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 0}), st, now=1001)
        assert len(out) == 3

    def test_no_config_is_a_no_op(self):
        assert len(_apply_source_intervals(_tasks(), _Cfg({}), _Meta(), now=1)) == 3


class TestTimestampPersistence:
    def test_records_the_time_it_actually_ran(self):
        st = _Meta()
        _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1234)
        assert st.data[_SOURCE_LAST_SCRAPE_PREFIX + "xior"] == "1234"

    def test_skipped_round_does_not_refresh_the_timestamp(self):
        """否则每次跳过都把闸门往后推，source 会被永久饿死。"""
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1100)
        assert st.data[_SOURCE_LAST_SCRAPE_PREFIX + "xior"] == "1000"

    def test_dry_run_does_not_write(self):
        st = _Meta()
        _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=5, dry_run=True)
        assert st.data == {}


class TestFailOpen:
    """读写 meta 出问题时宁可多抓一轮，也不能把整个 source 静默停掉。"""

    def test_unreadable_meta_still_scrapes(self):
        out = _apply_source_intervals(
            _tasks(), _Cfg({"xior": 600}), _Meta(fail_read=True), now=1,
        )
        assert len(out) == 3

    def test_unwritable_meta_still_scrapes(self):
        out = _apply_source_intervals(
            _tasks(), _Cfg({"xior": 600}), _Meta(fail_write=True), now=1,
        )
        assert len(out) == 3

    def test_garbage_timestamp_is_treated_as_never(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "not-a-number"})
        assert len(_apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1)) == 3

    def test_future_timestamp_does_not_wedge_the_source(self):
        """时钟回拨过一次，不该让 source 卡到时间追上为止。"""
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "99999999"})
        assert len(_apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1000)) == 3


class TestDefaults:
    def test_xior_is_throttled_by_default(self):
        """默认值要能直接修好生产上那个问题，不该等人去配。

        下界守的是「明显比轮次间隔松」——实测每栋楼 60–90 秒一次会持续吃
        429，单轮从 40 秒拖到 274 秒。具体数值会按实测继续调，所以这里不钉
        死某个数。
        """
        from config import _DEFAULT_SOURCE_MIN_INTERVALS

        assert _DEFAULT_SOURCE_MIN_INTERVALS.get("xior", 0) >= 180

    def test_other_sources_have_no_default_throttle(self):
        """H2S 是真正出房源的那个，高峰高频轮询是有意为之。"""
        from config import _DEFAULT_SOURCE_MIN_INTERVALS

        assert "holland2stay" not in _DEFAULT_SOURCE_MIN_INTERVALS
