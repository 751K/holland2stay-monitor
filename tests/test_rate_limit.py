"""app.rate_limit.SlidingWindow 的行为。

Web 侧所有内存限流共用它：窗口裁剪、只检查不记录、以及空闲键清理。
"""

from app import rate_limit
from app.rate_limit import SlidingWindow


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _window(window: float = 10.0):
    clock = FakeClock()
    return SlidingWindow("test", window, clock=clock), clock


class TestAllow:
    def test_allows_up_to_limit_then_denies(self):
        w, _ = _window()
        assert w.allow("ip", 2) is True
        assert w.allow("ip", 2) is True
        assert w.allow("ip", 2) is False

    def test_keys_are_independent(self):
        w, _ = _window()
        assert w.allow("a", 1) is True
        assert w.allow("b", 1) is True
        assert w.allow("a", 1) is False

    def test_recovers_after_window(self):
        w, clock = _window(window=10.0)
        assert w.allow("ip", 1) is True
        assert w.allow("ip", 1) is False
        clock.now = 11.0
        assert w.allow("ip", 1) is True


class TestCheckAndRecord:
    def test_would_allow_does_not_record(self):
        w, _ = _window()
        for _ in range(5):
            assert w.would_allow("ip", 3) is True
        assert w.hits("ip") == []

    def test_record_then_check(self):
        w, _ = _window()
        w.record("ip")
        assert len(w.hits("ip")) == 1
        assert w.would_allow("ip", 2) is True
        w.record("ip")
        assert w.would_allow("ip", 2) is False

    def test_hits_prune_expired(self):
        w, clock = _window(window=10.0)
        w.record("ip")
        clock.now = 5.0
        w.record("ip")
        clock.now = 12.0
        assert len(w.hits("ip")) == 1

    def test_clear_removes_key(self):
        w, _ = _window()
        w.record("ip")
        w.clear("ip")
        assert w.hits("ip") == []


class TestSweep:
    def test_idle_keys_are_removed_after_sweep_interval(self):
        w, clock = _window(window=10.0)
        w.allow("idle", 1)

        clock.now = rate_limit.SWEEP_INTERVAL_SEC + 1
        w.hits("other")  # 任意访问触发清理

        assert "idle" not in w._hits
