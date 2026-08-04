"""新建用户时若已填 shared 收件邮箱，必须当场发验证邮件。

2026-08-04 线上发现：``send_verification_email_sync`` 只有两个调用点——
``user_edit()`` 的「email_to 变了」分支，和用户手点的「重发验证邮件」按钮。
``user_new()`` 一个都没有。

后果是一条走不出去的死路：在创建表单里直接填收件邮箱的用户，建完就是
``email_verified=0``，而且**永远收不到验证链接**——他不会再经过「邮箱变了」
那个分支（除非把邮箱改成别的再改回来）。而未验证在 notifier 那边是直接跳过
email 渠道的，于是用户一封通知都收不到，日志里只有一行
「Email(shared) 邮箱未验证，跳过」。

这里盯三件事：
1. 新建时填了 shared 邮箱 → 发验证邮件；
2. custom 模式 / 没填邮箱 → 不发（custom 由用户自管，不该借道 shared 发信）；
3. 发送失败不能影响用户创建——用户已经落库了，异常只能降级成提示。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


BASE = {
    "csrf_token": "test_csrf",
    "enabled": "true",
    "NOTIFICATIONS_ENABLED": "true",
    "NOTIFICATION_CHANNELS": "email",
}


def _post_new(admin_client, name, **extra):
    data = dict(BASE, name=name)
    data.update(extra)
    return admin_client.post("/users/new", data=data, follow_redirects=False)


def _user(name):
    from users import load_users
    return next(u for u in load_users() if u.name == name)


class TestSendsOnCreate:
    def test_shared_email_triggers_verification(self, admin_client):
        with patch("app.email_verify.send_verification_email_sync",
                   return_value=True) as send:
            r = _post_new(admin_client, "NewShared",
                          EMAIL_MODE="shared", EMAIL_TO="a@example.com")

        assert r.status_code == 302
        assert send.call_count == 1, "新建用户时没有发验证邮件"

        u = _user("NewShared")
        # 传的必须是刚建出来那个用户的 id，不能是别人的
        user_id, user_name, email = send.call_args.args
        assert user_id == u.id
        assert user_name == "NewShared"
        assert email == "a@example.com"

        # 邮件发出去不等于已验证——仍要等用户点链接
        assert u.email_verified is False

    def test_user_is_created_even_if_send_fails(self, admin_client):
        """用户已经落库了，发信异常只能降级成提示，不能把创建也搭进去。"""
        with patch("app.email_verify.send_verification_email_sync",
                   side_effect=RuntimeError("resend down")):
            r = _post_new(admin_client, "SendBoom",
                          EMAIL_MODE="shared", EMAIL_TO="b@example.com")

        assert r.status_code == 302
        assert _user("SendBoom").email_to == "b@example.com"

    def test_config_error_does_not_break_creation(self, admin_client):
        """PUBLIC_BASE_URL 没配时抛的是 EmailVerifyConfigError，同样只降级。"""
        from app.email_verify import EmailVerifyConfigError

        with patch("app.email_verify.send_verification_email_sync",
                   side_effect=EmailVerifyConfigError("PUBLIC_BASE_URL 未配置")):
            r = _post_new(admin_client, "NoBaseUrl",
                          EMAIL_MODE="shared", EMAIL_TO="c@example.com")

        assert r.status_code == 302
        assert _user("NoBaseUrl").email_to == "c@example.com"


class TestDoesNotSend:
    def test_no_email_no_send(self, admin_client):
        with patch("app.email_verify.send_verification_email_sync") as send:
            _post_new(admin_client, "NoEmail",
                      NOTIFICATION_CHANNELS="imessage",
                      IMESSAGE_RECIPIENT="+15550000000")
        send.assert_not_called()

    def test_custom_mode_no_send(self, admin_client):
        """custom 模式用户自管 SMTP，不经过 shared 的 double opt-in。"""
        with patch("app.email_verify.send_verification_email_sync") as send:
            _post_new(admin_client, "CustomMode",
                      EMAIL_MODE="custom",
                      EMAIL_TO="d@example.com",
                      EMAIL_SMTP_HOST="smtp.example.com",
                      EMAIL_FROM="d@example.com")
        send.assert_not_called()


class TestEditPathUnchanged:
    """新建那条路补上之后，编辑那条原有语义不能变。"""

    def test_edit_without_email_change_does_not_resend(self, admin_client):
        with patch("app.email_verify.send_verification_email_sync",
                   return_value=True):
            _post_new(admin_client, "EditMe",
                      EMAIL_MODE="shared", EMAIL_TO="keep@example.com")
        uid = _user("EditMe").id

        # 只改名字，邮箱原样提交 → 不该再发一封
        with patch("app.email_verify.send_verification_email_sync") as send:
            r = admin_client.post(
                f"/users/{uid}",
                data=dict(BASE, name="EditMe2",
                          EMAIL_MODE="shared", EMAIL_TO="keep@example.com"),
            )
        assert r.status_code == 302
        send.assert_not_called()

    def test_edit_changing_email_resends(self, admin_client):
        with patch("app.email_verify.send_verification_email_sync",
                   return_value=True):
            _post_new(admin_client, "SwapMail",
                      EMAIL_MODE="shared", EMAIL_TO="old@example.com")
        uid = _user("SwapMail").id

        with patch("app.email_verify.send_verification_email_sync",
                   return_value=True) as send:
            admin_client.post(
                f"/users/{uid}",
                data=dict(BASE, name="SwapMail",
                          EMAIL_MODE="shared", EMAIL_TO="new@example.com"),
            )
        assert send.call_count == 1
        assert send.call_args.args[2] == "new@example.com"
