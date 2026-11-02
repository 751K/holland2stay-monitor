"""
用户管理页的卡片上显示「这个人在哪些客户端登录着」。

判据是**活跃 token**，不是设备。这两者不一样：登录了但没给推送权限的客户端没有
``device_tokens`` 行，而它确实是一个登录着的客户端——照设备查会把这些人显示成
「未在 App 登录」，而他们明明登录着。

反过来，「活跃」的定义必须和推送那边（get_active_devices_for_user）逐字一致：
``revoked = 0`` 且未过期。少一条就会出现卡片上写着 iPhone、推送那边早已把它滤掉
的情况——一个在界面上看不出来的谎。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk(st, uid: str, name: str, *, ttl_days=90, revoked=False,
        expires_at: object = "keep", last_used: str | None = None) -> int:
    tid, _ = st.create_app_token(role="user", user_id=uid,
                                 device_name=name, ttl_days=ttl_days)
    if revoked:
        st.revoke_app_token(tid)
    if expires_at != "keep":
        with st.conn:
            st.conn.execute("UPDATE app_tokens SET expires_at = ? WHERE id = ?",
                            (expires_at, tid))
    if last_used:
        with st.conn:
            st.conn.execute("UPDATE app_tokens SET last_used_at = ? WHERE id = ?",
                            (last_used, tid))
    return tid


# ── 存储层 ──────────────────────────────────────────────────────

class TestQuery:
    def test_empty_db(self, temp_db):
        assert temp_db.get_active_clients_by_user() == {}

    def test_one_client(self, temp_db):
        _mk(temp_db, "u1", "iPhone")
        got = temp_db.get_active_clients_by_user()
        assert list(got) == ["u1"]
        assert got["u1"][0]["name"] == "iPhone"
        assert got["u1"][0]["sessions"] == 1

    def test_same_device_is_merged(self, temp_db):
        """同一台设备重装 / 重新登录各签一枚 token。

        线上有个用户在同一台 vivo 上攒了 7 枚——逐条列出来就是七个一模一样的
        标签，把卡片撑成一堵墙。
        """
        for _ in range(7):
            _mk(temp_db, "u1", "vivo V2458A")
        got = temp_db.get_active_clients_by_user()["u1"]
        assert len(got) == 1
        assert got[0]["sessions"] == 7

    def test_different_devices_are_separate(self, temp_db):
        _mk(temp_db, "u1", "iPhone")
        _mk(temp_db, "u1", "iPad")
        assert {c["name"] for c in temp_db.get_active_clients_by_user()["u1"]} \
            == {"iPhone", "iPad"}

    def test_users_do_not_bleed(self, temp_db):
        _mk(temp_db, "u1", "iPhone")
        _mk(temp_db, "u2", "vivo V2458A")
        got = temp_db.get_active_clients_by_user()
        assert got["u1"][0]["name"] == "iPhone"
        assert got["u2"][0]["name"] == "vivo V2458A"

    def test_most_recently_used_first(self, temp_db):
        now = datetime.now(timezone.utc)
        _mk(temp_db, "u1", "iPad", last_used=_iso(now - timedelta(days=30)))
        _mk(temp_db, "u1", "iPhone", last_used=_iso(now - timedelta(hours=1)))
        assert [c["name"] for c in temp_db.get_active_clients_by_user()["u1"]] \
            == ["iPhone", "iPad"]

    def test_blank_name_has_a_fallback(self, temp_db):
        """auth.py 空名时写「未命名设备」，但历史行里有真的空串。"""
        _mk(temp_db, "u1", "")
        assert temp_db.get_active_clients_by_user()["u1"][0]["name"] == "未命名设备"


class TestActiveCriterion:
    """「活跃」= revoked=0 且未过期。这三条是这次改动唯一可能出错的地方。"""

    def test_revoked_is_excluded(self, temp_db):
        _mk(temp_db, "u1", "iPhone", revoked=True)
        assert temp_db.get_active_clients_by_user() == {}

    def test_expired_is_excluded(self, temp_db):
        _mk(temp_db, "u1", "iPhone",
            expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)))
        assert temp_db.get_active_clients_by_user() == {}

    def test_never_expiring_is_included(self, temp_db):
        """expires_at IS NULL 是「永不过期」，不是「没有有效期所以不算」。"""
        _mk(temp_db, "u1", "iPhone", ttl_days=None)
        assert temp_db.get_active_clients_by_user()["u1"][0]["name"] == "iPhone"

    def test_admin_tokens_are_excluded(self, temp_db):
        """admin token 的 user_id 是 NULL，混进来会多出一个 key 为 None 的分组。"""
        temp_db.create_app_token(role="admin", user_id=None,
                                 device_name="cli", ttl_days=None)
        assert temp_db.get_active_clients_by_user() == {}

    def test_role_and_user_id_are_bound_together(self, temp_db):
        """SQL 里 role='user' 和 user_id IS NOT NULL 互为冗余，删掉任一条行为不变。

        撑着这个等价关系的只有 create_app_token 的一层校验——这条把它钉住。
        哪天校验松了，那两条 WHERE 就不再冗余，而是各自在挡不同的脏数据。
        """
        with pytest.raises(ValueError):
            temp_db.create_app_token(role="user", user_id=None, device_name="x")
        with pytest.raises(ValueError):
            temp_db.create_app_token(role="admin", user_id="u1", device_name="x")

    def test_revoked_and_active_on_the_same_device(self, temp_db):
        """撤销一枚、留一枚：设备还在，但 sessions 只数活着的那枚。"""
        _mk(temp_db, "u1", "iPhone", revoked=True)
        _mk(temp_db, "u1", "iPhone")
        got = temp_db.get_active_clients_by_user()["u1"]
        assert len(got) == 1 and got[0]["sessions"] == 1

    def test_criterion_matches_the_push_path(self, temp_db):
        """和 get_active_devices_for_user 用同一套条件。

        这条是整个改动的验收点：卡片上说「在线」，推送那边就必须真的发得出去。
        分叉的表现是界面和投递各说各话，而两边都不会报错。
        """
        tid = _mk(temp_db, "u1", "iPhone")
        temp_db.register_device(app_token_id=tid, device_token="d" * 64,
                                env="production", platform="ios")
        assert temp_db.get_active_clients_by_user().get("u1")
        assert temp_db.get_active_devices_for_user("u1")

        with temp_db.conn:
            temp_db.conn.execute(
                "UPDATE app_tokens SET expires_at = ? WHERE id = ?",
                (_iso(datetime.now(timezone.utc) - timedelta(days=1)), tid))

        assert not temp_db.get_active_clients_by_user().get("u1")
        assert temp_db.get_active_devices_for_user("u1") == []

    def test_client_without_a_device_still_counts(self, temp_db):
        """没给推送权限的客户端没有 device_tokens 行，但它登录着。

        照设备查会把这些人显示成「未在 App 登录」——线上 51 枚活跃 token 里有
        23 枚没有设备行，那是近一半的人。
        """
        _mk(temp_db, "u1", "iPhone")
        assert temp_db.get_active_devices_for_user("u1") == []
        assert temp_db.get_active_clients_by_user()["u1"][0]["name"] == "iPhone"


# ── 页面 ────────────────────────────────────────────────────────

def _mk_user(client, name: str) -> str:
    from app.auth import _REGISTER_RECORDS
    _REGISTER_RECORDS.clear()
    r = client.post("/register", data={
        "csrf_token": "test_csrf", "register_username": name,
        "register_password": "pw1234", "terms_accepted": "1",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    from users import load_users
    return next(u.id for u in load_users() if u.name == name)


def _enable_notifications(uid: str) -> None:
    from users import update_users

    def _on(us):
        for u in us:
            if u.id == uid:
                u.notifications_enabled = True
                break

    update_users(_on)


def _row(html: str, name: str) -> str:
    """截出某个用户在左侧列表里的那一行。

    断言必须落在单个用户上：整页搜索的话，隔壁用户的内容会让「这个人没有某样东西」
    永远成立不了、也永远发现不了。
    """
    i = html.index(f"<strong>{name}</strong>")
    start = html.rindex("<a href=", 0, i)
    return html[start:html.index("</a>", i) + 4]


def _panel(html: str, name: str) -> str:
    """截出右侧详情里该用户的面板（每个用户一块，未选中的带 hidden）。"""
    uid = _row(html, name).split('data-user-id="', 1)[1].split('"', 1)[0]
    start = html.index(f'data-user-panel="{uid}"')
    nxt = html.find("data-user-panel=", start + 1)
    return html[start:nxt if nxt != -1 else len(html)]


def _card(html: str, name: str) -> str:
    return _row(html, name) + _panel(html, name)


class TestCard:
    def test_client_name_is_on_the_card(self, client, admin_client, test_app):
        uid = _mk_user(client, "CardWithPhone")
        with test_app.app_context():
            _enable_notifications(uid)      # 注册默认是关的，那条分支不画 pill
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "iPhone 17 Pro")
            finally:
                st.close()

        html = admin_client.get("/users").get_data(as_text=True)
        card = _card(html, "CardWithPhone")
        assert "iPhone 17 Pro" in _row(html, "CardWithPhone"), "列表行没写出客户端"
        assert "iPhone 17 Pro" in card

    def test_session_count_is_shown_when_merged(self, client, admin_client, test_app):
        uid = _mk_user(client, "CardManySessions")
        with test_app.app_context():
            _enable_notifications(uid)      # 注册默认是关的，那条分支不画 pill
            from app.db import storage
            st = storage()
            try:
                for _ in range(3):
                    _mk(st, uid, "vivo V2458A")
            finally:
                st.close()

        html = admin_client.get("/users").get_data(as_text=True)
        for part in (_row(html, "CardManySessions"), _panel(html, "CardManySessions")):
            assert part.count("vivo V2458A") == 1, "同一台设备被列了多次"
        assert "vivo V2458A ×3" in _panel(html, "CardManySessions")

    def test_single_session_has_no_count_suffix(self, client, admin_client, test_app):
        """一枚 token 不该显示「×1」——那是噪音，而且看着像出了什么事。"""
        uid = _mk_user(client, "CardOneSession")
        with test_app.app_context():
            _enable_notifications(uid)      # 注册默认是关的，那条分支不画 pill
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "iPad Air")
            finally:
                st.close()

        html = admin_client.get("/users").get_data(as_text=True)
        assert "iPad Air" in html
        assert "×1" not in html


    def test_active_token_counts_as_a_channel(self, client, admin_client, test_app):
        """有活跃 token 就是有通知渠道，和 Email 一样。

        这条钉的是判据本身：不是「配了 notification_channels」，是「有没有一条
        渠道」，而 App 登录就是其中一条。
        """
        uid = _mk_user(client, "CardTokenIsChannel")
        with test_app.app_context():
            _enable_notifications(uid)      # 渠道留空：只有 App
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "OnePlus 13")
            finally:
                st.close()

        card = _card(admin_client.get("/users").get_data(as_text=True),
                     "CardTokenIsChannel")
        assert "OnePlus 13" in card
        from translations import TRANSLATIONS
        assert not any(TRANSLATIONS["users_no_channels"][l] in card
                       for l in ("zh", "en")), "有 App 渠道却被写成「无通知渠道」"

    def test_channels_still_render_without_any_token(self, client, admin_client,
                                                     test_app):
        """配了 Email、没登录 App 的人照旧显示 Email。

        这条防的是把判据从「渠道 或 token」写成「token」——那样只用 Email 的人
        会一个 pill 都看不到，还被扣上「无通知渠道」。
        """
        uid = _mk_user(client, "CardEmailOnly")
        with test_app.app_context():
            from users import update_users

            def _on(us):
                for u in us:
                    if u.id == uid:
                        u.notifications_enabled = True
                        u.notification_channels = ["email"]
                        break

            update_users(_on)

        html = admin_client.get("/users").get_data(as_text=True)
        card = _card(html, "CardEmailOnly")
        assert "Email" in _row(html, "CardEmailOnly")
        assert "Not signed in" in _panel(html, "CardEmailOnly")
        from translations import TRANSLATIONS
        assert not any(TRANSLATIONS["users_no_channels"][l] in card
                       for l in ("zh", "en"))

    def test_test_notify_button_is_enabled_for_app_only_users(
            self, client, admin_client, test_app):
        """只登录了 App 的人也能点「测试通知」。

        /users/<id>/test 本来就会走设备推送（tests/test_onboarding.py 里
        test_device_only_user_gets_a_push_row 证过）。按钮此前只看
        notification_channels，把这批人的按钮置灰——推送发得出去，界面却说不行。
        """
        uid = _mk_user(client, "CardAppOnlyTest")
        with test_app.app_context():
            _enable_notifications(uid)
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "OnePlus 13")
            finally:
                st.close()

        card = _card(admin_client.get("/users").get_data(as_text=True),
                     "CardAppOnlyTest")
        btn = card[card.index("js-test-notify"):]
        btn = btn[:btn.index(">")]
        assert "disabled" not in btn, "只有 App 的用户被禁用了「测试通知」"

    def test_test_notify_button_stays_disabled_with_nothing(
            self, client, admin_client, test_app):
        """两样都没有还是该灰着——按钮的语义没被放宽成「永远能点」。"""
        uid = _mk_user(client, "CardNothingToTest")
        with test_app.app_context():
            _enable_notifications(uid)

        card = _card(admin_client.get("/users").get_data(as_text=True),
                     "CardNothingToTest")
        btn = card[card.index("js-test-notify"):]
        btn = btn[:btn.index(">")]
        assert "disabled" in btn

    def test_no_channel_and_no_token_still_warns(self, client, admin_client,
                                                 test_app):
        """两样都没有才是真的没有渠道——这句话得留得住。"""
        uid = _mk_user(client, "CardTrulyEmpty")
        with test_app.app_context():
            _enable_notifications(uid)

        card = _card(admin_client.get("/users").get_data(as_text=True),
                     "CardTrulyEmpty")
        from translations import TRANSLATIONS
        assert any(TRANSLATIONS["users_no_channels"][l] in card for l in ("zh", "en"))


    def test_pill_follows_the_master_switch_like_email_does(self, client,
                                                            admin_client, test_app):
        """关了总开关就和 Email 一样不显示——它是同级的一条渠道，不是旁注。

        通知已关闭那条分支本来就把整排换成一个红徽章；App pill 留在外面的话，
        它就成了这一排里唯一一个不跟开关走的东西。
        """
        uid = _mk_user(client, "CardNotifOff")      # 注册默认就是关的
        with test_app.app_context():
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "Xperia 1")
            finally:
                st.close()

        # 列表行跟总开关走；详情里照实列出登录着的客户端，那是事实不是渠道
        row = _row(admin_client.get("/users").get_data(as_text=True), "CardNotifOff")
        from translations import TRANSLATIONS
        assert any(TRANSLATIONS["users_notif_off"][l] in row for l in ("zh", "en"))
        assert "Xperia 1" not in row

    def test_last_used_tooltip_is_relative_not_raw_utc(self, client, admin_client,
                                                        test_app):
        """库里存 UTC，容器跑在 Europe/Amsterdam——直接渲染原文会差两小时。

        tests/test_timestamp_rendering.py 有一条全站正则守着这件事；这条钉的是
        这个 tooltip 本身还在，而不只是「没有渲染 UTC 原文」（把它整个删掉那条
        正则也会绿）。
        """
        uid = _mk_user(client, "CardTooltip")
        with test_app.app_context():
            _enable_notifications(uid)      # 注册默认是关的，那条分支不画 pill
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "iPhone SE",
                    last_used=_iso(datetime.now(timezone.utc) - timedelta(days=3)))
            finally:
                st.close()

        html = admin_client.get("/users").get_data(as_text=True)
        assert "3d ago" in html or "3天前" in html
        assert "iPhone SE" in html

    def test_user_without_a_token_shows_not_signed_in(self, client, admin_client):
        """没登录 App：详情里明说「未登录」，列表行不凭空写出客户端。"""
        _mk_user(client, "CardNoPhone")
        html = admin_client.get("/users").get_data(as_text=True)
        assert "Not signed in" in _panel(html, "CardNoPhone")
        assert " · " not in _row(html, "CardNoPhone").split('class="mail-meta">', 1)[1].split("</span>", 1)[0]

    def test_revoked_does_not_show_on_the_card(self, client, admin_client, test_app):
        uid = _mk_user(client, "CardRevoked")
        with test_app.app_context():
            from app.db import storage
            st = storage()
            try:
                _mk(st, uid, "SecretPhone9000", revoked=True)
            finally:
                st.close()

        html = admin_client.get("/users").get_data(as_text=True)
        assert "SecretPhone9000" not in html

    def test_page_survives_a_storage_failure(self, client, admin_client, monkeypatch):
        """客户端信息是附加信息，取不到不该让整个用户列表打不开。"""
        _mk_user(client, "CardResilient")
        from app.routes import users as users_routes

        class _Boom:
            def get_active_clients_by_user(self):
                raise RuntimeError("db gone")

            def close(self):
                pass

        monkeypatch.setattr(users_routes, "storage", lambda: _Boom())
        r = admin_client.get("/users")
        assert r.status_code == 200
        assert "CardResilient" in r.get_data(as_text=True)


def test_translation_keys_exist():
    from translations import TRANSLATIONS

    for k in ("users_client_last_used",):
        assert k in TRANSLATIONS, k
        assert TRANSLATIONS[k]["zh"] and TRANSLATIONS[k]["en"]
