"""轮次遥测（round_stats）存储层测试。

在此之前，一轮抓取的结果只以 ``meta.last_scrape_count`` 这个每轮被覆盖的标量
形式存在，任何关于历史或分 source 的问题都只能 grep 日志。这张表是整套可观测
性方案的地基——健康判定、退化告警、面板全部读它。

因此这里重点守两件事：
1. **写入永不上抛。** 观测组件不该把被观测的抓取带崩。
2. **剪枝自带节流。** monitor 每轮都会调它，没节流就是每 5 分钟一次全表扫描。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from storage import Storage


@pytest.fixture
def st(tmp_path) -> Storage:
    s = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
    yield s
    s.close()


def _fill(st, source, count, *, listings=10, targets=2, complete=2, error_type=""):
    for i in range(count):
        st.record_round_stat(
            round_at=f"2026-08-03T{i:02d}:00:00+00:00",
            source=source, listings=listings, targets=targets,
            complete=complete, error_type=error_type,
        )


class TestRecord:
    def test_roundtrip(self, st):
        assert st.record_round_stat(
            round_at="2026-08-03T10:00:00+00:00", source="xior",
            listings=3, targets=4, complete=4, duration_ms=1500,
        ) is True
        rows = st.recent_round_stats()
        assert len(rows) == 1
        r = rows[0]
        assert (r["source"], r["listings"], r["targets"], r["complete"]) == ("xior", 3, 4, 4)
        assert r["duration_ms"] == 1500
        assert r["error_type"] == ""

    def test_error_msg_is_truncated(self, st):
        st.record_round_stat(
            round_at="2026-08-03T10:00:00+00:00", source="h2s",
            error_type="BlockedError", error_msg="x" * 5000,
        )
        assert len(st.recent_round_stats()[0]["error_msg"]) == 500

    def test_write_failure_never_raises(self, st):
        """DB 挂了也只能返回 False，绝不能把抓取带崩。"""
        st._conn.close()
        assert st.record_round_stat(
            round_at="2026-08-03T10:00:00+00:00", source="xior",
        ) is False

    def test_read_failure_never_raises(self, st):
        st._conn.close()
        assert st.recent_round_stats() == []
        assert st.round_stats_sources() == []
        assert st.recent_rounds_grouped() == []


class TestQueries:
    def test_newest_first(self, st):
        _fill(st, "xior", 5)
        rows = st.recent_round_stats(source="xior")
        assert rows[0]["round_at"] > rows[-1]["round_at"]

    def test_limit_and_source_filter(self, st):
        _fill(st, "xior", 10)
        _fill(st, "ourdomain", 10)
        assert len(st.recent_round_stats(source="xior", limit=3)) == 3
        assert {r["source"] for r in st.recent_round_stats(source="xior")} == {"xior"}

    def test_sources_listing(self, st):
        _fill(st, "xior", 2)
        _fill(st, "holland2stay", 2)
        assert st.round_stats_sources() == ["holland2stay", "xior"]

    def test_sources_include_removed_ones(self, st):
        """答的是「抓过什么」而不是「现在配置了什么」——排查时往往正是刚摘掉的那个。"""
        _fill(st, "ourcampus", 1)
        assert "ourcampus" in st.round_stats_sources()


class TestGrouping:
    def test_limit_counts_rounds_not_rows(self, st):
        """limit=2 要返回 2 **轮**，不是 2 行——每轮有多个 source。"""
        for i in range(5):
            for src in ("xior", "ourdomain", "holland2stay"):
                st.record_round_stat(
                    round_at=f"2026-08-03T{i:02d}:00:00+00:00",
                    source=src, listings=1, targets=1, complete=1,
                )
        rounds = st.recent_rounds_grouped(limit=2)
        assert len(rounds) == 2
        assert all(len(r["sources"]) == 3 for r in rounds)

    def test_totals_and_error_count(self, st):
        st.record_round_stat(round_at="2026-08-03T01:00:00+00:00",
                             source="a", listings=10, targets=1, complete=1)
        st.record_round_stat(round_at="2026-08-03T01:00:00+00:00",
                             source="b", listings=5, targets=1, complete=1)
        st.record_round_stat(round_at="2026-08-03T01:00:00+00:00",
                             source="c", error_type="BlockedError", targets=1)
        rnd = st.recent_rounds_grouped()[0]
        assert rnd["listings"] == 15
        assert rnd["errors"] == 1

    def test_newest_round_first(self, st):
        _fill(st, "xior", 4)
        rounds = st.recent_rounds_grouped()
        assert rounds[0]["round_at"] > rounds[-1]["round_at"]

    def test_empty_db(self, st):
        assert st.recent_rounds_grouped() == []


class TestPrune:
    def test_deletes_beyond_retention(self, st):
        st.record_round_stat(round_at="2020-01-01T00:00:00+00:00", source="old")
        st.record_round_stat(round_at="2099-01-01T00:00:00+00:00", source="new")
        assert st.prune_round_stats(days=30, force=True) == 1
        assert st.round_stats_sources() == ["new"]

    def test_throttled_to_once_per_hour(self, st):
        st.record_round_stat(round_at="2020-01-01T00:00:00+00:00", source="old")
        now = 1_800_000_000.0
        assert st.prune_round_stats(days=30, now=now) == 1
        # 立刻再调：应被节流，一行都不删（此时也确实没得删，看的是提前返回）
        st.record_round_stat(round_at="2020-01-02T00:00:00+00:00", source="old2")
        assert st.prune_round_stats(days=30, now=now + 60) == 0
        assert "old2" in st.round_stats_sources()
        # 一小时后放行
        assert st.prune_round_stats(days=30, now=now + 3601) == 1

    def test_force_bypasses_throttle(self, st):
        now = 1_800_000_000.0
        st.prune_round_stats(days=30, now=now)
        st.record_round_stat(round_at="2020-01-01T00:00:00+00:00", source="old")
        assert st.prune_round_stats(days=30, now=now + 1, force=True) == 1

    def test_prune_failure_never_raises(self, st):
        st._conn.close()
        assert st.prune_round_stats(force=True) == 0


class TestResetAll:
    def test_reset_clears_round_stats(self, st):
        _fill(st, "xior", 3)
        st.reset_all()
        assert st.recent_round_stats() == []
