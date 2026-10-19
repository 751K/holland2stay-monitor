"""env_utils 的布尔解析语义。

这里守的核心是一条真实事故：``APNS_ENABLED=" true"``（带空格）曾被判成未启用，
推送静默关闭——四套手写解析都不 ``strip()``。
"""
from __future__ import annotations

import pytest

from env_utils import env_bool, parse_bool


@pytest.mark.parametrize("raw,want", [
    ("true", True),
    (" true ", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("on", True),
    ("false", False),
    (" false ", False),
    ("0", False),
    ("no", False),
    ("off", False),
    ("", False),
    (None, False),
    ("garbage", False),
])
def test_parse_bool(raw, want):
    assert parse_bool(raw) == want


def test_default_true_keeps_unrecognised_values():
    """默认开的开关：无法识别的值回落到 True，与旧行为一致。"""
    assert parse_bool("garbage", True) is True
    assert parse_bool("", True) is True
    assert parse_bool(None, True) is True


def test_env_bool_strips_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("FEATURE_FLAG", " true ")
    assert env_bool("FEATURE_FLAG") is True


def test_env_bool_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("FEATURE_FLAG", raising=False)
    assert env_bool("FEATURE_FLAG", True) is True
    assert env_bool("FEATURE_FLAG", False) is False
