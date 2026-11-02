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
    @staticmethod
    def _apppush_panel(html: str) -> str:
        start = html.index('data-panel="notifications:apppush"')
        return html[start:html.index('data-panel=', start + 1)]

    def test_card_is_present(self, admin_client):
        assert 'data-panel="notifications:apppush"' in _form_html(admin_client)

    def test_card_has_no_checkbox(self, admin_client):
        """这一块不能带渠道开关：带了就会被收进 NOTIFICATION_CHANNELS。"""
        panel = self._apppush_panel(_form_html(admin_client))
        assert "channel-chk" not in panel, "「App 推送」里出现了渠道开关"
        assert 'type="checkbox"' not in panel

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

    def test_the_android_link_points_at_the_repo_that_publishes_it(self, admin_client):
        """光有 ``app-release.apk`` 这个串不够——它不说链接指向哪个仓库。

        2026-09-05 安卓客户端迁到 751K/FlatRadar-Android，Release 从那边发。这个
        页面上的链接当时还指着 holland2stay-monitor，而上面那条断言照样是绿的：
        子串还在，只是它会把人送到一个**再也不会有新版本**的 Release 页。

        「链接存在」和「链接指对地方」是两件事，后者才是用户点下去关心的。
        """
        html = _form_html(admin_client)
        assert "751K/FlatRadar-Android/releases/latest/download/app-release.apk" in html
        assert "holland2stay-monitor/releases/latest/download/app-release.apk" not in html

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

    def test_device_count_shown_when_present(self, admin_client, test_app):
        """已连接设备时显示台数，而不是继续劝人去安装。"""
        r = admin_client.post("/users/new", data={
            "csrf_token": "test_csrf", "name": "AppPushProbe",
            "enabled": "true", "NOTIFICATIONS_ENABLED": "true",
        }, follow_redirects=False)
        assert r.status_code == 302, f"建用户失败 {r.status_code}"
        from users import load_users
        with test_app.app_context():
            uid = next(u.id for u in load_users() if u.name == "AppPushProbe")
            from app.db import storage
            st = storage()
            try:
                for n in (1, 2):
                    tid, _ = st.create_app_token(role="user", user_id=uid, device_name=f"Phone {n}", ttl_days=90)
                    st.register_device(app_token_id=tid, device_token=f"{n:064x}")
            finally:
                st.close()
        admin_client.set_cookie("h2s-lang", "zh")
        html = _form_html(admin_client, f"/users/{uid}")
        assert "2 台设备已登录" in html, "有设备时仍在劝人去安装"

