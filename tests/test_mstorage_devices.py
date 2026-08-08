"""
mstorage._devices DeviceOps 单元测试
======================================

覆盖：
- register_device 新建 / 同 (token,session) 刷新 / disable 复活
- 输入校验：bad env / 过短 device_token
- list_devices_for_token: 按会话隔离
- get_active_devices_for_user: JOIN app_tokens 过滤 revoked / disabled
- disable_device / disable_device_by_token / delete_device
"""

from __future__ import annotations

import pytest


# ── register ───────────────────────────────────────────────────────


class TestRegister:
    def _seed_token(self, db, role="user", user_id="kong0001"):
        tid, _ = db.create_app_token(role=role, user_id=user_id)
        return tid

    def test_register_new(self, temp_db):
        tid = self._seed_token(temp_db)
        did = temp_db.register_device(
            app_token_id=tid,
            device_token="a" * 64,
            env="production",
            model="iPhone15,2",
            bundle_id="com.x.y",
        )
        assert did > 0
        rows = temp_db.list_devices_for_token(tid)
        assert len(rows) == 1
        assert rows[0]["device_token"] == "a" * 64

    def test_register_same_refreshes(self, temp_db):
        """重复注册同 (app_token, device_token) → 刷新而非新增。"""
        tid = self._seed_token(temp_db)
        d1 = temp_db.register_device(
            app_token_id=tid, device_token="b" * 64, env="production",
        )
        d2 = temp_db.register_device(
            app_token_id=tid, device_token="b" * 64, env="sandbox", model="X",
        )
        assert d1 == d2
        rows = temp_db.list_devices_for_token(tid)
        assert len(rows) == 1
        assert rows[0]["env"] == "sandbox"
        assert rows[0]["model"] == "X"

    def test_re_register_revives_disabled(self, temp_db):
        tid = self._seed_token(temp_db)
        did = temp_db.register_device(
            app_token_id=tid, device_token="c" * 64,
        )
        temp_db.disable_device(did, reason="Unregistered")
        # disabled
        assert temp_db.get_device(did)["disabled_at"] is not None
        # 用户重装 App → 再注册同 token → 复活
        did2 = temp_db.register_device(
            app_token_id=tid, device_token="c" * 64,
        )
        assert did2 == did
        assert temp_db.get_device(did)["disabled_at"] is None

    def test_bad_env_rejected(self, temp_db):
        tid = self._seed_token(temp_db)
        with pytest.raises(ValueError):
            temp_db.register_device(
                app_token_id=tid, device_token="d" * 64, env="weird",
            )

    def test_short_token_rejected(self, temp_db):
        tid = self._seed_token(temp_db)
        with pytest.raises(ValueError):
            temp_db.register_device(
                app_token_id=tid, device_token="short", env="production",
            )


# ── 查询 ────────────────────────────────────────────────────────────


class TestQueries:
    def test_active_devices_per_user(self, temp_db):
        # 两个用户各一台设备
        tA, _ = temp_db.create_app_token(role="user", user_id="userA")
        tB, _ = temp_db.create_app_token(role="user", user_id="userB")
        temp_db.register_device(app_token_id=tA, device_token="a" * 64)
        temp_db.register_device(app_token_id=tB, device_token="b" * 64)
        assert len(temp_db.get_active_devices_for_user("userA")) == 1
        assert len(temp_db.get_active_devices_for_user("userB")) == 1
        assert len(temp_db.get_active_devices_for_user("ghost")) == 0

    def test_revoked_token_excludes_device(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="userX")
        temp_db.register_device(app_token_id=tid, device_token="x" * 64)
        temp_db.revoke_app_token(tid)
        # revoked → 不再可推
        assert temp_db.get_active_devices_for_user("userX") == []

    def test_disabled_device_excludes(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="userY")
        did = temp_db.register_device(
            app_token_id=tid, device_token="y" * 64,
        )
        temp_db.disable_device(did, reason="Unregistered")
        assert temp_db.get_active_devices_for_user("userY") == []


# ── 失活 / 删除 ────────────────────────────────────────────────────


class TestDisableDelete:
    def test_disable_idempotent(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="u")
        did = temp_db.register_device(app_token_id=tid, device_token="z" * 64)
        assert temp_db.disable_device(did, reason="x") is True
        # 二次 disable 已是 disabled → no-op
        assert temp_db.disable_device(did, reason="x") is False

    def test_disable_by_token_bulk(self, temp_db):
        """APNs 返回 410 时，按 token 失效要覆盖它名下所有还活着的行。

        正常流程下 register_device 已经保证同一 token 只有一行是活的（见
        TestDeviceBelongsToLatestSession），所以这里直接造出多行活着的历史
        状态——存量数据里就有这种行（该不变量 2026-08-07 才引入）。
        """
        tok = "dup" + "0" * 61
        tA, _ = temp_db.create_app_token(role="user", user_id="A")
        tB, _ = temp_db.create_app_token(role="user", user_id="B")
        temp_db.register_device(app_token_id=tA, device_token=tok)
        temp_db.register_device(app_token_id=tB, device_token=tok)
        # 把 A 的行手工复活，还原引入不变量之前的存量状态
        temp_db.conn.execute(
            "UPDATE device_tokens SET disabled_at = NULL, disabled_reason = NULL "
            "WHERE app_token_id = ?", (tA,),
        )
        temp_db.conn.commit()
        n = temp_db.disable_device_by_token(tok, reason="Unregistered")
        assert n == 2

    def test_delete_device(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="u")
        did = temp_db.register_device(app_token_id=tid, device_token="e" * 64)
        assert temp_db.delete_device(did) is True
        assert temp_db.get_device(did) is None
        assert temp_db.delete_device(did) is False


