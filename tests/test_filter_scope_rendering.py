"""平台适用范围的提示要真的渲染到页面上。

``dim_scope_note`` / ``dim_scope_badge`` 本身有单测，但它们得先被注入模板、模板
里得先调用，用户才看得到。函数写对了而模板没接，测试全绿、界面照旧什么都没有。

同时守住密度：整句说明只出现一次。逐字段各写一遍的话，用户表单一页里同一句话
要出现八遍，其中六遍一字不差。
"""
from __future__ import annotations

import re

import pytest


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _account_html(client, lang_header: str) -> str:
    """注册一个普通用户，取他的账号页（房源条件分组）。"""
    from app.auth import _REGISTER_RECORDS
    from users import load_users
    _REGISTER_RECORDS.clear()
    r = client.post("/register", data={
        "csrf_token": "test_csrf", "register_username": "ScopeUser",
        "register_password": "pw1234", "terms_accepted": "1",
    })
    assert r.status_code in (302, 303)
    uid = next(u.id for u in load_users() if u.name == "ScopeUser")
    client.set_cookie("h2s-lang", lang_header[:2])
    return client.get(f"/users/{uid}?section=filters").get_data(as_text=True)


class TestAccountFilters:
    #: 账号页房源条件里带范围徽标的维度，与 _settings_macros.filter_fields 一一对应。
    PAGE_DIMS = ("type", "occupancy", "contract", "tenant", "offer", "finishing", "energy", "neighborhood")

    @pytest.mark.parametrize("lang,header", [("zh", "zh-CN"), ("en", "en-US")])
    def test_badges_match_the_capability_table(self, client, lang, header):
        """页面上的徽标必须与 _SOURCE_FILTER_DIMS 逐字一致，多一个少一个都算错。

        语言两边都钉死：页面语言和 dim_scope_badge 的语言参数各有默认值，碰巧撞上
        时测试会假绿（2026-08-27 回落改成 en 之后就因此变红过一次）。
        """
        from collections import Counter

        from app.filter_scope import dim_scope_badge

        html = _account_html(client, header)
        assert f'<html lang="{lang}"' in html
        expected = Counter(b for d in self.PAGE_DIMS if (b := dim_scope_badge(d, lang)))
        assert expected, "没有任何维度需要徽标，这条测试已经失去意义"
        for badge, n in expected.items():
            # 数 `>徽标</small>`：英文 tooltip 里也含「Holland2Stay only」，裸数会多算
            anchor = f">{badge}</small>"
            assert html.count(anchor) == n, f"徽标 {badge!r} 应出现 {n} 次，实际 {html.count(anchor)} 次"

    def test_badge_carries_the_full_note_as_tooltip(self, client):
        html = _account_html(client, "zh-CN")
        assert "其余平台不提供该属性" in html, "徽标没有带完整说明"


class TestUserForm:

    def test_fields_carry_badges(self, admin_client):
        html = admin_client.get(
            "/users/new", headers={"Accept-Language": "zh-CN"}).get_data(as_text=True)
        assert html.count("仅 Holland2Stay") >= 4
        # 徽标要点名平台，不能是「仅 3 个平台」那种只报数的写法
        assert "Xior 除外" in html
        assert "个平台" not in html

    def test_no_per_field_sentences(self, admin_client):
        """逐字段整句提示已经收掉——同一句话一页出现八遍太吵。"""
        html = admin_client.get("/users/new").get_data(as_text=True)
        # 徽标的 tooltip 里仍有完整句子（title 属性），但正文里不该再有 <div> 版
        assert '<div class="text-xs text-secondary mt-1"><i class="bi bi-info-circle mr-1"></i>仅对' not in html


class TestEnglishUi:
    def test_english_badge_and_note(self, client):
        html = _account_html(client, "en-US")
        assert "Holland2Stay only" in html
        assert "unaffected" in html
        assert "仅 Holland2Stay" not in html
