"""普通用户只收房源相关的推送，系统级消息一律只给 admin。

抓取被 403 屏蔽、source 熔断、429 限流、每小时的心跳——这些回答的都是「监控还
正常吗」，属于运维问题。普通用户既无从判断也无从处置，而每小时一条推送足够让人
把整个通知渠道静音，连真正的房源通知一起埋掉。

这条边界很容易在加新告警时被无意破坏：手边就有 user_notifiers，循环一发就完事。
所以这里守的是**清单本身**——用户渠道上只允许出现这四个方法。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

MONITOR = Path(__file__).resolve().parent.parent / "monitor.py"

#: 允许发给普通用户的推送。四种都是他要么能行动、要么明确关心的：
#: 新房源和状态变更是他订阅的内容；预订成功带着付款链接，不发等于白订；
#: 预订失败要让他知道得手动补上。
ALLOWED_USER_SENDS = {
    "send_new_listing",
    "send_status_change",
    "send_booking_success",
    "send_booking_failed",
}


def _user_notifier_calls() -> set[str]:
    """monitor.py 里对**用户** notifier 调用了哪些 send_* 方法。

    按 AST 找 ``<x>.send_*(...)``，排除 web_notifier（那是 admin 面板）。
    """
    tree = ast.parse(MONITOR.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or not fn.attr.startswith("send_"):
            continue
        recv = fn.value
        name = recv.id if isinstance(recv, ast.Name) else getattr(recv, "attr", "")
        if "web" in name or "admin" in name:
            continue
        found.add(fn.attr)
    return found


class TestOnlyListingPushesReachUsers:
    def test_no_unexpected_send_methods(self):
        extra = _user_notifier_calls() - ALLOWED_USER_SENDS
        assert not extra, (
            f"这些推送发到了用户渠道上：{sorted(extra)}。"
            "系统级消息请改用 _notify_admin_only()"
        )

    @pytest.mark.parametrize("method", sorted(ALLOWED_USER_SENDS))
    def test_the_allowed_ones_are_still_wired(self, method):
        """反向守一道：清单不能靠「把调用删光」来满足。"""
        assert method in _user_notifier_calls(), f"{method} 不再发给用户了"

    def test_errors_never_broadcast(self):
        """send_error 只应出现在 admin 路径上。

        原先有个 _broadcast_error() 把 403 熔断 / 403 屏蔽 / 429 限流三种系统故障
        广播给所有用户。
        """
        assert "send_error" not in _user_notifier_calls()

    def test_heartbeat_never_broadcast(self):
        """每小时一条「监控还活着，库里 N 条」——对用户是纯噪音。"""
        assert "send_heartbeat" not in _user_notifier_calls()

    def test_broadcast_helper_is_gone(self):
        """留着这个函数，下一个人会顺手再用一次。"""
        src = MONITOR.read_text()
        assert "_broadcast_error" not in src


class TestBlockedAutoBookGoesToTheRightPlace:
    """自动预订被屏蔽这条，用户该收——他开了自动预订，没订上，得手动补。

    但原先发的是给运维看的聚合文案，直接抄送给了每一个受影响的用户：

        🚫 自动预订被 403 屏蔽（12 套候选 / 5 个用户）
        ...
        影响用户: Wu, Yixin, Zhou, ...     ← 每个人都看到其他人的名字
    """

    def test_users_get_a_per_listing_notice(self):
        src = MONITOR.read_text()
        i = src.index("blocked_in_round and _should_notify_block()")
        block = src[i:i + 1600]
        assert "send_booking_failed" in block, "改成了不通知用户，那他不知道要手动补"
        assert "n.send_error(agg_msg)" not in block

    def test_aggregate_text_never_reaches_a_user(self):
        """「影响用户: A, B, C」只能出现在 admin 那条路上。"""
        src = MONITOR.read_text()
        i = src.index("影响用户")
        after = src[i:i + 900]
        # 聚合文案之后，只能看到 web_notifier / dispatch_admin 消费它
        assert "web_notifier.send_error(agg_msg)" in after
        for line in after.splitlines():
            if "send_" in line and "agg_msg" in line:
                assert "web_notifier" in line or "dispatch_admin" in line, \
                    f"聚合文案发给了用户: {line.strip()}"


class TestAdminStillGetsThem:
    """静音的是用户那一侧，运维信息本身不能丢。"""

    def test_admin_helper_covers_the_three_failures(self):
        import monitor

        src = inspect.getsource(monitor)
        for kind in ("h2s_circuit", "scrape_blocked", "scrape_rate_limited"):
            assert f'kind="{kind}"' in src, f"{kind} 没有走 admin 告警"

    def test_heartbeat_still_reaches_the_panel(self):
        import monitor

        src = inspect.getsource(monitor)
        assert "web_notifier.send_heartbeat" in src
