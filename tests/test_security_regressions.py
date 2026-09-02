"""四处安全回归。

  device test  测试通知写成 user_id=""（系统通知）→ 全站含访客都看得到攻击者
               控制的 title/body，且无限流
  /system      os.environ.clear() 再还原 → 那一瞬 WEB_PASSWORD 消失 →
               auth_enabled() 为 False → **is_admin() 对所有人为 True**
  反代         全库无 ProxyFix → remote_addr 恒为代理 IP → 十一处 IP 限流退化成
               一个全局桶：既防不住，又能被三次注册把全站锁一小时
  /login       用户不存在就自动注册 → 绕过 terms_accepted 与 64 字符截断，
               而且构成用户枚举侧信道
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _code(fn) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln)
                     for ln in inspect.getsource(fn).split("\n"))


class TestTestPushIsNotABroadcast:
    def test_notification_is_scoped_to_a_user(self):
        """user_id="" 是「系统通知」，get_notifications 的过滤是
        ``user_id = ? OR user_id = ''``——每个用户都看得到。"""
        from app.services import device_service

        src = _code(device_service.create_web_test_notification)
        assert "user_id=" in src, "写入时没有传 user_id"
        assert "_ADMIN_TEST_SCOPE" in src, "留空时没有退到私有作用域"

    def test_never_writes_an_empty_user_id(self):
        """哪怕调用方漏传，也不能落进「所有人可见」那一档。"""
        import app.services.device_service as ds

        captured = {}

        class _St:
            def add_web_notification(self, **kw):
                captured.update(kw)
                return 1

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        ds.storage_ctx = lambda: _St()
        ds.create_web_test_notification(title="t", body="b")      # 不传 user_id
        assert captured["user_id"], "写出了 user_id=''，全站可见"

    def test_route_passes_the_caller(self):
        src = (ROOT / "app" / "routes" / "api_v1" / "devices.py").read_text(
            encoding="utf-8")
        i = src.index("send_test_push(")
        assert "current_user_id()" in src[i:i + 400]


class TestSystemPageDoesNotOpenAnAuthWindow:
    def test_no_environ_clear(self):
        """gunicorn 单进程八线程共享 os.environ。clear() 到 update() 之间那一瞬
        WEB_PASSWORD 不存在，is_admin() 的第一行就是「鉴权没开就当 admin」。"""
        from app.routes import system

        src = _code(system)
        assert "environ.clear()" not in src

    def test_reads_env_without_mutating_the_process(self):
        from app.routes import system

        src = _code(system)
        assert "dotenv_values" in src, "还在用 load_dotenv(override=True)"

    def test_is_admin_really_depends_on_the_env(self, monkeypatch):
        """钉住那个前提：WEB_PASSWORD 一消失，所有人就是 admin。

        这条不测我们的修复，测的是**为什么必须修**——哪天 is_admin 改了实现，
        这条会红，提醒重新评估上面两条还够不够。
        """
        import os

        from app import auth

        monkeypatch.setenv("WEB_PASSWORD", "x")
        assert auth.auth_enabled()
        monkeypatch.delenv("WEB_PASSWORD")
        assert not auth.auth_enabled()


class TestRealClientIp:
    def test_proxyfix_is_available(self):
        src = (ROOT / "web.py").read_text(encoding="utf-8")
        assert "ProxyFix" in src, "反代后 remote_addr 恒为代理 IP，限流全废"

    def test_it_is_opt_in(self):
        """直接暴露端口的部署里开它 = 让客户端自报 IP。默认必须关。"""
        src = (ROOT / "web.py").read_text(encoding="utf-8")
        i = src.index("_TRUSTED_PROXY_HOPS")
        assert 'or "0"' in src[i:i + 200], "默认值不是 0"
        assert "if _TRUSTED_PROXY_HOPS > 0:" in src

    def test_key_is_registered(self):
        from env_registry import KNOWN_ENV_KEYS

        assert "TRUSTED_PROXY_HOPS" in KNOWN_ENV_KEYS


class TestLoginDoesNotAutoRegister:
    def test_no_auto_registration(self):
        """绕过 terms_accepted（登录表单上根本没有那个勾选框）与 64 字符截断，
        而且构成用户枚举侧信道。"""
        from app.routes import sessions

        src = _code(sessions.login)
        assert "update_users" not in src, "/login 仍在建用户"
        assert "set_app_password" not in src

    def test_unknown_user_gets_the_same_message(self):
        """文案必须与「密码错误」一致，否则一次 POST 就能判断用户名是否存在。"""
        from app.routes import sessions

        src = _code(sessions.login)
        assert src.count("用户名或密码错误") >= 2

    def test_registration_still_checks_terms(self):
        """正规注册那条路不能跟着一起松。"""
        from app.routes import sessions

        src = _code(sessions.register_user)
        assert "terms_accepted" in src
        assert '[:64]' in src


class TestLoginRateLimitDoesNotBlockThreads:
    def test_no_sleep_in_the_login_path(self):
        """gunicorn 只有 8 条线程；sleep 是拿自己的服务能力去关自己。"""
        from app.routes import sessions

        src = _code(sessions.login)
        assert "_time.sleep" not in src and "time.sleep" not in src

    def test_returns_429(self):
        from app.routes import sessions

        src = _code(sessions.login)
        i = src.index("check_login_rate")
        assert "429" in src[i:i + 500]
