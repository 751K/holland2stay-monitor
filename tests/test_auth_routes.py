"""
鉴权路由 HTTP 测试：login/logout/guest + CSRF + brute-force + admin gating。

关键场景：
- 未登录访问页面 → 302 /login；访问 API → 401 JSON
- POST /login 无 CSRF token → 403
- POST /login 错凭据 → 重渲染 + 记录失败 → 触发 brute-force 退避
- POST /login 正确凭据 → 302 / + session 注入 admin
- /guest → 注入 guest role；不能从 admin 降级
- /logout 清 session
- admin_required 页面：guest 重定向到 /；admin 200
- admin_api_required API：guest 返 403；admin 通过 auth 后续校验
"""
from __future__ import annotations

import pytest


# ─── 未登录态 ───────────────────────────────────────────────────


class TestUnauthenticated:
    def test_root_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_protected_page_redirects(self, client):
        r = client.get("/listings")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_api_returns_401(self, client):
        r = client.get("/api/status")
        assert r.status_code == 401
        assert r.get_json() == {"error": "unauthorized"}

    def test_admin_api_returns_401(self, client):
        r = client.get("/api/logs")
        assert r.status_code == 401

    def test_health_no_auth_required(self, client):
        """/health 用于 docker healthcheck，必须开放。"""
        r = client.get("/health")
        assert r.status_code == 200

    def test_login_page_accessible(self, client):
        r = client.get("/login")
        assert r.status_code == 200


# ─── CSRF 防护 ──────────────────────────────────────────────────


class TestCSRF:
    def test_login_post_without_csrf_returns_403(self, client, test_credentials):
        r = client.post("/login", data={
            "username": test_credentials["username"],
            "password": test_credentials["password"],
        })
        assert r.status_code == 403

    def test_login_post_with_wrong_csrf_returns_403(self, client, test_credentials):
        # 先 GET 拿真实 token，但 POST 时用错的
        client.get("/login")
        r = client.post("/login", data={
            "username": test_credentials["username"],
            "password": test_credentials["password"],
            "csrf_token": "wrong-token",
        })
        assert r.status_code == 403

    def test_logout_requires_csrf(self, admin_client):
        # admin_client 注入了 csrf=test_csrf，但没传 → 403
        r = admin_client.post("/logout")
        assert r.status_code == 403

    def test_logout_with_correct_csrf_succeeds(self, admin_client):
        r = admin_client.post("/logout", data={"csrf_token": "test_csrf"})
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ─── 登录成功路径 ───────────────────────────────────────────────


def _login(client, username, password):
    """完整登录辅助：GET /login 取 CSRF，再 POST。"""
    client.get("/login")
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token", "")
    return client.post("/login", data={
        "username": username,
        "password": password,
        "csrf_token": csrf,
    })


class TestLoginSuccess:
    def test_correct_credentials_redirects_to_index(self, client, test_credentials):
        r = _login(client, test_credentials["username"], test_credentials["password"])
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")

    def test_session_marks_admin(self, client, test_credentials):
        _login(client, test_credentials["username"], test_credentials["password"])
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True
            assert sess.get("role") == "admin"

    def test_can_access_protected_pages_after_login(self, client, test_credentials):
        _login(client, test_credentials["username"], test_credentials["password"])
        for url in ["/", "/listings", "/users", "/settings"]:
            r = client.get(url)
            assert r.status_code == 200, f"{url} = {r.status_code}"


# ─── 登录失败 + brute-force 退避 ───────────────────────────────


class TestLoginFailureAndBackoff:
    def test_wrong_password_returns_200_with_flash(self, client, test_credentials):
        client.get("/login")
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token", "")
        r = client.post("/login", data={
            "username": test_credentials["username"],
            "password": "WRONG",
            "csrf_token": csrf,
        })
        # 失败渲染回 login 页（200），不重定向
        assert r.status_code == 200

    def test_wrong_username_returns_200(self, client, test_credentials):
        client.get("/login")
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token", "")
        r = client.post("/login", data={
            "username": "wrong-user",
            "password": test_credentials["password"],
            "csrf_token": csrf,
        })
        assert r.status_code == 200

    def test_brute_force_state_is_module_level(self):
        """app.auth 维护 IP → 失败时间戳 list，超阈值后产生延迟。
        本测试直接测函数（不走 HTTP），避免登录慢测试。"""
        from app.auth import (
            check_login_rate, record_login_failure, clear_login_failures,
            LOGIN_MAX_FAILURES, LOGIN_BASE_DELAY,
        )
        ip = "1.2.3.4"
        clear_login_failures(ip)

        # 阈值以下：0 延迟
        for _ in range(LOGIN_MAX_FAILURES - 1):
            record_login_failure(ip)
        assert check_login_rate(ip) == 0.0

        # 达到阈值：开始延迟（extra=0 → BASE_DELAY × 2^0）
        record_login_failure(ip)
        assert check_login_rate(ip) == LOGIN_BASE_DELAY

        # 阈值+1：指数翻倍
        record_login_failure(ip)
        assert check_login_rate(ip) == LOGIN_BASE_DELAY * 2

        # clear 后重置
        clear_login_failures(ip)
        assert check_login_rate(ip) == 0.0

    def test_successful_login_clears_failures(self, client, test_credentials):
        """登录成功后该 IP 的失败计数应该清零。"""
        # Flask test_client 默认 IP = 127.0.0.1
        from app.auth import _LOGIN_FAILURES, record_login_failure
        ip = "127.0.0.1"
        # 制造 3 次失败
        for _ in range(3):
            record_login_failure(ip)
        assert len(_LOGIN_FAILURES.get(ip, [])) == 3

        # 成功登录
        _login(client, test_credentials["username"], test_credentials["password"])
        # 计数应被清掉
        assert _LOGIN_FAILURES.get(ip, []) == []


