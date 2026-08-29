"""
通知邮件的标题少一个左括号。

``_strip_leading_symbol`` 想去掉的是 ``⚠️`` / ``✅`` 这类装饰性 emoji，写法是
「删掉开头连续的非文字字符」。而所有通知的首行都是 ``[H2S] 新房源上架`` 这种
来源标记，``[`` 正好是非文字字符——于是每一封通知邮件的标题都少一个左括号，
主题行是 ``[FlatRadar] H2S] New Listing``。

同一个意图在 Telegram 那边另有一份实现，它的字符类里排除了 ``[``，一直是对的。
两份实现分叉，而分叉只在其中一条通道上显形——Telegram 的标题从来没出过问题，
也就没有任何信号提示邮件那边不对。
"""
from __future__ import annotations

import inspect
import re

import pytest

import notifier


# ── 邮件标题 ────────────────────────────────────────────────────

class TestSourceTagSurvives:
    @pytest.mark.parametrize("first_line", [
        "[H2S] New Listing",
        "[OD] Status Change",
        "[OC] Booking Successful!",
        "[XR] Lottery listings × 3",
        "[H2S] 新房源上架",
    ])
    def test_bracket_is_not_stripped(self, first_line):
        assert notifier._strip_leading_symbol(first_line) == first_line

    def test_subject_keeps_the_bracket(self):
        """主题行是用户在收件箱列表里唯一看得见的东西。"""
        subject = notifier._format_email_subject("[H2S] New Listing\n\nJames Wattstraat 77B3")
        assert subject == "[FlatRadar] [H2S] New Listing"

    def test_html_heading_keeps_the_bracket(self):
        html = notifier._format_email_html("[H2S] New Listing\n\nJames Wattstraat 77B3")
        assert "[H2S] New Listing" in html
        assert "H2S] New Listing" not in html.replace("[H2S] New Listing", "")

    def test_every_notification_first_line_is_affected(self):
        """这不是某一种通知的问题——所有格式化函数的首行都是 ``[来源] ...``。

        钉住这一点，是为了说明这条判据的影响面：改坏它不是漏掉一个标点，
        是所有邮件标题一起变形。
        """
        import inspect
        src = inspect.getsource(notifier)
        assert src.count('f"[{source}] ') >= 5


class TestDecorationStillStripped:
    """修的是判据太宽，不是把这个函数关掉。"""

    @pytest.mark.parametrize("raw,want", [
        ("⚠️ 抓取被限流（429）", "抓取被限流（429）"),
        ("✅ 抓取已恢复",        "抓取已恢复"),
        ("🚀 真实预订",          "真实预订"),
        ("• bullet",            "bullet"),
        ("—— dash",             "dash"),
    ])
    def test_emoji_and_bullets_go(self, raw, want):
        assert notifier._strip_leading_symbol(raw) == want

    def test_all_decoration_falls_back_to_the_original(self):
        """整行都是装饰时保留原文，而不是给出一个空标题。"""
        assert notifier._strip_leading_symbol("⚠️⚠️") == "⚠️⚠️"

    def test_empty_input(self):
        assert notifier._strip_leading_symbol("") == ""
        assert notifier._strip_leading_symbol(None) is None or \
            notifier._strip_leading_symbol(None) == ""


def test_email_and_telegram_share_one_implementation():
    """两处各写一份字符类就是这次的成因。

    分叉的表现只在其中一条通道上出现——Telegram 的标题一直是对的，因此没有
    任何信号提示邮件那边不对。
    """
    import inspect

    src = inspect.getsource(notifier)
    assert src.count(r"[^\w<@#/\[]") == 1, "又出现了第二份前缀字符类"
    for name in ("_strip_leading_symbol", "_strip_telegram_icon"):
        body = inspect.getsource(getattr(notifier, name))
        assert "_LEADING_DECORATION_RE" in body, name


@pytest.mark.parametrize("raw,want", [
    ("[H2S] New Listing", "[H2S] New Listing"),
    ("⚠️ 抓取被限流",       "抓取被限流"),
])
def test_both_strippers_agree(raw, want):
    assert notifier._strip_leading_symbol(raw) == want
    assert notifier._strip_telegram_icon(raw) == want