# ── 一台设备只归属最后一次注册的会话 ─────────────────────────────────

class TestDeviceBelongsToLatestSession:
    """换账号之后，旧账号不能继续往这台设备推。

    APNs 的 device token 是「一个 App 安装 = 一个 token」，所以同一 token 出现
    在新会话下只意味着同一台设备换了个登录。但 ``device_tokens`` 是按会话分行
    的，而 ``get_active_devices_for_*`` 只按 ``revoked`` 过滤——换账号时客户端
    如果没能调成 ``/auth/logout``，旧会话一直是 ``revoked=0``。

    2026-08-07 生产实测两台设备中招，其中一台横跨两个不同账号：

        9e660f7c…  user=c622bf26  登录 06-07  最后活跃 07-30
                   user=ffdfe243  登录 08-03  最后活跃 08-07
    """

    TOK = "sw" + "0" * 62

    def test_new_session_supersedes_old(self, temp_db):
        tA, _ = temp_db.create_app_token(role="user", user_id="A")
        tB, _ = temp_db.create_app_token(role="user", user_id="B")
        temp_db.register_device(app_token_id=tA, device_token=self.TOK)
        temp_db.register_device(app_token_id=tB, device_token=self.TOK)

        assert temp_db.get_active_devices_for_user("A") == []
        assert len(temp_db.get_active_devices_for_user("B")) == 1

    def test_supersede_records_a_distinguishable_reason(self, temp_db):
        """理由要和 APNs 的 410 区分开，否则排查时看不出是被顶掉还是设备没了。"""
        tA, _ = temp_db.create_app_token(role="user", user_id="A")
        tB, _ = temp_db.create_app_token(role="user", user_id="B")
        temp_db.register_device(app_token_id=tA, device_token=self.TOK)
        temp_db.register_device(app_token_id=tB, device_token=self.TOK)

        old = [d for d in temp_db.list_all_devices() if d["user_id"] == "A"][0]
        assert old["disabled_reason"] == "SupersededBySession"

    def test_switching_back_revives(self, temp_db):
        """A→B→A：切回来要能收，且 B 这时该停掉。"""
        tA, _ = temp_db.create_app_token(role="user", user_id="A")
        tB, _ = temp_db.create_app_token(role="user", user_id="B")
        temp_db.register_device(app_token_id=tA, device_token=self.TOK)
        temp_db.register_device(app_token_id=tB, device_token=self.TOK)
        temp_db.register_device(app_token_id=tA, device_token=self.TOK)

        assert len(temp_db.get_active_devices_for_user("A")) == 1
        assert temp_db.get_active_devices_for_user("B") == []

    def test_admin_sessions_do_not_double_push(self, temp_db):
        """同一台设备两个 admin 会话都活着 → 同一条推送发两遍。生产实测过。"""
        t1, _ = temp_db.create_app_token(role="admin", user_id=None)
        t2, _ = temp_db.create_app_token(role="admin", user_id=None)
        temp_db.register_device(app_token_id=t1, device_token=self.TOK)
        temp_db.register_device(app_token_id=t2, device_token=self.TOK)

        assert len(temp_db.get_active_devices_for_admin()) == 1

    def test_other_devices_untouched(self, temp_db):
        """只顶掉同一个 token 的行，别把这个会话下的其它设备一起停了。"""
        tA, _ = temp_db.create_app_token(role="user", user_id="A")
        tB, _ = temp_db.create_app_token(role="user", user_id="B")
        temp_db.register_device(app_token_id=tA, device_token=self.TOK)
        temp_db.register_device(app_token_id=tA, device_token="other" + "0" * 59)
        temp_db.register_device(app_token_id=tB, device_token=self.TOK)

        remaining = temp_db.get_active_devices_for_user("A")
        assert [d["device_token"] for d in remaining] == ["other" + "0" * 59]


class TestExpiredSessionsStopPushing:
    """``revoked=0`` 不等于「会话还有效」——到期的会话也不该再推。

    鉴权侧 (``app.api_auth``) 判的是 ``expires_at < now`` 即失效；推送侧此前
    只看 ``revoked``，于是一个已经过期、用户那边早就登出了的会话仍在收推送。
    2026-08-07 查production 时受影响 0 台，只是因为 90 天 TTL 一个都还没到。
    """

    @staticmethod
    def _expire(db, token_id: str | int) -> None:
        db.conn.execute(
            "UPDATE app_tokens SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (token_id,),
        )
        db.conn.commit()

    def test_expired_user_session_not_pushable(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="u", ttl_days=90)
        temp_db.register_device(app_token_id=tid, device_token="x" * 64)
        assert len(temp_db.get_active_devices_for_user("u")) == 1

        self._expire(temp_db, tid)
        assert temp_db.get_active_devices_for_user("u") == []

    def test_expired_admin_session_not_pushable(self, temp_db):
        tid, _ = temp_db.create_app_token(role="admin", user_id=None, ttl_days=90)
        temp_db.register_device(app_token_id=tid, device_token="y" * 64)
        assert len(temp_db.get_active_devices_for_admin()) == 1

        self._expire(temp_db, tid)
        assert temp_db.get_active_devices_for_admin() == []

    def test_never_expiring_session_still_pushable(self, temp_db):
        """``expires_at IS NULL`` 是「永不过期」，别被新判据误杀。"""
        tid, _ = temp_db.create_app_token(role="user", user_id="u")
        temp_db.register_device(app_token_id=tid, device_token="z" * 64)
        assert len(temp_db.get_active_devices_for_user("u")) == 1