# ─── Guest 模式 ─────────────────────────────────────────────────


class TestGuestMode:
    def test_guest_route_creates_guest_session(self, client):
        r = client.get("/guest")
        assert r.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True
            assert sess.get("role") == "guest"

    def test_admin_cannot_be_demoted_via_guest_route(self, admin_client):
        """已登录的 admin 调 /guest 不应该被降级到 guest（CSRF 降级攻击防护）。"""
        r = admin_client.get("/guest")
        assert r.status_code == 302
        with admin_client.session_transaction() as sess:
            assert sess.get("role") == "admin", "admin 被错误降级"

    def test_guest_can_read_index(self, guest_client):
        r = guest_client.get("/")
        assert r.status_code == 200

    def test_guest_blocked_from_admin_page(self, guest_client):
        r = guest_client.get("/settings")
        # admin_required 重定向到 index（不显示登录页，因为 authenticated=True）
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")

    def test_guest_blocked_from_admin_api(self, guest_client):
        r = guest_client.post("/api/reset-db",
                              json={"confirm": True},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 403  # admin_api_required 返回 forbidden


# ─── Logout ─────────────────────────────────────────────────────


class TestLogout:
    def test_logout_clears_session(self, admin_client):
        admin_client.post("/logout", data={"csrf_token": "test_csrf"})
        with admin_client.session_transaction() as sess:
            assert "authenticated" not in sess
            assert "role" not in sess

    def test_after_logout_protected_pages_redirect(self, admin_client):
        admin_client.post("/logout", data={"csrf_token": "test_csrf"})
        r = admin_client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ─── 非 ASCII 输入回归（hmac.compare_digest 修复） ─────────────


class TestNonAsciiInputs:
    """
    回归保护：hmac.compare_digest 不接受含非 ASCII 字符的 str 参数，
    会抛 TypeError。攻击者用中文/emoji 用户名/CSRF token 能让路由 500。

    修复（sessions.py / csrf.py）：在比较前 .encode("utf-8") 转 bytes。
    断言：所有非 ASCII 输入应返回 200/302/403 等业务码，绝不 500。
    """

    def test_chinese_username_returns_200_not_500(self, client, test_credentials):
        client.get("/login")
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token", "")
        r = client.post("/login", data={
            "username": "管理员",            # 中文用户名
            "password": test_credentials["password"],
            "csrf_token": csrf,
        })
        # 应当被业务层判为"用户名错误" → 重渲染 200，绝不能 500
        assert r.status_code == 200, f"non-ASCII username crashed: {r.status_code}"

    def test_chinese_password_returns_200_not_500(self, client, test_credentials):
        client.get("/login")
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token", "")
        r = client.post("/login", data={
            "username": test_credentials["username"],
            "password": "密码123",
            "csrf_token": csrf,
        })
        assert r.status_code == 200, f"non-ASCII password crashed: {r.status_code}"

    def test_emoji_in_credentials_returns_200_not_500(self, client, test_credentials):
        """更极端：emoji + 中文混合，模拟实际攻击 payload。"""
        client.get("/login")
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token", "")
        r = client.post("/login", data={
            "username": "😀admin",
            "password": "🔑pw中文",
            "csrf_token": csrf,
        })
        assert r.status_code == 200

    def test_chinese_csrf_token_returns_403_not_500(self, client):
        """POST 带中文 CSRF token：应 403（token 不匹配），绝不 500。"""
        # 任何 POST 路由都行，用 /login 即可；先 GET 让 session 拿到真 token
        client.get("/login")
        r = client.post("/login", data={
            "username": "admin",
            "password": "x",
            "csrf_token": "中文token伪造",     # 非 ASCII csrf
        })
        assert r.status_code == 403, f"non-ASCII CSRF crashed: {r.status_code}"

    def test_emoji_csrf_token_returns_403_not_500(self, client):
        client.get("/login")
        r = client.post("/login", data={
            "username": "admin", "password": "x",
            "csrf_token": "🔓bypass",
        })
        assert r.status_code == 403

    def test_chinese_csrf_via_header_returns_403_not_500(self, admin_client):
        """X-CSRF-Token header 路径也要安全（fetch / XHR 用这个）。"""
        r = admin_client.post(
            "/api/logs/clear",
            headers={"X-CSRF-Token": "非ASCII头"},
        )
        assert r.status_code == 403


# ─── Admin gating: 已登录但角色不对 ──────────────────────────


class TestAdminGating:
    def test_admin_can_access_settings(self, admin_client):
        assert admin_client.get("/settings").status_code == 200

    def test_admin_can_access_users(self, admin_client):
        assert admin_client.get("/users").status_code == 200

    def test_admin_can_access_system(self, admin_client):
        assert admin_client.get("/system").status_code == 200

    def test_admin_api_passes_auth_layer(self, admin_client):
        """admin POST /api/reset-db 没 confirm 应被业务逻辑挡 400（说明 auth 已通过）。"""
        r = admin_client.post("/api/reset-db",
                              json={},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400  # 业务层挡，不是 401/403
