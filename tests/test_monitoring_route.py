"""/monitoring 页面 + /api/monitoring 测试。

这是「不 ssh 就能回答数据问题」的落点，所以除了结构，重点守两条：
- **鉴权**：遥测会暴露抓取节奏和失败原因，只给 admin。
- **空库不炸**：全新部署、reset-db 之后都会走到这条路径。
"""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone

from storage import Storage


#: 时间锚。判级带了时间维度（「多久没有完整扫描」），钉死日期会让每一轮都显得
#: 陈旧数年，所有 source 集体报 stale。模块级算一次，好让多个 source 归到同一轮。
_NOW = datetime.now(timezone.utc)


def _seed(st, source, specs):
    """specs 最旧在前，每项 (listings, targets, complete, error_type)。"""
    n = len(specs)
    for i, (l, t, c, e) in enumerate(specs):
        st.record_round_stat(
            round_at=(_NOW - timedelta(minutes=n - 1 - i)).isoformat(), source=source,
            listings=l, targets=t, complete=c, error_type=e,
        )


@pytest.fixture
def seeded(test_app):
    """往测试库里灌三个 source 的遥测：一个正常、一个塌了、一个常态零。"""
    from app.db import storage as _storage
    with test_app.app_context():
        st = _storage()
        _seed(st, "holland2stay", [(284, 6, 6, "")] * 10)
        _seed(st, "ourdomain", [(12, 2, 2, "")] * 7 + [(0, 2, 0, "RateLimitError")] * 3)
        _seed(st, "xior", [(0, 4, 4, "")] * 10)
    return True


class TestAuth:
    def test_page_requires_admin(self, client):
        r = client.get("/monitoring", follow_redirects=False)
        assert r.status_code in (301, 302, 401, 403)

    def test_api_requires_admin(self, client):
        r = client.get("/api/monitoring")
        assert r.status_code in (401, 403)

    def test_admin_can_load_page(self, admin_client):
        r = admin_client.get("/monitoring")
        assert r.status_code == 200


class TestApiShape:
    def test_empty_db_does_not_crash(self, admin_client):
        r = admin_client.get("/api/monitoring")
        assert r.status_code == 200
        body = r.get_json()
        assert body["sources"] == []
        assert body["rounds"] == []
        assert body["status"] == "unknown"

    def test_reports_per_source_status(self, admin_client, seeded):
        body = admin_client.get("/api/monitoring").get_json()
        by_src = {s["source"]: s for s in body["sources"]}
        assert by_src["holland2stay"]["status"] == "ok"
        assert by_src["ourdomain"]["status"] == "down"
        # 常态零可订不该被钉在告警上
        assert by_src["xior"]["status"] == "ok"

    def test_overall_is_worst_source(self, admin_client, seeded):
        assert admin_client.get("/api/monitoring").get_json()["status"] == "down"

    def test_includes_active_alerts(self, admin_client, seeded):
        body = admin_client.get("/api/monitoring").get_json()
        assert "source_down:ourdomain" in [a["key"] for a in body["alerts"]]

    def test_snapshot_does_not_throttle_real_alerts(self, admin_client, seeded, test_app):
        """面板只读，不能把 watchdog 的节流状态推进掉——否则打开一次面板，
        monitor 那边真正的告警就被吃了。"""
        from app.db import storage as _storage
        from mcore import watchdog
        admin_client.get("/api/monitoring")
        with test_app.app_context():
            st = _storage()
            assert [a.key for a in watchdog.poll(st, now=1.0e9)] == ["source_down:ourdomain"]

    def test_rounds_are_grouped_with_all_sources(self, admin_client, seeded):
        body = admin_client.get("/api/monitoring?rounds=3").get_json()
        assert len(body["rounds"]) == 3
        assert {s["source"] for s in body["rounds"][0]["sources"]} == {
            "holland2stay", "ourdomain", "xior",
        }

    def test_params_are_clamped(self, admin_client, seeded):
        for qs in ("rounds=0", "rounds=99999", "rounds=abc", "window=0", "window=abc"):
            r = admin_client.get(f"/api/monitoring?{qs}")
            assert r.status_code == 200

    def test_exposes_timezone_for_display(self, admin_client, seeded):
        """时间戳一律返回 UTC（canonical），由这个字段告诉前端按哪个时区显示。

        面板和 /logs 必须对得上：容器跑在 TZ=Europe/Amsterdam，日志的 asctime
        是本地时间，面板直接渲染 UTC 原文的话夏令时期间会差两小时。
        """
        body = admin_client.get("/api/monitoring").get_json()
        assert body["timezone"]
        # round_at 本身仍是 UTC，没有被服务端就地改写
        assert body["rounds"][0]["round_at"].endswith("+00:00")

    def test_includes_liveness_fields(self, admin_client, seeded):
        body = admin_client.get("/api/monitoring").get_json()
        # 数据健康和存活是两个问题，面板要同时给出，不能只给一个
        assert "monitor_running" in body
        assert "heartbeat_at" in body
        assert "thresholds" in body
