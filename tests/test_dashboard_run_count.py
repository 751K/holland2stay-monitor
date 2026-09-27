"""仪表盘「平均每轮条数」：monitor.log 上限从 2MB 提到 100MB 之后的读法。

原实现 ``read_text().splitlines()`` 每次打开仪表盘都整份读入内存。改成从尾部
倒着读、到 7 天窗口起点就停。这里锁三件事：结果与整份读一致、块边界上的行
不会被切坏、窗口之外的内容根本不读。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services import dashboard_service as ds
from mstorage._notifications import WEB_NOTIFICATIONS_KEEP


def _line(ts: datetime, count: int) -> str:
    return (f"{ts:%Y-%m-%d %H:%M:%S},123 [INFO] monitor: "
            f"本次抓取共 {count} 条房源")


def _write(tmp_path, monkeypatch, lines):
    (tmp_path / "monitor.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(ds, "DATA_DIR", tmp_path)


def test_only_counts_rounds_inside_window(tmp_path, monkeypatch):
    now = datetime.now()
    _write(tmp_path, monkeypatch, [
        _line(now - timedelta(days=9), 1000),     # 窗口外
        _line(now - timedelta(days=3), 10),
        f"{now - timedelta(days=2):%Y-%m-%d %H:%M:%S},000 [INFO] x: 无关行",
        "  File \"x.py\", line 1, in <module>",   # traceback 续行没有时间戳，不能触发停止
        _line(now - timedelta(hours=1), 20),
    ])
    assert ds._avg_run_count(days=7) == (15, 2)


def test_matches_full_read_across_chunk_boundaries(tmp_path, monkeypatch):
    now = datetime.now()
    lines = [_line(now - timedelta(minutes=500 - i), i) for i in range(500)]
    _write(tmp_path, monkeypatch, lines)
    got = list(ds._iter_lines_reversed(tmp_path / "monitor.log", chunk=37))
    assert got == list(reversed(lines))        # 小块强制每行都可能跨边界
    assert ds._avg_run_count(days=7) == (round(sum(range(500)) / 500), 500)


def test_stops_reading_before_old_part(tmp_path, monkeypatch):
    """窗口之前的 2 万行不读：读到第一条越界的行就停。"""
    now = datetime.now()
    old = [_line(now - timedelta(days=30, seconds=i), 999) for i in range(20000)]
    new = [_line(now - timedelta(hours=2), 7), _line(now - timedelta(hours=1), 9)]
    _write(tmp_path, monkeypatch, old + new)

    seen = 0
    real = ds._iter_lines_reversed

    def counting(path, chunk=1 << 20):
        nonlocal seen
        for ln in real(path, chunk=4096):
            seen += 1
            yield ln

    monkeypatch.setattr(ds, "_iter_lines_reversed", counting)
    assert ds._avg_run_count(days=7) == (8, 2)
    assert seen == 3        # 两条新行 + 第一条越界的旧行


def test_missing_log_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "DATA_DIR", tmp_path)
    assert ds._avg_run_count(days=7, fallback=42) == (42, 0)


def test_notification_keep_is_2000_and_monitor_uses_it():
    from pathlib import Path
    assert WEB_NOTIFICATIONS_KEEP == 2000
    src = (Path(__file__).resolve().parent.parent / "monitor.py").read_text(encoding="utf-8")
    assert "prune_notifications(keep=WEB_NOTIFICATIONS_KEEP)" in src
    assert "maxBytes=100 * 1024 * 1024" in src
