"""Telegram 永久性失败的处理。

回归背景：用户拉黑 bot 后，Telegram 每次都回
``403 Forbidden: bot was blocked by the user``。旧实现把它和限流 / 5xx 一样
当普通失败，于是每轮重试、每轮刷 4–6 条 ERROR，且永远不会好转。
"""
from __future__ import annotations

import pytest

from notifier import TelegramNotifier, _telegram_permanent_reason

_URL = "https://api.telegram.org/bottoken/sendMessage"


class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text
        self.ok = 200 <= status < 300


class _Session:
    """记录实际发出的请求次数。"""

    def __init__(self, *responses: _Resp):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json, timeout):
        self.calls += 1
        return self._responses.pop(0) if self._responses else _Resp(200, "{}")

    def close(self):
        pass


def _notifier(session: _Session, on_fail=None) -> TelegramNotifier:
    n = TelegramNotifier("token", "chat", on_permanent_failure=on_fail)
    n._session = session
    return n


# ── 分类 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("body", [
    '{"ok":false,"error_code":403,"description":"Forbidden: bot was blocked by the user"}',
    '{"ok":false,"error_code":403,"description":"Forbidden: the bot can\'t send messages to the bot"}',
    '{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}',
])
def test_permanent_errors_are_recognised(body):
    assert _telegram_permanent_reason(403 if '"error_code":403' in body else 400, body)


@pytest.mark.parametrize("status,body", [
    (429, '{"ok":false,"error_code":429,"description":"Too Many Requests: retry after 30"}'),
    (500, "Internal Server Error"),
    (502, "Bad Gateway"),
    (400, '{"ok":false,"error_code":400,"description":"Bad Request: message is too long"}'),
])
def test_transient_errors_are_not_treated_as_permanent(status, body):
    """限流 / 5xx / 可修正的请求错误必须继续重试，不能被误停。"""
    assert _telegram_permanent_reason(status, body) is None


# ── 行为 ────────────────────────────────────────────────────────────
def test_permanent_failure_stops_further_api_calls():
    """停用后不再打 API——否则要等下次热重载才会停，中间照样每轮刷错。"""
    blocked = _Resp(403, '{"description":"Forbidden: bot was blocked by the user"}')
    session = _Session(blocked)
    n = _notifier(session)

    assert n._post(_URL, "第一条") is False
    assert session.calls == 1

    for _ in range(5):
        assert n._post(_URL, "后续") is False
    assert session.calls == 1, "永久失败后不应再发请求"


def test_permanent_failure_invokes_callback_once():
    reasons: list[str] = []
    session = _Session(
        _Resp(403, '{"description":"Forbidden: bot was blocked by the user"}')
    )
    n = _notifier(session, on_fail=reasons.append)

    for _ in range(4):
        n._post(_URL, "x")

    assert reasons == ["bot was blocked by the user"]


def test_transient_failure_keeps_retrying_and_does_not_disable():
    """限流之后恢复：不该触发停用回调，也不该短路后续请求。"""
    reasons: list[str] = []
    session = _Session(
        _Resp(429, '{"description":"Too Many Requests: retry after 1"}'),
        _Resp(200, "{}"),
    )
    n = _notifier(session, on_fail=reasons.append)

    assert n._post(_URL, "第一条") is False
    assert n._post(_URL, "第二条") is True
    assert session.calls == 2
    assert reasons == []


def test_disable_channel_removes_telegram_but_keeps_credentials(isolated_data_dir):
    """自动停用只摘渠道，凭据保留——解除拉黑后重新勾选即可，不用重填。"""
    from notifier import _disable_telegram_channel
    from users import UserConfig, load_users, save_users

    u = UserConfig(
        name="Blocked",
        id="cafebabe",
        notification_channels=["telegram", "email"],
        telegram_token="123:AABB",
        telegram_chat_id="42",
    )
    save_users([u])

    _disable_telegram_channel(u.id, u.name, "bot was blocked by the user")

    saved = load_users()[0]
    assert saved.notification_channels == ["email"]
    assert saved.telegram_token == "123:AABB"
    assert saved.telegram_chat_id == "42"


def test_disable_channel_is_idempotent(isolated_data_dir):
    """同一用户重复触发不应报错，也不该动到其它渠道。"""
    from notifier import _disable_telegram_channel
    from users import UserConfig, load_users, save_users

    u = UserConfig(
        name="Blocked", id="cafebabe",
        notification_channels=["telegram"],
        telegram_token="t", telegram_chat_id="1",
    )
    save_users([u])

    _disable_telegram_channel(u.id, u.name, "chat not found")
    _disable_telegram_channel(u.id, u.name, "chat not found")

    assert load_users()[0].notification_channels == []


def test_disable_channel_tolerates_missing_user(isolated_data_dir):
    from notifier import _disable_telegram_channel
    from users import UserConfig, save_users

    save_users([UserConfig(name="Other", id="0badf00d")])
    _disable_telegram_channel("nosuchid", "Ghost", "chat not found")  # 不该抛


def test_callback_error_does_not_break_sending_path():
    """回调里落库失败不应该把通知链路带崩。"""
    def _boom(reason):
        raise RuntimeError("DB 写入失败")

    session = _Session(
        _Resp(403, '{"description":"Forbidden: bot was blocked by the user"}')
    )
    n = _notifier(session, on_fail=_boom)

    assert n._post(_URL, "x") is False
