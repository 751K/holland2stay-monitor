"""/api/logs 服务端过滤测试。

以前 /api/logs 只能 tail，页面上的搜索是纯前端的——只在已拉取的 500 行里找。
所以"凌晨三点发生了什么"这类问题仍然只能 ssh 上去 grep，而那正是可观测性
方案要消掉的东西。

最要紧的一条契约：**按记录过滤，不按行**。traceback 的续行既没有时间戳也没有
级别，逐行过滤会把 traceback 拦腰截断——而 traceback 恰恰是最需要看的部分。
"""
from __future__ import annotations

import pytest

from app.routes.system import _group_records, _parse_log_time, _read_tail


SAMPLE = "\n".join([
    "2026-08-03 03:00:01,100 [INFO] monitor: ===== 第 1 轮 =====",
    "2026-08-03 03:05:02,200 [WARNING] scrapers.xior: 429 too many requests",
    "2026-08-03 03:10:03,300 [ERROR] scrapers.ourdomain: 抓取失败",
    "Traceback (most recent call last):",
    '  File "scrapers/ourdomain.py", line 42, in _fetch',
    "RateLimitError: 429",
    "2026-08-03 04:00:04,400 [INFO] monitor: 本轮结束",
]) + "\n"


# ── 时间解析 ────────────────────────────────────────────────────────


class TestParseLogTime:
    @pytest.mark.parametrize("raw,want", [
        ("2026-08-03", "2026-08-03 00:00:00"),
        ("2026-08-03 03", "2026-08-03 03:00:00"),
        ("2026-08-03 03:05", "2026-08-03 03:05:00"),
        ("2026-08-03 03:05:07", "2026-08-03 03:05:07"),
        ("2026-08-03T03:05", "2026-08-03 03:05:00"),
        ("2026-08-03 03:05:07,123", "2026-08-03 03:05:07"),
    ])
    def test_normalizes(self, raw, want):
        assert _parse_log_time(raw) == want

    @pytest.mark.parametrize("raw", ["", None, "  ", "08-03 03:00", "yesterday", "3pm"])
    def test_invalid_is_none(self, raw):
        """不合法就整个忽略这一维过滤，而不是抛错或返回垃圾。"""
        assert _parse_log_time(raw) is None


# ── 记录分组 ────────────────────────────────────────────────────────


class TestGroupRecords:
    def test_traceback_attaches_to_its_header(self):
        recs = _group_records(SAMPLE)
        assert len(recs) == 4
        assert len(recs[2]["lines"]) == 4          # ERROR 行 + 3 行 traceback
        assert recs[2]["level"] == "ERROR"
        assert "RateLimitError: 429" in recs[2]["lines"][-1]

    def test_parses_ts_and_level(self):
        recs = _group_records(SAMPLE)
        assert recs[0]["ts"] == "2026-08-03 03:00:01"
        assert [r["level"] for r in recs] == ["INFO", "WARNING", "ERROR", "INFO"]

    def test_unformatted_lines_each_become_a_record(self):
        """否则整个非标准格式的文件会并成一条巨型记录，lines 上限彻底失效。"""
        recs = _group_records("alpha\nbeta\ngamma\n")
        assert len(recs) == 3
        assert all(r["ts"] == "" for r in recs)

    def test_orphan_continuation_does_not_swallow_the_rest(self):
        """扫描窗口从半截切进来时，头部在窗口之外的续行不能吞掉后面所有行。"""
        recs = _group_records(
            "  orphan continuation\n"
            "  another orphan\n"
            "2026-08-03 03:00:01,100 [INFO] m: real\n"
        )
        assert len(recs) == 3
        assert recs[2]["level"] == "INFO"

    def test_blank_lines_are_dropped(self):
        assert _group_records("\n\n\n") == []


# ── 尾部读取 ────────────────────────────────────────────────────────


