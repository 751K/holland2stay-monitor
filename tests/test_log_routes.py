"""
/api/logs + /api/logs/clear 测试。

最近的"分离错误日志"改造引入了 `?file=monitor|errors` 参数。
关键安全契约：**白名单**，绝不允许任意路径。

测试覆盖：
- 默认 file=monitor 不变（向后兼容）
- ?file=errors 走另一个文件
- ?file=../../etc/passwd 等路径穿越 → 400
- ?file=unknown 未在白名单 → 400
- /api/logs/clear 也走同一套白名单
- guest 被拦
"""
from __future__ import annotations

import pytest


# ─── /api/logs 读取 ──────────────────────────────────────────


class TestApiLogsRead:
    def test_default_file_is_monitor(self, admin_client):
        r = admin_client.get("/api/logs?lines=5")
        assert r.status_code == 200
        body = r.get_json()
        # 文件可能不存在（隔离环境），但响应结构必须正确
        assert body is not None
        # 当文件存在时 file=monitor；不存在时 note 提示
        assert body.get("file") == "monitor" or "note" in body

    def test_explicit_monitor_file(self, admin_client):
        r = admin_client.get("/api/logs?file=monitor&lines=3")
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("file") == "monitor" or "note" in body

    def test_errors_file(self, admin_client):
        r = admin_client.get("/api/logs?file=errors&lines=10")
        assert r.status_code == 200
        body = r.get_json()
        # errors.log 可能尚未生成 → note 提示
        assert body.get("file") == "errors" or "note" in body

    def test_path_traversal_blocked(self, admin_client):
        """`?file=../../etc/passwd` 等绝对路径穿越必须 400。"""
        for evil in [
            "../../etc/passwd",
            "../monitor",
            "/etc/passwd",
            "../",
            "/var/log/auth.log",
        ]:
            r = admin_client.get(f"/api/logs?file={evil}")
            assert r.status_code == 400, f"path traversal not blocked: {evil}"

    def test_unknown_file_key_blocked(self, admin_client):
        r = admin_client.get("/api/logs?file=evil")
        assert r.status_code == 400
        body = r.get_json()
        assert "error" in body
        # 错误响应应当列出合法 key（运维友好）
        assert "allowed" in body["error"].lower() or "unknown" in body["error"].lower()

    def test_lines_parameter_clamped(self, admin_client):
        """lines 参数最大 2000，超大值应被裁剪而不是 500。"""
        r = admin_client.get("/api/logs?lines=999999")
        assert r.status_code == 200

    def test_negative_lines_clamped(self, admin_client):
        """lines=-1 不应该 500，应该被 clamp 到 1。"""
        r = admin_client.get("/api/logs?lines=-1")
        assert r.status_code == 200

    def test_non_integer_lines_falls_back_to_default(self, admin_client):
        r = admin_client.get("/api/logs?lines=abc")
        assert r.status_code == 200


# ─── /api/logs/clear ───────────────────────────────────────


