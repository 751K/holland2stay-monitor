"""
新用户引导：判据 + 面板上的呈现。

这批测试锁的是**判据**，不是排版。核心的一条是「设备算不算接收方式」——
线上 61 个用户里有 19 个只靠 APNs、没有任何外部渠道。判据里漏掉设备，就会
对着这 19 个人显示「你现在收不到通知」，而他们的手机一直在响。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.services.onboarding_service import delivery_state, route_labels


class FakeStore:
    def __init__(self, devices=0):
        self._n = devices

    def get_active_devices_for_user(self, user_id):
        return [{"id": i} for i in range(self._n)]


@dataclass
class FakeFilter:
    max_rent: float | None = None
    min_area: float | None = None
    allowed_cities: list = field(default_factory=list)
    allowed_energy: str = ""


@dataclass
class FakeUser:
    id: str = "u1"
    enabled: bool = True
    notifications_enabled: bool = True
    notification_channels: list = field(default_factory=list)
    listing_filter: FakeFilter = field(default_factory=FakeFilter)


# ── 判据 ────────────────────────────────────────────────────────

class TestReachable:
    def test_device_alone_is_enough(self):
        """只有设备、没有任何外部渠道 → 能收到。

        线上 19 个用户是这个形态。把 notification_channels 当成唯一判据，
        就会对着他们喊「你还没配通知渠道」——他们配了，配在手机上。
        """
        st = delivery_state(FakeStore(devices=1), FakeUser())
        assert st["reachable"] is True
        assert st["blocked_by"] is None
        assert st["routes"] == ["push"]

    def test_channel_alone_is_enough(self):
        st = delivery_state(FakeStore(devices=0),
                            FakeUser(notification_channels=["telegram"]))
        assert st["reachable"] is True
        assert st["routes"] == ["telegram"]

    def test_neither_is_blocked(self):
        st = delivery_state(FakeStore(devices=0), FakeUser())
        assert st["reachable"] is False
        assert st["blocked_by"] == "no_route"

    def test_toggle_off_blocks_even_with_device(self):
        """总开关关着 → 挡住，哪怕有设备。

        这条和 mcore/push.py 的闸是同一个语义。两边不一致的话，引导会说
        「已启用：设备推送」而实际一条都不发。
        """
        st = delivery_state(FakeStore(devices=2),
                            FakeUser(notifications_enabled=False,
                                     notification_channels=["email"]))
        assert st["reachable"] is False
        assert st["blocked_by"] == "toggle"

    def test_disabled_account_reported_separately(self):
        """账号停用和开关关闭是两回事，措辞不同——用户能自己开开关，
        但停用的账号得去找管理员。"""
        st = delivery_state(FakeStore(devices=1), FakeUser(enabled=False))
        assert st["blocked_by"] == "account"

    def test_device_query_failure_is_not_optimistic(self):
        """查设备失败时按 0 台算。

        反过来（失败即假设有设备）会让一个真的收不到通知的人看到「已配置」，
        而这正是引导要解决的那个问题本身。
        """
        class Broken:
            def get_active_devices_for_user(self, uid):
                raise RuntimeError("db gone")

        st = delivery_state(Broken(), FakeUser())
        assert st["device_count"] == 0
        assert st["reachable"] is False

    def test_blank_channel_strings_do_not_count(self):
        st = delivery_state(FakeStore(devices=0),
                            FakeUser(notification_channels=["", "  "]))
        assert st["reachable"] is False


# ── 筛选条件是提示，不是闸 ──────────────────────────────────────

class TestFilterIsNotAGate:
    def test_empty_filter_still_reachable(self):
        """筛选为空是合法状态（「什么都推给我」），不能算未完成。

        算进去的话，线上 30 个只想随便看看的用户会天天看到一条待办。
        """
        st = delivery_state(FakeStore(devices=1), FakeUser())
        assert st["filter_empty"] is True
        assert st["reachable"] is True, "筛选为空不该挡住通知"
        assert st["done"] is True

    def test_filter_count(self):
        st = delivery_state(FakeStore(devices=1), FakeUser(
            listing_filter=FakeFilter(max_rent=900, allowed_cities=["Eindhoven"])))
        assert st["filter_count"] == 2
        assert st["filter_empty"] is False


# ── 文案 ────────────────────────────────────────────────────────

class TestLabels:
    def test_single_device_has_no_count(self):
        st = delivery_state(FakeStore(devices=1), FakeUser())
        assert st["route_labels"] == ["设备推送"]

    def test_multiple_devices_show_count(self):
        """换过手机、旧设备还挂着的时候，这个数字是用户唯一的线索。"""
        st = delivery_state(FakeStore(devices=3), FakeUser())
        assert st["route_labels"] == ["设备推送（3）"]

    def test_english(self):
        st = delivery_state(FakeStore(devices=1),
                            FakeUser(notification_channels=["telegram"]), "en")
        assert st["route_labels"] == ["Device push", "Telegram"]


# ── 面板上的呈现 ────────────────────────────────────────────────

class TestDashboardRendering:
    def _login_as_new_user(self, client, name="OnbUser", lang="en"):
        """注册并登录。

        测试客户端不带 Accept-Language，get_lang() 会落到 en（本会话为了让
        Googlebot 拿到英文站改的）。断言中文文案的用例必须显式设 lang，
        否则会因为语言而不是因为逻辑失败——那种红是最浪费时间的一种。
        """
        from app.auth import _REGISTER_RECORDS
        _REGISTER_RECORDS.clear()
        r = client.post("/register", data={
            "csrf_token": "test_csrf",
            "register_username": name,
            "register_password": "pw1234",
            "terms_accepted": "1",
        }, follow_redirects=False)
        assert r.status_code in (302, 303), f"注册失败 {r.status_code}"
        client.set_cookie("h2s-lang", lang)
        return name

    @pytest.mark.parametrize("lang,phrase", [
        ("zh", "收不到通知"),
        ("en", "not receiving anything"),
    ])
    def test_new_user_sees_the_card(self, client, lang, phrase):
        """刚注册的用户：开关 False、无渠道、无设备 → 红卡。

        两种语言都测：这条卡是新用户看到的第一样东西，缺一套文案就等于对
        那半边用户什么都没说。
        """
        self._login_as_new_user(client, f"OnbFresh{lang}", lang=lang)
        html = client.get("/").get_data(as_text=True)
        assert 'id="onb-title"' in html, "新用户没看到引导卡"
        assert "onb-step-blocked" in html
        # 「收不到通知」这件事必须写出来，不能只给个待办勾
        assert phrase in html

    def test_card_links_to_the_users_own_settings(self, client, test_app):
        self._login_as_new_user(client, "OnbLink")
        with test_app.app_context():
            from users import load_users
            uid = next(u.id for u in load_users() if u.name == "OnbLink")
        html = client.get("/").get_data(as_text=True)
        assert f"/users/{uid}" in html, "「去设置」没指向他自己的配置页"

    def test_admin_does_not_see_it(self, admin_client):
        """admin 没有 UserConfig 行，引导对他没有意义。"""
        html = admin_client.get("/").get_data(as_text=True)
        assert 'id="onb-title"' not in html

    def test_guest_does_not_see_it(self, guest_client):
        html = guest_client.get("/").get_data(as_text=True)
        assert 'id="onb-title"' not in html

    def test_card_disappears_when_configured(self, client, test_app):
        """接收方式和筛选都配好 → 整块消失。引导不是常驻装饰。"""
        self._login_as_new_user(client, "OnbDone")
        with test_app.app_context():
            from users import update_users

            def _cfg(users):
                for u in users:
                    if u.name == "OnbDone":
                        u.notifications_enabled = True
                        u.notification_channels = ["telegram"]
                        u.telegram_token = "1:x"
                        u.telegram_chat_id = "1"
                        u.listing_filter.max_rent = 900
                        break

            update_users(_cfg)

        html = client.get("/").get_data(as_text=True)
        assert 'id="onb-title"' not in html, "都配好了还在显示引导"

    def test_reachable_but_empty_filter_is_soft(self, client, test_app):
        """能收到、只是没设筛选 → 软状态：不报警，只说明会发生什么。"""
        self._login_as_new_user(client, "OnbSoft", lang="en")
        with test_app.app_context():
            from users import update_users

            def _cfg(users):
                for u in users:
                    if u.name == "OnbSoft":
                        u.notifications_enabled = True
                        u.notification_channels = ["telegram"]
                        break

            update_users(_cfg)

        html = client.get("/").get_data(as_text=True)
        # 判据是「有没有报红」，不是某个 class 名——外壳在两种状态下是同一套
        # 样式，区别只在标题和那一步自己的颜色。
        assert "onb-step-blocked" not in html, "已经能收到了却还在报红"
        assert "Alerts are on" in html
        assert "every listing" in html, "没说清楚「筛选为空」会导致什么"

    def test_dashboard_survives_a_broken_onboarding_state(self, client, monkeypatch):
        """引导算不出来时首页照常打开。

        一块辅助信息不该把主页面拖下水——尤其是这块只对新用户显示，
        坏了的话恰好打在最不该被劝退的那批人身上。
        """
        self._login_as_new_user(client, "OnbBroken")
        from app.routes import dashboard

        def _boom(*_a, **_kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(dashboard, "delivery_state", _boom)
        r = client.get("/")
        assert r.status_code == 200
        assert 'id="onb-title"' not in r.get_data(as_text=True)


# ── 「确认能收到」必须测真正生效的那条路 ────────────────────────

class TestTestNotifyCoversPush:
    """/users/<id>/test 原来只遍历 notification_channels。

    那个字段只列外部渠道，APNs / FCM 走的是 device_tokens。于是一个只用 App、
    没配邮件的用户（线上 19 个）点「确认能收到」会得到「未配置任何通知渠道」，
    而他的投递明明是好的。按钮承诺「确认能收到」，就得测真正在生效的那条路。
    """

    def _mk(self, client, name):
        from app.auth import _REGISTER_RECORDS
        _REGISTER_RECORDS.clear()
        r = client.post("/register", data={
            "csrf_token": "test_csrf", "register_username": name,
            "register_password": "pw1234", "terms_accepted": "1",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        from users import load_users
        return next(u.id for u in load_users() if u.name == name)

    def test_device_only_user_gets_a_push_row(self, client, test_app, monkeypatch):
        from app.routes import users as users_routes

        uid = self._mk(client, "PushOnly")
        with test_app.app_context():
            from users import update_users

            def _on(us):
                for u in us:
                    if u.id == uid:
                        u.notifications_enabled = True   # 渠道留空：只有设备
                        break

            update_users(_on)

        monkeypatch.setattr(users_routes, "_test_push_to_devices",
                            lambda _uid: [{"channel": "设备推送", "ok": True,
                                           "error": None}])
        r = client.post(f"/users/{uid}/test", headers={"X-CSRF-Token": "test_csrf"})
        body = r.get_json()
        assert body["ok"] is True
        assert body["results"] == [{"channel": "设备推送", "ok": True, "error": None}]

    def test_toggle_off_means_no_test_push(self, client, test_app, monkeypatch):
        """总开关关着时真实投递不推，测试也不能推。

        测试结果比真实投递乐观，比不测还糟——用户会以为配好了。
        """
        from app.routes import users as users_routes

        uid = self._mk(client, "PushOffProbe")   # 注册后开关就是 False
        called = []
        monkeypatch.setattr(users_routes, "_test_push_to_devices",
                            lambda _uid: called.append(_uid) or [])
        client.post(f"/users/{uid}/test", headers={"X-CSRF-Token": "test_csrf"})
        assert called == [], "开关关着还去发测试推送了"

    def test_no_devices_is_not_a_failure_row(self, test_app, monkeypatch):
        """没有设备时返回空列表，而不是一条「失败」。

        一个只配了邮件的用户不该在结果里看到「设备推送 ✗」——他从来没打算
        用设备推送，那条红叉只会让他去查一个不存在的问题。
        """
        from app.routes.users import _test_push_to_devices
        from app.routes import users as users_routes

        class NoDevices:
            def get_active_devices_for_user(self, uid):
                return []

            def close(self):
                pass

        monkeypatch.setattr(users_routes, "storage", lambda: NoDevices(),
                            raising=False)
        with test_app.app_context():
            import app.db
            monkeypatch.setattr(app.db, "storage", lambda: NoDevices())
            assert _test_push_to_devices("whoever") == []
