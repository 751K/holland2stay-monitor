"""
用户配置页的「App 推送」卡片是说明，不是渠道。

推送走 device_tokens（App 登录时登记），不在 notification_channels 里。给这张卡
加一个真的 checkbox 会往那个字段写进一个 create_user_notifier 不认识的值——日志里
是「未知通知渠道」，而且永远不会投递。界面上却看着像配好了。

所以这批用例盯的是「它没有变成一个渠道」，而不是它长什么样。
"""
from __future__ import annotations

import re

import pytest


def _form_html(client, path="/users/new"):
    r = client.get(path)
    assert r.status_code == 200, f"{path} 返回 {r.status_code}"
    return r.get_data(as_text=True)


class TestNotAChannel:
    def test_card_is_present(self, admin_client):
        html = _form_html(admin_client)
        assert 'id="sec_apppush"' in html

    def test_card_has_no_checkbox(self, admin_client):
        """卡片本体不能带 checkbox。

        带了就会被 updateChannelsInput() 收进 NOTIFICATION_CHANNELS。
        """
        html = _form_html(admin_client)
        i = html.index('toggleInfoCard(\'apppush\')')
        # 取这张卡的 label 区块：从 <label 到最近的 </label>
        start = html.rindex("<label", 0, i)
        end = html.index("</label>", i)
        card = html[start:end]
        assert "<input" not in card, "「App 推送」卡片里出现了输入控件"
        assert "channel-chk" not in card

    def test_it_is_not_in_the_channel_list(self, admin_client):
        """data-ch 的取值只能是后端认得的那四个。"""
        html = _form_html(admin_client)
        chans = set(re.findall(r'data-ch="([a-z]+)"', html))
        assert chans == {"imessage", "telegram", "email", "whatsapp"}, chans
        assert "apppush" not in chans

    def test_backend_would_not_understand_it(self):
        """反向确认这条判据的依据：notifier 确实不认识 apppush。

        这条不测界面，测的是「为什么不能把它做成渠道」。哪天 notifier 真的支持了
        一个叫 apppush 的渠道，这条会红，提醒改界面。
        """
        import inspect

        import notifier

        src = inspect.getsource(notifier.create_user_notifier)
        for known in ("imessage", "telegram", "email", "whatsapp"):
            assert f'"{known}"' in src or f"'{known}'" in src, known
        assert "apppush" not in src


class TestContent:
    @pytest.mark.parametrize("needle", [
        "apps.apple.com",                      # iOS 下载
        "app-release.apk",                     # Android 下载
    ])
    def test_download_links(self, admin_client, needle):
        assert needle in _form_html(admin_client)

    @pytest.mark.parametrize("lang,needle", [
        ("zh", "使用账号登录即可收到通知"),
        ("en", "sign in with this account"),
    ])
    def test_explains_that_signing_in_is_enough(self, admin_client, lang, needle):
        """两种语言都要有说明——只写一半等于对那半边用户什么都没说。

        cookie 必须设在**发请求的那个 client** 上。设在别的 client 上页面会按
        默认语言（en）渲染，中文断言于是因为语言而不是因为内容失败。
        """
        admin_client.set_cookie("h2s-lang", lang)
        assert needle in _form_html(admin_client)

    def test_device_count_shown_when_present(self, admin_client, test_app, monkeypatch):
        """已连接设备时显示台数，而不是继续劝人去安装。"""
        from app.routes import users as users_routes

        class FakeStore:
            def get_active_devices_for_user(self, uid):
                return [{"id": 1}, {"id": 2}]

            def close(self):
                pass

        from app.auth import _REGISTER_RECORDS
        _REGISTER_RECORDS.clear()
        r = admin_client.post("/users/new", data={
            "csrf_token": "test_csrf", "name": "AppPushProbe",
            "enabled": "true", "NOTIFICATIONS_ENABLED": "true",
        }, follow_redirects=False)
        assert r.status_code == 302, f"建用户失败 {r.status_code}"

        from users import load_users
        with test_app.app_context():
            uid = next(u.id for u in load_users() if u.name == "AppPushProbe")

        monkeypatch.setattr(users_routes, "storage", lambda: FakeStore())
        admin_client.set_cookie("h2s-lang", "zh")
        html = _form_html(admin_client, f"/users/{uid}")
        assert "已连接 2 台设备" in html, "有设备时仍在劝人去安装"
