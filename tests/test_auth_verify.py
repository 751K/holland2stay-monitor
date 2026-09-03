"""``POST /auth/verify`` —— 只回答"密码对不对"，不签发 token。

存在的理由：iOS 要在设置页开启 Face ID，必须把明文密码写进 Keychain，而设置页
手里只有 token。用 ``/auth/login`` 去确认会真的签出 token，用户每确认一次
"活跃设备会话数"就涨一个。

这组测试守三件事：
1. 判定与 ``/auth/login`` 完全一致（两处分家 → 存进 Keychain 的密码登录时不认）
2. 不产生任何副作用（不发 token、不撤会话）
3. 不能被当成密码验证预言机去撞别人的账号
"""
import pytest

# fixtures 定义在 test_api_v1_endpoints 里（api_app / seeded / *_token 是一整套，
# 挪进 conftest 会波及全部 4000+ 条用例），这里直接引进来复用。
from tests.test_api_v1_endpoints import (  # noqa: F401
    api_app, api_client, seeded, user_token, admin_token,
)


PW = "user_pw_xyz"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestVerifyBasics:
    def test_correct_password_returns_ok(self, api_client, seeded, user_token):
        r = api_client.post("/api/v1/auth/verify", json={"password": PW},
                            headers=_bearer(user_token))
        assert r.status_code == 200
        assert r.get_json()["data"]["ok"] is True

    def test_wrong_password_is_401(self, api_client, seeded, user_token):
        r = api_client.post("/api/v1/auth/verify", json={"password": "nope"},
                            headers=_bearer(user_token))
        assert r.status_code == 401

    def test_empty_password_is_rejected(self, api_client, seeded, user_token):
        r = api_client.post("/api/v1/auth/verify", json={"password": ""},
                            headers=_bearer(user_token))
        assert r.status_code == 400

    def test_requires_a_token(self, api_client, seeded):
        r = api_client.post("/api/v1/auth/verify", json={"password": PW})
        assert r.status_code == 401

    def test_admin_is_rejected_by_the_role_gate(self, api_client, seeded,
                                                admin_token, test_credentials):
        """admin 密码在 .env，且 Face ID 只对 user 开放。

        断言必须是 **403**（角色闸），不能写成 ``in (401, 403)``：admin 没有
        user_id，把端点开放给 admin 之后处理函数照样会返回 401，两种写法在
        宽松断言下无法区分——第一版就是这么漏掉的。密码也用**正确**的那个，
        否则 401 只能证明密码错，证明不了角色被拦。
        """
        r = api_client.post(
            "/api/v1/auth/verify",
            json={"password": test_credentials["password"]},
            headers=_bearer(admin_token))
        assert r.status_code == 403, r.get_json()


class TestNoSideEffects:
    def test_does_not_issue_a_token(self, api_client, seeded, user_token):
        """整个响应里不能出现任何像 token 的东西。"""
        r = api_client.post("/api/v1/auth/verify", json={"password": PW},
                            headers=_bearer(user_token))
        data = r.get_json()["data"]
        assert set(data) == {"ok"}, data

    def test_session_count_is_unchanged(self, api_client, seeded, user_token):
        """确认密码不该让"活跃设备会话数"往上涨——这正是不复用 login 的原因。"""
        user, _ = seeded

        def sessions() -> int:
            from app.db import storage
            st = storage()
            try:
                return len(st.list_app_tokens(user_id=user.id))
            finally:
                st.close()

        before = sessions()
        for _ in range(3):
            api_client.post("/api/v1/auth/verify", json={"password": PW},
                            headers=_bearer(user_token))
        assert sessions() == before

    def test_current_token_still_works_afterwards(self, api_client, seeded, user_token):
        api_client.post("/api/v1/auth/verify", json={"password": PW},
                        headers=_bearer(user_token))
        r = api_client.get("/api/v1/auth/me", headers=_bearer(user_token))
        assert r.status_code == 200

    def test_a_failed_attempt_does_not_revoke_the_token(self, api_client, seeded, user_token):
        api_client.post("/api/v1/auth/verify", json={"password": "wrong"},
                        headers=_bearer(user_token))
        r = api_client.get("/api/v1/auth/me", headers=_bearer(user_token))
        assert r.status_code == 200


class TestNotAnOracle:
    def test_ignores_a_username_in_the_body(self, api_client, seeded, user_token):
        """操作对象由 g.api_user_id 锁定。

        接受 username 就等于把它变成密码验证预言机：任意一个合法 token
        的持有者可以拿它去撞别人的账号。
        """
        r = api_client.post(
            "/api/v1/auth/verify",
            json={"username": "admin", "password": "wrong-admin-pw"},
            headers=_bearer(user_token))
        assert r.status_code == 401

    def test_own_password_still_passes_with_a_bogus_username(self, api_client, seeded, user_token):
        """body 里的 username 完全不被看——传垃圾也不影响自己的密码判定。"""
        r = api_client.post(
            "/api/v1/auth/verify",
            json={"username": "someone-else", "password": PW},
            headers=_bearer(user_token))
        assert r.status_code == 200


class TestAgreesWithLogin:
    """判定必须和 /auth/login 一致——这是这个端点存在的前提。"""

    @pytest.mark.parametrize("password,expect_ok", [
        (PW, True),
        (PW.upper(), False),
        (PW + " ", False),
        ("", False),
        ("x" * 200, False),
    ])
    def test_same_verdict_as_login(self, api_client, seeded, user_token,
                                   password, expect_ok):
        verify = api_client.post("/api/v1/auth/verify", json={"password": password},
                                 headers=_bearer(user_token))
        login = api_client.post("/api/v1/auth/login",
                                json={"username": "kong", "password": password,
                                      "device_name": "t"})
        assert (verify.status_code == 200) == expect_ok
        assert (login.status_code == 200) == expect_ok, (
            f"{password!r}: verify={verify.status_code} login={login.status_code}")
