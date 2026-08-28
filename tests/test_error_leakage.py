"""
旧路由（``/api/*``，非 v1）的 500 分支不得把异常文本回给客户端。

api_errors.py 的模块文档早就写了这条规则，但只有 v1 层照做了。旧路由一路
``except Exception as e: jsonify({"error": str(e)})``，两个后果：

1. 异常文本进了响应体。sqlite3.OperationalError 带库路径，OSError 带日志路径，
   Telegram / Twilio SDK 的异常带 token 片段和内部 URL。
2. 更要命的是异常被吞了——catch 之后 Flask 不再记录，traceback 直接消失。
   线上出问题时日志里什么都没有，只有管理员浏览器里留下一行字。

所以每条用例同时断言两件事：响应里没有异常文本，日志里有。

用一个哨兵字符串当探针。它长得像真正会泄漏的东西（路径 + 内部标识），
所以一旦某天有人把 str(e) 加回去，断言会立刻炸。
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

# 只要它出现在响应体里，就说明异常文本被原样透传了。
CANARY = "/srv/secret/flatradar.db: internal-token-abc123"


def _boom(*_a, **_kw):
    raise RuntimeError(CANARY)


def _assert_clean(resp, caplog, *, status=500):
    """响应里没有哨兵，日志里有哨兵和 traceback。"""
    assert resp.status_code == status
    body = resp.get_data(as_text=True)
    assert CANARY not in body, f"异常文本泄漏进响应体：{body[:300]}"
    assert "internal-token" not in body
    assert "/srv/secret" not in body

    logged = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert CANARY in logged, "异常被吞了——日志里没有异常文本"
    assert "RuntimeError" in logged, "日志里没有 traceback"


@pytest.fixture
def caplog_exc(caplog):
    caplog.set_level(logging.ERROR)
    return caplog


# ── control.py：监控进程控制 ────────────────────────────────

class TestControlRoutes:
    @pytest.mark.parametrize("url,target", [
        ("/api/reload",          "reload_monitor"),
        ("/api/monitor/start",   "start_monitor"),
        ("/api/monitor/stop",    "stop_monitor"),
        ("/api/monitor/restart", "restart_monitor"),
    ])
    def test_no_leak(self, admin_client, caplog_exc, url, target):
        with patch(f"app.routes.control.{target}", _boom):
            r = admin_client.post(url, headers={"X-CSRF-Token": "test_csrf"})
        _assert_clean(r, caplog_exc)
        assert r.get_json()["ok"] is False
        assert r.get_json()["error"] == "服务器内部错误"

    def test_typed_error_still_speaks(self, admin_client, monkeypatch):
        """对照组：MonitorServiceError 的文案是我们自己写的，必须原样保留。

        修的是「异常文本直出」，不是「所有错误都变成一句服务器内部错误」。
        管理员点 Start 时看到「监控已在运行」，比看到 500 有用得多。
        """
        from app.services import monitor_service as svc
        monkeypatch.setattr(svc, "monitor_pid", lambda: 12345)
        r = admin_client.post("/api/monitor/start", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 409
        assert "监控已在运行" in r.get_json()["error"]


# ── system.py：日志 / 崩溃报告 / reset-db / 公告 ──────────────

class TestSystemRoutes:
    def test_logs_no_leak(self, admin_client, caplog_exc):
        """/api/logs 的壳形与其它路由不同——前端无条件读 lines/size。"""
        from app.routes import system
        # 日志文件不存在时路由在 try 之前就 200 返回了，异常根本不会抛。
        system._LOG_FILES["monitor"].write_text("x\n", encoding="utf-8")
        with patch("app.routes.system._read_tail", _boom):
            r = admin_client.get("/api/logs")
        _assert_clean(r, caplog_exc)
        body = r.get_json()
        assert body["lines"] == [] and body["size"] == 0, "壳形被改坏了，前端会抛"
        assert body["error"] == "读取日志失败"

    def test_logs_clear_no_leak(self, admin_client, caplog_exc):
        from app.routes import system
        system._LOG_FILES["monitor"].write_text("x\n", encoding="utf-8")
        with patch("pathlib.Path.write_text", _boom):
            r = admin_client.post("/api/logs/clear",
                                  headers={"X-CSRF-Token": "test_csrf"})
        _assert_clean(r, caplog_exc)

    def test_reset_db_no_leak(self, admin_client, caplog_exc):
        """sqlite3 的异常文本会带上数据库文件的绝对路径。"""
        # system.py 是 ``from app.db import storage``——必须打它自己的名字。
        with patch("app.routes.system.storage") as st:
            st.return_value.reset_all.side_effect = _boom
            r = admin_client.post("/api/reset-db", json={"confirm": True},
                                  headers={"X-CSRF-Token": "test_csrf"})
        _assert_clean(r, caplog_exc)

    def test_announcement_no_leak(self, admin_client, caplog_exc):
        with patch("app.services.announcement_service.broadcast", _boom):
            r = admin_client.post("/api/announcement",
                                  json={"title": "t", "body": "b", "dry_run": True},
                                  headers={"X-CSRF-Token": "test_csrf"})
        _assert_clean(r, caplog_exc)

    def test_announcement_validation_still_speaks(self, admin_client):
        """对照组：标题为空是用户能自己修的错，文案必须保留。"""
        r = admin_client.post("/api/announcement",
                              json={"title": "", "body": "b"},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400
        assert "标题" in r.get_json()["error"]


# ── 非管理员也够得着的两处 ──────────────────────────────────
#
# 上面那些全在 @admin_api_required 后面——泄漏的路径管理员本来就在界面上
# 看得见。下面这两处不是：普通用户就能触发，异常文本还来自第三方 SDK，
# 内容完全不受我们控制。真正的越权面在这里。

class TestUserReachableRoutes:
    def test_test_notify_no_leak(self, admin_client, caplog_exc, test_app):
        """/users/<id>/test 是 @self_or_admin_required。

        异常来自 Telegram / Twilio SDK，文本里可能带 bot token 片段。
        """
        # 必须真的配一个渠道，否则路由在 for 循环里就 continue 掉了，
        # except 分支根本不执行——那样这条用例会「通过」但什么都没测到。
        r = admin_client.post("/users/new", data={
            "csrf_token": "test_csrf",
            "name": "LeakProbe",
            "enabled": "true",
            "NOTIFICATIONS_ENABLED": "true",
            "NOTIFICATION_CHANNELS": "telegram",
            "TELEGRAM_BOT_TOKEN": "123:FAKE",
            "TELEGRAM_CHAT_ID": "999",
        }, follow_redirects=False)
        assert r.status_code == 302, "建用户失败，后面的断言就没意义了"

        from users import load_users
        with test_app.app_context():
            uid = next(u.id for u in load_users() if u.name == "LeakProbe")

        def _boom_closing_coro(coro, *_a, **_kw):
            # _run_async 平时会消费掉这个协程；这里替换掉它，就得自己关，
            # 否则 pytest 会报 "coroutine was never awaited"。
            coro.close()
            raise RuntimeError(CANARY)

        with patch("app.routes.users._run_async", _boom_closing_coro):
            r = admin_client.post(f"/users/{uid}/test",
                                  headers={"X-CSRF-Token": "test_csrf"})

        body = r.get_data(as_text=True)
        assert CANARY not in body, f"SDK 异常文本泄漏：{body[:300]}"

        results = r.get_json()["results"]
        assert results, "没走到任何渠道，except 分支没被执行"
        assert results[0]["ok"] is False
        assert results[0]["error"] == "发送失败，请检查日志"

        logged = "\n".join(rec.getMessage() + (rec.exc_text or "")
                           for rec in caplog_exc.records)
        assert CANARY in logged, "异常被吞了"

    def test_geocode_status_no_leak(self, caplog_exc):
        """/api/map/geocode/status 是 @api_login_required——普通用户读得到。

        直接跑 worker：状态是模块级全局，不需要过 HTTP 也能验。
        """
        from app.routes import map_routes

        with patch.object(map_routes, "_geocode_one", _boom), \
             patch.object(map_routes, "storage"), \
             patch.object(map_routes._time, "sleep", lambda *_: None):
            map_routes._run_geocode_worker(["Damrak 1, Amsterdam"])

        errors = map_routes._geocode_status["errors"]
        assert errors, "worker 没记下失败"
        reason = errors[0]["reason"]
        assert CANARY not in reason, f"Photon 异常文本泄漏：{reason}"
        assert reason == "geocoding request failed"
        assert errors[0]["address"] == "Damrak 1, Amsterdam", "地址要留着，用户得知道是哪条失败"

        logged = "\n".join(r.getMessage() + (r.exc_text or "")
                           for r in caplog_exc.records)
        assert CANARY in logged, "异常被吞了"


# ── 防回归：源码级扫描 ──────────────────────────────────────

class TestNoRawExceptionInSource:
    """单测只能覆盖已知路由。这条扫源码，挡住以后新加的。

    白名单里的每一条都是「异常是我们自己抛的、文案是我们自己写的」——
    ValueError 来自表单校验，MonitorServiceError 是带 .status 的自定义类型。
    判据是异常的来源，不是 str(e) 这个写法本身。
    """

    def test_no_new_str_e_in_responses(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "app" / "routes"
        # (相对路径, 捕获的异常类型) —— 只有自家类型可以直出
        allowed_excs = {"ValueError", "MonitorServiceError", "LookupError"}

        offenders = []
        for py in sorted(root.rglob("*.py")):
            lines = py.read_text(encoding="utf-8").split("\n")
            for i, line in enumerate(lines):
                if "str(e)" not in line:
                    continue
                # str(e) 拿来做比较或写日志都不算泄漏
                if re.search(r'str\(e\)\s*[=!]=', line) or "logger." in line:
                    continue
                # 往回找最近的 except，看捕获的是什么
                exc_type = None
                for j in range(i, max(-1, i - 8), -1):
                    m = re.search(r"except\s+([A-Za-z_][\w.]*)", lines[j])
                    if m:
                        exc_type = m.group(1)
                        break
                if exc_type not in allowed_excs:
                    rel = py.relative_to(root.parent.parent)
                    offenders.append(f"{rel}:{i+1} except {exc_type}: {line.strip()}")

        assert not offenders, (
            "这些地方把裸异常文本回给了客户端。改用 "
            "api_errors.legacy_server_error(e, \"<endpoint>\") ——"
            "它写日志、只回固定文案：\n  " + "\n  ".join(offenders)
        )
