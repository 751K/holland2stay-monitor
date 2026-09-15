"""TLS 指纹池：名字必须真实存在，SecureRC 池必须按实测挑。

两个池各有用途（见 ``config._CURL_IMPERSONATE_POOL`` 与
``scrapers.ourdomain._DEFAULT_IMPERSONATES`` 的说明）：全局池给不挑指纹的平台，
SecureRC 池只放 2026-09-15 实测能过的。
"""
from __future__ import annotations

import re
from pathlib import Path

import curl_cffi
import pytest
from curl_cffi.requests import BrowserType

import config
from scrapers import ourdomain as od

_SUPPORTED = {b.value for b in BrowserType}
_LOCK = (Path(__file__).resolve().parent.parent / "requirements.lock").read_text(encoding="utf-8")
_PINNED = re.search(r"^curl_cffi==(\S+)$", _LOCK, re.M).group(1)

#: 2026-09-15 实测 SecureRC 全部 403 的指纹（curl_cffi 0.16.3）。WAF 策略变了可以
#: 重测后改这张表，但不能不测就把它们放回去——旧池排第一的 chrome136 就是这么每次
#: 冷启动白扔两次请求的。
_MEASURED_BLOCKED_ON_SECURERC = {
    "chrome146", "chrome145", "chrome142", "chrome136", "chrome133a",
    "safari260", "edge101", "chrome131_android",
}
#: 同一次实测全过的。
_MEASURED_CLEAN_ON_SECURERC = {
    "chrome150", "safari2601", "safari184", "safari184_ios", "safari18_0",
    "safari180", "safari180_ios", "safari17_2_ios", "safari172_ios", "safari170",
}


@pytest.mark.parametrize("name", sorted(set(config._CURL_IMPERSONATE_POOL) | set(od._DEFAULT_IMPERSONATES)))
def test_every_name_is_a_real_curl_cffi_target(name):
    """写错一个字母不会在导入时报错，只会在第一次请求时炸。

    按 requirements.lock 锁定的版本核对。本地装的版本不同时跳过并说明——
    chrome150 从 0.16 起才有，拿旧版核对只会误报。
    """
    if curl_cffi.__version__ != _PINNED:
        pytest.skip(
            f"本地 curl_cffi {curl_cffi.__version__} ≠ 锁定的 {_PINNED}，"
            f"pip install curl_cffi=={_PINNED} 后再核对")
    assert name in _SUPPORTED, f"{name!r} 不是 curl_cffi {BrowserType.__module__} 支持的 target"


def test_global_pool_weights_line_up():
    assert len(config._POOL_WEIGHTS) == len(config._CURL_IMPERSONATE_POOL)
    assert len(set(config._CURL_IMPERSONATE_POOL)) == len(config._CURL_IMPERSONATE_POOL)


def test_securerc_pool_has_no_fingerprint_measured_as_blocked():
    bad = set(od._DEFAULT_IMPERSONATES) & _MEASURED_BLOCKED_ON_SECURERC
    assert not bad, f"SecureRC 池里放进了实测 403 的指纹：{sorted(bad)}"


def test_first_four_are_all_measured_clean():
    """默认 OURDOMAIN_WAF_RETRIES=4 只取前 4 个——前 4 个必须都是 5/5 的。"""
    assert set(od._DEFAULT_IMPERSONATES[:4]) <= _MEASURED_CLEAN_ON_SECURERC


def test_attempts_do_not_start_with_a_global_pool_pick(monkeypatch):
    """全局池里有实测 403 的桌面 Chrome。插到 SecureRC 尝试列表最前，冷启动第一发就是 403。"""
    monkeypatch.delenv("OURDOMAIN_IMPERSONATES", raising=False)
    monkeypatch.delenv("OURDOMAIN_WAF_RETRIES", raising=False)
    monkeypatch.setattr(od, "_FINGERPRINT_STATE", {})
    monkeypatch.setattr(config, "get_impersonate", lambda: "chrome146")
    attempts = od._impersonate_attempts()
    assert attempts == list(od._DEFAULT_IMPERSONATES[:4])
    assert "chrome146" not in attempts


def test_lock_and_requirements_agree_on_the_minimum():
    """requirements.txt 的下限不能低于池里用到的指纹所需版本——本地照它装会装成旧版。"""
    req = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^curl_cffi>=(\S+)", req, re.M)
    assert m and m.group(1) == _PINNED
