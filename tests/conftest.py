"""
共享测试 fixture。
================

T1 主要是纯函数测试，需要的 fixture 很少：
- ``temp_db``       : 每个测试一个 Storage 实例（基于 tmp_path），自动隔离
- ``app_ctx``       : Flask app 请求上下文（safe_next_url 需要 url_for）
- ``fresh_crypto``  : 隔离 crypto 模块的全局 _CIPHER + DATA_ENCRYPTION_KEY

设计原则：
- 不引入复杂的 Flask client（T1 没有 HTTP 测试）
- 不 mock 外部网络（T1 是纯逻辑测试）
- 文件/数据库都走 tmp_path，进程隔离
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保从项目根目录 import
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def temp_db(tmp_path):
    """单测试用 Storage，结束后自动 close。SQLite 文件位于 tmp_path 下。"""
    from storage import Storage
    db = Storage(tmp_path / "test.db", timezone_str="UTC")
    yield db
    db.close()


@pytest.fixture
def app_ctx():
    """
    提供最小 Flask 请求上下文，供需要 url_for 的纯函数测试使用。

    不 import web.py —— 避免触发整套应用启动副作用（读 .env、
    生成 FLASK_SECRET 并写入真实 .env、连数据库等）。

    只注册一个 endpoint='index' 路由，这是 safe_next_url 用到的唯一
    url_for 调用。
    """
    from flask import Flask
    _app = Flask("test_minimal_app")

    @_app.route("/", endpoint="index")
    def _index():
        return ""

    with _app.test_request_context():
        yield


@pytest.fixture
def fresh_crypto(tmp_path, monkeypatch):
    """
    隔离 crypto 模块的全局 _CIPHER 状态 + 临时 ENV_PATH。

    每个测试看到的都是干净的 crypto 状态：
    - 全局 _CIPHER 被重置
    - DATA_ENCRYPTION_KEY 环境变量被清除
    - ENV_PATH 指向 tmp_path/.env（避免污染真实 .env）
    """
    import os
    import crypto
    monkeypatch.setattr(crypto, "_CIPHER", None)
    monkeypatch.setattr(crypto, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
    # 阻止 crypto.write_env_key 写到真实 .env
    import config
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    yield
    # cleanup: 重置 _CIPHER（下一个 test 可能不用 fresh_crypto）
    monkeypatch.setattr(crypto, "_CIPHER", None)


# ────────────────────────────────────────────────────────────────────────
# HTTP 测试 fixture（T2）
# ────────────────────────────────────────────────────────────────────────

# 测试用凭据 —— 设到 env var 中，被 _auth_enabled() / login 处理函数读取
_TEST_USERNAME = "test_admin"
_TEST_PASSWORD = "test_password_xyz_123"


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """
    把所有写盘操作重定向到 tmp_path，避免测试污染真实 data/。

    覆盖：
    - users.USERS_FILE             → tmp_path/users.json
    - config.ENV_PATH              → tmp_path/.env
    - settings_route.ENV_PATH      → 同上（settings 路由 import 时已抓走）
    - system_route._LOG_FILES      → tmp_path/{monitor,errors}.log
                                     **关键**：否则 /api/logs/clear 会清空真实日志
    """
    import users, config
    fake_users_file = tmp_path / "users.json"
    fake_env = tmp_path / ".env"
    fake_env.write_text("WEB_PASSWORD=dummy\n", encoding="utf-8")  # 保证 ENV_PATH.exists()

    monkeypatch.setattr(users, "USERS_FILE", fake_users_file)
    monkeypatch.setattr(config, "ENV_PATH", fake_env)

    # settings 路由
    try:
        from app.routes import settings as settings_route
        monkeypatch.setattr(settings_route, "ENV_PATH", fake_env)
    except ImportError:
        pass

    # system 路由的日志白名单 —— 必须替换整个 dict 的值，
    # 否则 /api/logs/clear 会 write_text("") 到真实 data/monitor.log
    try:
        from app.routes import system as system_route
        fake_log_files = {
            "monitor": tmp_path / "monitor.log",
            "errors":  tmp_path / "errors.log",
            "web":     tmp_path / "web.log",
        }
        monkeypatch.setattr(system_route, "_LOG_FILES", fake_log_files)
    except ImportError:
        pass

    return tmp_path


@pytest.fixture(autouse=True)
def _reset_login_failures():
    """每个测试前后清空登录失败计数，避免测试间互相污染。"""
    try:
        from app.auth import _LOGIN_FAILURES
        _LOGIN_FAILURES.clear()
    except ImportError:
        pass
    yield
    try:
        from app.auth import _LOGIN_FAILURES
        _LOGIN_FAILURES.clear()
    except ImportError:
        pass


@pytest.fixture
def test_app(monkeypatch, isolated_data_dir):
    """
    返回 web.app，且环境变量被设为已知的测试凭据。
    依赖 isolated_data_dir 保证文件写不到真实 data/。
    """
    monkeypatch.setenv("WEB_PASSWORD", _TEST_PASSWORD)
    monkeypatch.setenv("WEB_USERNAME", _TEST_USERNAME)
    monkeypatch.setenv("WEB_GUEST_MODE", "true")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    from web import app
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(test_app):
    """匿名 client（未登录）。"""
    return test_app.test_client()


@pytest.fixture
def admin_client(test_app):
    """
    已注入 admin session 的 client。
    CSRF token 是固定值 `test_csrf`，测试 POST 时用 X-CSRF-Token 或 form csrf_token 字段。
    跳过实际 /login flow，直接走 session 注入，避免每个测试重复几行 setup。
    """
    c = test_app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["role"] = "admin"
        sess["csrf_token"] = "test_csrf"
    return c


@pytest.fixture
def guest_client(test_app):
    """已注入 guest session 的 client（只读身份）。"""
    c = test_app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["role"] = "guest"
        sess["csrf_token"] = "test_csrf"
    return c


# 暴露常量供测试 import
@pytest.fixture
def test_credentials():
    return {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
