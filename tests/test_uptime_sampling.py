"""
7 天 uptime% 采样测试（Storage.record_uptime_sample / uptime_percent_7d）。

旧方案存单个 monitor_started_at，超 7 天后重启会被覆盖 → 掉到 1%，且不感知
宕机。新方案每小时记一个存活样本（持久 + 累加），抗重启、真实反映宕机。
"""
from __future__ import annotations

import pytest

from storage import Storage


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "uptime.db", timezone_str="UTC")
    yield s
    s.close()


_HOUR = 3600


class TestUptimeSampling:
    def test_empty_returns_zero(self, st):
        assert st.uptime_percent_7d() == 0

    def test_single_sample_is_one_of_168(self, st):
        now = 1_000_000 * _HOUR  # 任意整点
        st.record_uptime_sample(now=now)
        # 1 / 168 ≈ 0.6% → round → 1
        assert st.uptime_percent_7d(now=now) == round(100 / 168)

    def test_idempotent_within_same_hour(self, st):
        now = 1_000_000 * _HOUR
        st.record_uptime_sample(now=now)
        st.record_uptime_sample(now=now + 60)      # 同一小时
        st.record_uptime_sample(now=now + 1800)    # 同一小时
        # 仍只记 1 个小时
        assert st.uptime_percent_7d(now=now) == round(100 / 168)

    def test_full_168_hours_is_100(self, st):
        base = 1_000_000 * _HOUR
        for h in range(168):
            st.record_uptime_sample(now=base + h * _HOUR)
        # 在最后一个小时看：过去 168h 全有样本 → 100%
        assert st.uptime_percent_7d(now=base + 167 * _HOUR) == 100

    def test_half_uptime_is_about_50(self, st):
        """隔小时记样本（模拟一半时间宕机）→ ~50%。"""
        base = 1_000_000 * _HOUR
        for h in range(0, 168, 2):   # 84 个样本
            st.record_uptime_sample(now=base + h * _HOUR)
        pct = st.uptime_percent_7d(now=base + 167 * _HOUR)
        assert 49 <= pct <= 51

    def test_old_samples_pruned_outside_window(self, st):
        base = 1_000_000 * _HOUR
        # 记一个很老的样本（8 天前）
        st.record_uptime_sample(now=base)
        # 8 天后再记一个
        later = base + 8 * 24 * _HOUR
        st.record_uptime_sample(now=later)
        # 老样本已被剪掉，窗口内只有 1 个
        assert st.uptime_percent_7d(now=later) == round(100 / 168)

    def test_survives_reopen(self, tmp_path):
        """模拟"重启/重建"——重开同一 DB 文件，样本仍在（持久）。"""
        db = tmp_path / "persist.db"
        now = 1_000_000 * _HOUR
        s1 = Storage(db, timezone_str="UTC")
        for h in range(10):
            s1.record_uptime_sample(now=now + h * _HOUR)
        before = s1.uptime_percent_7d(now=now + 9 * _HOUR)
        s1.close()

        # 重开（等价于容器重建后挂同一个 volume 的 DB）
        s2 = Storage(db, timezone_str="UTC")
        after = s2.uptime_percent_7d(now=now + 9 * _HOUR)
        s2.close()

        assert after == before
        assert after == round(10 * 100 / 168)

    def test_corrupt_meta_returns_zero(self, st):
        st.set_meta("uptime_alive_hours", "not-json{{{")
        assert st.uptime_percent_7d() == 0
        # 记一次能自愈（覆盖坏值）
        now = 1_000_000 * _HOUR
        st.record_uptime_sample(now=now)
        assert st.uptime_percent_7d(now=now) == round(100 / 168)
