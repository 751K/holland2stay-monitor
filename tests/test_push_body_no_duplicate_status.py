"""新房源推送的正文里，状态只出现一次。

这个 bug 长什么样
----------------
锁屏上收到的是::

    Available to book · Available to book · €809/mo · 26 m² · 2026-09-09 move-in

起因是 335e248 的多语言改造：原先是 ``[listing.status, f"{price}/月"]``，
改造时把第二项换成了 ``_t("{status} · {price}/mo")``——新模板**自己就带 status**，
而第一项没删。APNs 和 FCM 两份 payload 是复制粘贴的关系，所以两边一起错。

为什么一直没被发现
------------------
既有的四条 payload 用例全部只断言 **title**（``"新房源" in title`` /
``startswith("[H2S]")``），没有一条看过 body。不是「测试把 bug 钉成了期望行为」，
是**根本没人看那一格**。所以这里断言的是正文本身，且用计数而不是相等——
``body != "…"`` 那种写法换个字段照样通过。
"""
from __future__ import annotations

import dataclasses

import pytest

from mcore.push import _fcm_payload_new_listing, _payload_new_listing


@dataclasses.dataclass
class _L:
    id: str = "l1"
    name: str = "Kastanjelaan 1-639"
    city: str = "Eindhoven"
    status: str = "Available to book"
    price_display: str = "€700"
    available_from: str = "2026-06-01"
    source: str = "holland2stay"

    def feature_map(self) -> dict:
        return {"area": "26 m²"}


def _apns_body(listing, lang: str) -> str:
    return _payload_new_listing(listing, lang=lang)["aps"]["alert"]["body"]


def _fcm_body(listing, lang: str) -> str:
    return _fcm_payload_new_listing(listing, lang=lang)["message"]["data"]["body"]


_RENDER = {"apns": _apns_body, "fcm": _fcm_body}


@pytest.mark.parametrize("channel", sorted(_RENDER))
@pytest.mark.parametrize("lang", ["en", "zh"])
class TestStatusAppearsOnce:
    def test_status_is_not_repeated(self, channel, lang):
        body = _RENDER[channel](_L(), lang)
        assert body.count("Available to book") == 1, f"状态重复了：{body}"

    def test_an_unusual_status_is_not_repeated_either(self, channel, lang):
        """换个状态再数一次——钉住「只出现一次」而不是钉住某个具体字面量。"""
        body = _RENDER[channel](_L(status="Lottery"), lang)
        assert body.count("Lottery") == 1, f"状态重复了：{body}"

    def test_status_is_still_there(self, channel, lang):
        """去重不能变成删掉——状态是这条通知最要紧的一格。"""
        assert "Available to book" in _RENDER[channel](_L(), lang)

    def test_the_other_fields_survive(self, channel, lang):
        """租金 / 面积 / 入住日一样都不能少，否则「修好了」只是正文被削短。"""
        body = _RENDER[channel](_L(), lang)
        assert "€700" in body
        assert "26 m²" in body
        assert "2026-06-01" in body


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_both_channels_render_the_same_body(lang):
    """两份 payload 是复制粘贴的关系——只修一边就是下一次的 bug。

    同一段逻辑写两遍、只改一处，是这个仓库反复出现的形状；这条断言让第二处
    漏改当场变红。
    """
    listing = _L()
    assert _apns_body(listing, lang) == _fcm_body(listing, lang)
