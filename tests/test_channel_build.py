"""monitor 的真实投递和面板的测试通知，按同一个判据构造渠道。

测试通知原先逐个渠道另写了一份：SMTP 只填一半凭据时它报错，monitor 那边却按
「参数不完整」跳过；iMessage 在非 macOS 上它照发（然后失败），monitor 直接跳过。
"""
from __future__ import annotations

import pytest

import notifier
from notifier import CHANNEL_PROBLEMS, build_channel_notifier
from users import UserConfig


@pytest.mark.parametrize("channel,fields,problem", [
    ("telegram", {"telegram_token": "t"}, "telegram_incomplete"),
    ("email", {"email_mode": "custom", "email_smtp_host": "h", "email_to": "a@x",
               "email_from": "f@x", "email_username": "u"}, "email_smtp_auth_half"),
    ("email", {"email_mode": "custom", "email_to": "a@x"}, "email_smtp_incomplete"),
    ("email", {"email_mode": "shared"}, "email_shared_no_recipient"),
    ("whatsapp", {"twilio_sid": "s"}, "whatsapp_incomplete"),
    ("pigeon", {}, "unknown"),
])
def test_incomplete_channels_report_a_known_problem(channel, fields, problem):
    b = build_channel_notifier(UserConfig(name="u", **fields), channel)
    assert b.notifier is None
    assert b.problem == problem
    assert problem in CHANNEL_PROBLEMS


def test_imessage_off_macos_is_skipped_everywhere(monkeypatch):
    monkeypatch.setattr(notifier, "is_macos", lambda: False)
    b = build_channel_notifier(UserConfig(name="u", imessage_recipient="+31"), "imessage")
    assert b.notifier is None and b.problem == "imessage_not_macos"


def test_monitor_gets_exactly_the_channels_the_builder_accepts():
    user = UserConfig(
        name="u", notification_channels=["telegram", "whatsapp", "pigeon"],
        telegram_token="t", telegram_chat_id="1",
    )
    built = notifier.create_user_notifier(user)
    kinds = [type(n).__name__ for n in built._notifiers]
    assert kinds == ["TelegramNotifier"]
