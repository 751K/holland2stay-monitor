"""分轮抓取（task sharding）测试。

为什么需要分片：Xior 的请求间隔是 5s（限流按速率算，调小会直接撞回 429，
见 commit 4d71b9d），实测**每栋楼 13.9 秒**。官方注册表 30 栋 ≈ 417 秒/轮，
而 CHECK_INTERVAL 是 300 秒；更糟的是 H2S 排在其它 source **之后**执行，
不分片等于每轮把真正出房源的那个 source 推迟 7 分钟。

这里守两条：
1. **轮转必须覆盖全部 target**，不能有楼栋被系统性漏掉。
2. **任何异常都回退成全量**，宁可慢一轮，也不能悄悄少抓。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from monitor import _apply_task_sharding, _shard_source_tasks
from storage import Storage


class _Task:
    def __init__(self, source: str, name: str) -> None:
        self.source = source
        self.city_display = name

    def __repr__(self) -> str:
        return f"{self.source}:{self.city_display}"


def _tasks(source: str, n: int, prefix: str = "B") -> list[_Task]:
    return [_Task(source, f"{prefix}{i}") for i in range(n)]


class _Cfg:
    def __init__(self, sizes):
        self.shard_sizes = sizes


@pytest.fixture
def st(tmp_path):
    s = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
    yield s
    s.close()


# ── 纯轮转规则 ──────────────────────────────────────────────────────


class TestShardRotation:
    def test_covers_everything_without_repeats(self):
        ts, cur, seen = _tasks("xior", 30), 0, []
        for _ in range(6):
            picked, cur = _shard_source_tasks(ts, "xior", 5, cur)
            seen += [t.city_display for t in picked]
        assert len(seen) == 30
        assert len(set(seen)) == 30, "6 轮必须正好覆盖 30 栋且不重复"
        assert cur == 0, "整除时应回到起点"

    def test_wraps_around_on_uneven_division(self):
        """target 数不是 size 整数倍时也要均匀，不能让末尾几个总排同一轮。"""
        from collections import Counter
        ts, cur, seen = _tasks("xior", 7), 0, []
        for _ in range(7):
            picked, cur = _shard_source_tasks(ts, "xior", 5, cur)
            seen += [t.city_display for t in picked]
        assert set(Counter(seen).values()) == {5}, "7 轮后每个 target 次数应相同"

    def test_no_sharding_when_size_ge_targets(self):
        """配了「每轮 5 个」但只有 4 个 target 时，行为必须与没配过一样。"""
        ts = _tasks("xior", 4)
        picked, cur = _shard_source_tasks(ts, "xior", 5, 3)
        assert picked == ts and cur == 0

    def test_size_zero_disables(self):
        ts = _tasks("xior", 30)
        picked, cur = _shard_source_tasks(ts, "xior", 0, 7)
        assert picked == ts and cur == 0

    def test_empty_task_list(self):
        assert _shard_source_tasks([], "xior", 5, 3) == ([], 0)

    def test_out_of_range_cursor_is_wrapped(self):
        """游标可能因为 target 数变少而越界（改了 XIOR_CITIES）。"""
        ts = _tasks("xior", 10)
        picked, _ = _shard_source_tasks(ts, "xior", 3, 999)
        assert len(picked) == 3


# ── 与 storage / cfg 的集成 ─────────────────────────────────────────


class TestApplySharding:
    def test_only_configured_source_is_sharded(self, st):
        tasks = _tasks("xior", 30) + _tasks("holland2stay", 10, "C")
        out = _apply_task_sharding(tasks, _Cfg({"xior": 5}), st)
        assert sum(1 for t in out if t.source == "xior") == 5
        assert sum(1 for t in out if t.source == "holland2stay") == 10

    def test_cursor_advances_across_rounds(self, st):
        tasks = _tasks("xior", 30)
        cfg = _Cfg({"xior": 5})
        first = _apply_task_sharding(tasks, cfg, st)
        second = _apply_task_sharding(tasks, cfg, st)
        assert {t.city_display for t in first} != {t.city_display for t in second}

    def test_cursor_survives_restart(self, tmp_path):
        """游标存 meta 而不是内存：每次重启都从第一片开始的话，
        后面的楼栋会被系统性少抓。"""
        db = Path(tmp_path) / "t.db"
        tasks, cfg = _tasks("xior", 30), _Cfg({"xior": 5})
        st1 = Storage(db, timezone_str="UTC")
        try:
            first = _apply_task_sharding(tasks, cfg, st1)
        finally:
            st1.close()
        st2 = Storage(db, timezone_str="UTC")   # 「重启」
        try:
            second = _apply_task_sharding(tasks, cfg, st2)
        finally:
            st2.close()
        assert {t.city_display for t in first} != {t.city_display for t in second}

    def test_dry_run_does_not_advance_cursor(self, st):
        tasks, cfg = _tasks("xior", 30), _Cfg({"xior": 5})
        a = _apply_task_sharding(tasks, cfg, st, dry_run=True)
        b = _apply_task_sharding(tasks, cfg, st, dry_run=True)
        assert [t.city_display for t in a] == [t.city_display for t in b]

    def test_no_config_is_passthrough(self, st):
        tasks = _tasks("xior", 30)
        assert _apply_task_sharding(tasks, _Cfg({}), st) is tasks

    def test_missing_attr_is_tolerated(self, st):
        class _Bare:
            pass
        tasks = _tasks("xior", 30)
        assert _apply_task_sharding(tasks, _Bare(), st) is tasks

    def test_storage_failure_falls_back_to_full_scan(self, st):
        """宁可这一轮慢，也不能悄悄漏抓楼栋。"""
        st._conn.close()
        tasks = _tasks("xior", 30)
        out = _apply_task_sharding(tasks, _Cfg({"xior": 5}), st)
        assert len(out) == 30


# ── 配置解析 ────────────────────────────────────────────────────────


class TestShardConfig:
    def test_default_shards_xior_only(self, monkeypatch):
        monkeypatch.delenv("SHARD_SIZES", raising=False)
        monkeypatch.setenv("SOURCES", "holland2stay")
        import config
        assert config.load_config().shard_sizes == {"xior": 5}

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv("SHARD_SIZES", "xior:8,ourdomain:2")
        monkeypatch.setenv("SOURCES", "holland2stay")
        import config
        sizes = config.load_config().shard_sizes
        assert sizes["xior"] == 8 and sizes["ourdomain"] == 2

    def test_zero_disables_a_source(self, monkeypatch):
        monkeypatch.setenv("SHARD_SIZES", "xior:0")
        monkeypatch.setenv("SOURCES", "holland2stay")
        import config
        assert config.load_config().shard_sizes["xior"] == 0

    def test_malformed_entries_are_ignored_not_fatal(self, monkeypatch):
        """配置写错不该让监控起不来。"""
        monkeypatch.setenv("SHARD_SIZES", "garbage,xior:abc,:5,ourdomain:3")
        monkeypatch.setenv("SOURCES", "holland2stay")
        import config
        sizes = config.load_config().shard_sizes
        assert sizes["ourdomain"] == 3
        assert sizes["xior"] == 5     # 非法值不覆盖默认