class TestReadTail:
    def test_small_file_read_whole(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text(SAMPLE, encoding="utf-8")
        text, scanned, truncated = _read_tail(p, 1024 * 1024)
        assert text == SAMPLE
        assert truncated is False

    def test_truncated_drops_partial_first_line(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("\n".join(f"line {i:04d}" for i in range(1000)) + "\n", encoding="utf-8")
        text, scanned, truncated = _read_tail(p, 200)
        assert truncated is True
        # 第一行必须是完整的一行，不能是半截
        assert text.split("\n")[0].startswith("line ")
        assert text.rstrip().endswith("line 0999")


# ── 端到端 ──────────────────────────────────────────────────────────


@pytest.fixture
def log_file(admin_client, isolated_data_dir, monkeypatch):
    from app.routes import system as system_route
    p = isolated_data_dir / "monitor.log"
    p.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setitem(system_route._LOG_FILES, "monitor", p)
    return p


class TestApiFiltering:
    def test_no_filter_returns_everything(self, admin_client, log_file):
        body = admin_client.get("/api/logs?lines=500").get_json()
        assert len(body["lines"]) == 7
        assert body["matched"] == 4

    def test_level_filter(self, admin_client, log_file):
        body = admin_client.get("/api/logs?level=ERROR").get_json()
        assert body["matched"] == 1
        # 整条记录都要带上，traceback 不能被截断
        assert len(body["lines"]) == 4
        assert "RateLimitError" in body["lines"][-1]

    def test_multiple_levels(self, admin_client, log_file):
        body = admin_client.get("/api/logs?level=ERROR,WARNING").get_json()
        assert body["matched"] == 2

    def test_keyword_matches_inside_traceback(self, admin_client, log_file):
        """关键字要在整条记录里找——报错的线索多半在 traceback 里，不在头部行。"""
        body = admin_client.get("/api/logs?q=RateLimitError").get_json()
        assert body["matched"] == 1
        assert body["lines"][0].startswith("2026-08-03 03:10:03")

    def test_keyword_is_case_insensitive(self, admin_client, log_file):
        assert admin_client.get("/api/logs?q=ratelimiterror").get_json()["matched"] == 1

    def test_time_range(self, admin_client, log_file):
        body = admin_client.get(
            "/api/logs?since=2026-08-03 03:04&until=2026-08-03 03:11"
        ).get_json()
        assert body["matched"] == 2

    def test_since_only(self, admin_client, log_file):
        body = admin_client.get("/api/logs?since=2026-08-03 04:00").get_json()
        assert body["matched"] == 1
        assert "本轮结束" in body["lines"][0]

    def test_combined_filters(self, admin_client, log_file):
        body = admin_client.get(
            "/api/logs?level=INFO&since=2026-08-03 03:30"
        ).get_json()
        assert body["matched"] == 1

    def test_no_match_reports_zero_not_error(self, admin_client, log_file):
        body = admin_client.get("/api/logs?q=nonexistent-string").get_json()
        assert body["lines"] == []
        assert body["matched"] == 0
        assert "error" not in body

    def test_returns_scan_metadata(self, admin_client, log_file):
        """命中 0 条时，「没有这条日志」和「没扫到那么远」是天差地别的两件事。"""
        body = admin_client.get("/api/logs").get_json()
        assert body["truncated"] is False
        assert body["scanned_bytes"] > 0
        assert body["records"] == 4

    def test_lines_limit_counts_records(self, admin_client, log_file):
        body = admin_client.get("/api/logs?lines=1").get_json()
        assert body["returned"] == 1
        assert body["matched"] == 4
        assert "本轮结束" in body["lines"][0]     # 最后一条记录

    def test_invalid_time_is_ignored_not_fatal(self, admin_client, log_file):
        body = admin_client.get("/api/logs?since=not-a-time").get_json()
        assert body["matched"] == 4

    def test_scan_param_is_clamped(self, admin_client, log_file):
        for scan in ("0", "-5", "99999", "abc"):
            r = admin_client.get(f"/api/logs?scan={scan}")
            assert r.status_code == 200

    def test_filters_still_enforce_whitelist(self, admin_client, log_file):
        r = admin_client.get("/api/logs?file=../../etc/passwd&q=root")
        assert r.status_code == 400
