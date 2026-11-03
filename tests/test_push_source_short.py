"""推送标题里的平台缩写来自 ``mcore.sources`` 的注册表。

push.py 曾手写一份只认 H2S / OD 的缩写表，于是 Xior 的推送标题是 ``[XIOR]``、
Student Experience 是 ``[STUDENTEXPERIENCE]``，而文本通知里早就是 XR / SE。
"""
from __future__ import annotations

import dataclasses

import pytest

import notifier
from mcore import push
from mcore.push import _fcm_payload_new_listing, _payload_new_listing
from mcore.sources import SPECS


@dataclasses.dataclass
class _L:
    source: str
    id: str = "l1"
    name: str = "Kastanjelaan 1-639"
    city: str = "Eindhoven"
    status: str = "Available to book"
    price_display: str = "€700"
    available_from: str = "2026-06-01"

    def feature_map(self) -> dict:
        return {"area": "26 m²"}


@pytest.mark.parametrize("source", sorted(SPECS))
def test_push_titles_use_the_registered_short_name(source):
    short = SPECS[source].short_name
    apns = _payload_new_listing(_L(source), lang="en")["aps"]["alert"]["title"]
    fcm = _fcm_payload_new_listing(_L(source), lang="en")["message"]["data"]["title"]
    assert short in apns
    assert short in fcm


@pytest.mark.parametrize("source", [*sorted(SPECS), None, "not-a-platform"])
def test_push_and_text_notifications_agree(source):
    assert push._source_short(source) == notifier._source_short(source)
