"""
token 的 expires_at 随使用滑动。

原来是登录时写死 now+90d、之后只读。到期那一刻 get_active_devices_for_user
的 JOIN 把设备滤掉、推送立即停——而推送正是把用户叫回 App 的唯一通道。拿不到
推送的人不会打开 App，也就看不到 401、不会重新登录。

2026-08-28 实测：48 个登录过 App 的用户里 9 个已经这么掉出去，重新登录回来的
0 人。其中两个在过期前三天还在用 App。

所以判据从「登录后 90 天」改成「停用 90 天」。这批测试锁的是**不该滑的那些
情况**——续期的正向行为一条就够，容易出事的全在边界上：已撤销的不能复活、
已过期的不能复活、NULL（永不过期）不能被安上到期日。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mstorage._tokens import SLIDING_TTL_DAYS


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def st(temp_db):
    """一个干净的 Storage。"""
    yield temp_db


def _row(st, tid: int) -> dict:
    r = st.conn.execute(
        "SELECT expires_at, last_used_at, revoked FROM app_tokens WHERE id = ?",
        (tid,),
    ).fetchone()
    return dict(r)


def _set_expiry(st, tid: int, when: datetime | None) -> None:
    with st.conn:
        st.conn.execute("UPDATE app_tokens SET expires_at = ? WHERE id = ?",
                        (_iso(when) if when else None, tid))


def _mk(st, ttl_days=SLIDING_TTL_DAYS, user_id="u1"):
    tid, _ = st.create_app_token(role="user", user_id=user_id,
                                 device_name="iPhone", ttl_days=ttl_days)
    return tid


# ── 该滑的 ──────────────────────────────────────────────────────

class TestSlides:
    def test_touch_extends_expiry(self, st):
        tid = _mk(st)
        _set_expiry(st, tid, datetime.now(timezone.utc) + timedelta(days=3))

        st.touch_app_tokens([tid])

        exp = datetime.fromisoformat(_row(st, tid)["expires_at"].replace("Z", "+00:00"))
        left = (exp - datetime.now(timezone.utc)).days
        assert left >= SLIDING_TTL_DAYS - 1, f"没续上，只剩 {left} 天"

    def test_last_used_at_still_updated(self, st):
        """续期是搭在原有那条 UPDATE 上的，不能把它挤掉。"""
        tid = _mk(st)
        assert _row(st, tid)["last_used_at"] is None
        st.touch_app_tokens([tid])
        assert _row(st, tid)["last_used_at"] is not None

    def test_batch(self, st):
        ids = [_mk(st, user_id=f"u{i}") for i in range(3)]
        for i in ids:
            _set_expiry(st, i, datetime.now(timezone.utc) + timedelta(days=1))
        st.touch_app_tokens(ids)
        for i in ids:
            exp = datetime.fromisoformat(_row(st, i)["expires_at"].replace("Z", "+00:00"))
            assert (exp - datetime.now(timezone.utc)).days >= SLIDING_TTL_DAYS - 1

    def test_empty_list_is_noop(self, st):
        st.touch_app_tokens([])   # 不该抛


# ── 不该滑的 ────────────────────────────────────────────────────

class TestDoesNotSlide:
    def test_revoked_token_is_not_resurrected(self, st):
        """已撤销的不能靠「被用了一下」复活。

        撤销是用户或管理员的明示意图。异步 flush 和 5 分钟 token 缓存意味着
        撤销之后仍可能有 id 排在待刷队列里——判断必须在 SQL 的 WHERE 里，
        放到 Python 里做就会被这个竞态绕过。
        """
        tid = _mk(st)
        # 必须先把到期日挪开。留着建 token 时的 now+90d 的话，就算续期真的
        # 执行了也是写回同一秒的同一个值，before == after 会让这条用例在
        # 「撤销判据被删掉」时照样变绿——测了个寂寞。
        st.revoke_app_token(tid)
        _set_expiry(st, tid, datetime.now(timezone.utc) + timedelta(days=2))
        before = _row(st, tid)["expires_at"]

        st.touch_app_tokens([tid])

        assert _row(st, tid)["expires_at"] == before, "撤销的 token 被续期了"
        assert _row(st, tid)["revoked"] == 1

    def test_already_expired_is_not_resurrected(self, st):
        """已经死了的不能复活。

        能走到 touch 说明请求当时通过了鉴权，但 flush 是异步的、token 行还有
        5 分钟缓存，跨过期点的窗口真实存在。复活一个已过期 token 等于让 401
        之后的请求又变回 200。
        """
        tid = _mk(st)
        _set_expiry(st, tid, datetime.now(timezone.utc) - timedelta(minutes=1))
        before = _row(st, tid)["expires_at"]

        st.touch_app_tokens([tid])

        assert _row(st, tid)["expires_at"] == before, "过期的 token 被复活了"

    def test_never_expiring_token_stays_null(self, st):
        """expires_at IS NULL 是「永不过期」，续期不能给它安一个到期日。

        反过来会把一个长期 admin token 变成 90 天后失效，而没有任何地方报错，
        直到那天它突然不认了。

        注：SQL 里那道 ``expires_at IS NOT NULL`` 其实是冗余的——``expires_at
        >= ?`` 已经排除了 NULL 行（SQLite 三值逻辑，``NULL >= x`` 得 NULL，
        WHERE NULL 不成立）。留着是不想让一个安全相关的判据依赖三值逻辑的
        细节，将来有人改写那条 WHERE 时不会踩空。所以删掉它这条用例仍然绿，
        那是等价变异，不是覆盖漏洞。
        """
        tid, _ = st.create_app_token(role="admin", user_id=None,
                                     device_name="cli", ttl_days=None)
        assert _row(st, tid)["expires_at"] is None

        st.touch_app_tokens([tid])

        assert _row(st, tid)["expires_at"] is None, "永不过期的 token 被安上了到期日"

    def test_longer_expiry_is_not_shortened(self, st):
        """手工设过更长到期日的，续期不该把它砍回 90 天。"""
        tid = _mk(st)
        far = datetime.now(timezone.utc) + timedelta(days=365)
        _set_expiry(st, tid, far)

        st.touch_app_tokens([tid])

        exp = datetime.fromisoformat(_row(st, tid)["expires_at"].replace("Z", "+00:00"))
        assert (exp - datetime.now(timezone.utc)).days > SLIDING_TTL_DAYS + 1


# ── 端到端：设备可见性 ──────────────────────────────────────────

class TestDeviceVisibility:
    """真正要保住的东西不是 expires_at 这个字段，是「推送还发得出去」。

    投递路径查的是 get_active_devices_for_user，它 JOIN app_tokens 过滤
    expires_at。所以断言落在设备可见性上，而不是字段值上。
    """

    def _with_device(self, st, uid="udev"):
        tid = _mk(st, user_id=uid)
        st.register_device(app_token_id=tid, device_token="d" * 64,
                           env="production", platform="ios")
        return tid, uid

    def test_expiry_hides_the_device(self, st):
        tid, uid = self._with_device(st)
        assert len(st.get_active_devices_for_user(uid)) == 1

        _set_expiry(st, tid, datetime.now(timezone.utc) - timedelta(days=1))
        assert st.get_active_devices_for_user(uid) == [], "过期后设备居然还可见"

    def test_touch_keeps_the_device_visible(self, st):
        """这条是整个改动的验收条件：还在用 App 的人不会因为一个固定到期日
        而在第 90 天突然收不到推送。"""
        tid, uid = self._with_device(st)
        # 快到期了
        _set_expiry(st, tid, datetime.now(timezone.utc) + timedelta(hours=1))

        st.touch_app_tokens([tid])          # 用户打开了一次 App

        # 把时钟往后推 30 天的效果：直接看新的到期日够不够远
        exp = datetime.fromisoformat(_row(st, tid)["expires_at"].replace("Z", "+00:00"))
        assert (exp - datetime.now(timezone.utc)).days >= SLIDING_TTL_DAYS - 1
        assert len(st.get_active_devices_for_user(uid)) == 1


# ── 常量不能分叉 ────────────────────────────────────────────────

def test_login_ttl_matches_sliding_ttl():
    """登录签发的 TTL 和续期用的必须是同一个数。

    两处各写一个 90 迟早分叉，而分叉的表现是「登录发的和续期发的不一样长」，
    不会有任何地方报错——只会让某些 token 莫名其妙比别的短命。
    """
    from app.routes.api_v1 import auth

    assert auth.DEFAULT_TTL_DAYS == SLIDING_TTL_DAYS
    assert auth.MAX_TTL_DAYS == SLIDING_TTL_DAYS