class TestApiLogsClear:
    def test_isolation_prevents_real_log_corruption(self, isolated_data_dir):
        """
        回归保护（P1 修复后）：_LOG_FILES 必须指向 tmp_path，不能指向真实 DATA_DIR。
        否则 /api/logs/clear 会 write_text("") 到 ~/data/monitor.log。
        """
        from app.routes import system as system_route
        for key, path in system_route._LOG_FILES.items():
            assert isolated_data_dir in path.parents, (
                f"_LOG_FILES[{key!r}] 指向 {path}，不在 tmp_path 内 — "
                f"会污染真实日志！"
            )

    def test_clear_default_monitor(self, admin_client, isolated_data_dir):
        # 写一些内容，验证 clear 真的清空了 fake 文件（而不是真实日志）
        from app.routes import system as system_route
        fake_monitor = system_route._LOG_FILES["monitor"]
        fake_monitor.write_text("garbage content\n", encoding="utf-8")
        assert fake_monitor.read_text() == "garbage content\n"

        r = admin_client.post("/api/logs/clear",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("ok") is True
        assert body.get("file") == "monitor"
        # 确认清空生效
        assert fake_monitor.read_text() == ""

    def test_clear_errors_file(self, admin_client):
        r = admin_client.post("/api/logs/clear?file=errors",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("ok") is True
        assert body.get("file") == "errors"

    def test_clear_unknown_file_blocked(self, admin_client):
        r = admin_client.post("/api/logs/clear?file=evil",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400

    def test_clear_path_traversal_blocked(self, admin_client):
        r = admin_client.post(
            "/api/logs/clear?file=../../etc/shadow",
            headers={"X-CSRF-Token": "test_csrf"},
        )
        assert r.status_code == 400

    def test_clear_requires_csrf(self, admin_client):
        r = admin_client.post("/api/logs/clear")
        assert r.status_code == 403


# ─── 鉴权 ────────────────────────────────────────────────


class TestLogRoutesAuth:
    def test_anon_blocked_from_logs_api(self, client):
        r = client.get("/api/logs")
        assert r.status_code == 401

    def test_guest_blocked_from_logs_api(self, guest_client):
        # api_logs 用 admin_api_required → guest 应被拦
        r = guest_client.get("/api/logs")
        assert r.status_code == 403

    def test_anon_blocked_from_logs_clear(self, client):
        r = client.post("/api/logs/clear",
                        headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 401

    def test_guest_blocked_from_logs_clear(self, guest_client):
        r = guest_client.post("/api/logs/clear",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 403

    def test_logs_page_requires_admin(self, client, guest_client, admin_client):
        # 匿名 → 302 /login
        assert client.get("/logs").status_code == 302
        # guest → 302 /（admin_required）
        assert guest_client.get("/logs").status_code == 302
        # admin → 200
        assert admin_client.get("/logs").status_code == 200


# ─── 端到端：写入 errors.log → 通过 API 读出 ──────────────


class TestEndToEndLogReadback:
    """实际写一条日志到 errors.log，验证 API 能读回来。"""

    def test_error_log_written_and_readable(self, admin_client, isolated_data_dir, monkeypatch):
        """
        把 _LOG_FILES 的 'errors' 项指到 tmp_path 下的 errors.log，
        写一行进去，然后通过 API 读出来。

        注意：测试 isolated_data_dir 已经提供了 tmp_path，
        但 system.py 的 _LOG_FILES 在 import 时已经把 DATA_DIR 抓走，
        需要 monkeypatch 重新指向 isolated tmp。
        """
        from app.routes import system as system_route
        fake_errors = isolated_data_dir / "errors.log"
        fake_errors.write_text(
            "2026-05-11 [WARNING] scraper._post_gql:42 429 too many requests\n"
            "2026-05-11 [ERROR] booker.try_book:917 [Test] 预订失败 phase=race_lost\n",
            encoding="utf-8",
        )
        # 替换 _LOG_FILES 中 errors 项
        monkeypatch.setitem(system_route._LOG_FILES, "errors", fake_errors)

        r = admin_client.get("/api/logs?file=errors&lines=10")
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("file") == "errors"
        assert len(body["lines"]) == 2
        assert "429 too many requests" in body["lines"][0]
        assert "预订失败" in body["lines"][1]

    def test_tail_returns_last_n_lines(self, admin_client, isolated_data_dir, monkeypatch):
        """lines=N 应该返回最后 N 行（监控日志查看的核心契约）。"""
        from app.routes import system as system_route
        fake = isolated_data_dir / "monitor.log"
        fake.write_text("\n".join(f"line {i}" for i in range(100)) + "\n", encoding="utf-8")
        monkeypatch.setitem(system_route._LOG_FILES, "monitor", fake)

        r = admin_client.get("/api/logs?lines=5")
        body = r.get_json()
        assert body["lines"] == ["line 95", "line 96", "line 97", "line 98", "line 99"]
