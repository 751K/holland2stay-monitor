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


@pytest.fixture
def seeded(admin_client):
    """筛选下拉的选项来自库里已有的取值——空库渲染不出任何 checkbox。"""
    from app.db import storage
    from models import Listing

    st = storage()
    st.diff([
        Listing(id=f"S{i}", name=f"S{i}", status="Available to book",
                price_raw="1000", available_from="", url="",
                city="Eindhoven", source="holland2stay",
                features=[f"Finishing: {v}", "Occupancy: One", "Type: Studio"])
        for i, v in enumerate(("Furnished", "Semi furnished", "Fully furnished"))
    ])
    st.close()
    return admin_client


class TestListingsPage:
    #: /listings 筛选栏渲染徽标的维度，与 templates/listings.html 一一对应。
    PAGE_DIMS = ("contract", "type", "occupancy", "tenant", "energy", "finishing")

    def test_badges_match_the_capability_table(self, admin_client):
        """页面上的徽标必须与 _SOURCE_FILTER_DIMS 逐字一致。

        原先这里断言的是「至少 3 个『仅 Holland2Stay』」。写死数字的问题在
        v1.16.2 暴露了：租客资格扩到四个平台后徽标本该消失，而 `>= 3` 既可能
        因此变红（实际如此），也可能在别处多出一个徽标时继续变绿——两种都不是
        在测「说明是对的」。改成从能力表推导，多一个少一个都会失败。
        """
        from collections import Counter

        from config import dim_scope_badge

        html = admin_client.get("/listings").get_data(as_text=True)
        expected = Counter(
            b for d in self.PAGE_DIMS if (b := dim_scope_badge(d))
        )
        assert expected, "没有任何维度需要徽标，这条测试已经失去意义"
        for badge, n in expected.items():
            assert html.count(badge) == n, (
                f"徽标 {badge!r} 应出现 {n} 次，实际 {html.count(badge)} 次"
            )

    @pytest.fixture
    def tenant_seeded(self, admin_client):
        """租客筛选的选项来自库里已有取值，空库连 checkbox 都渲染不出来。"""
        from app.db import storage
        from models import Listing

        st = storage()
        st.diff([
            Listing(id="T1", name="T1", status="Available to book",
                    price_raw="1000", available_from="", url="",
                    city="Eindhoven", source="holland2stay",
                    features=["Tenant: student only"]),
            Listing(id="T2", name="T2", status="Available to book",
                    price_raw="1000", available_from="", url="",
                    city="Amsterdam", source="ourdomain",
                    features=["Tenant: employed only"]),
        ])
        st.close()
        return admin_client

    def test_tenant_badge_reflects_the_capability_table(self, tenant_seeded):
        """徽标要如实反映该维度覆盖哪几个平台。

        覆盖范围变过三次：v1.16.2 从「仅 H2S」扩到四家，2026-08-18 因白名单缩回
        三家，2026-08-19 靠详情补齐恢复四家。徽标错了比没有徽标更糟——用户会据此
        误判哪些平台的房源在按资格过滤。
        """
        admin_client = tenant_seeded
        from config import dim_scope_badge, dim_scope_note

        html = admin_client.get("/listings").get_data(as_text=True)
        # 覆盖范围变过三次：v1.16.2 四家 → 2026-08-18 因白名单缩回三家 →
        # 2026-08-19 靠 GetProductDetail 详情补齐恢复四家。断言的是「徽标与能力表
        # 一致」，不是某个具体字样——写死字样会在下次覆盖变化时误报。
        badge = dim_scope_badge("tenant")
        assert badge == "", "四家全覆盖时不该有徽标"
        assert 'name="tenant"' in html, "租客筛选没渲染出来，这条测试测了个空"
        # 通用维度那一行的 label 里不该出现任何「仅 …」范围提示
        head = html[: html.index('name="tenant"')]
        labels = re.findall(
            r'<label class="form-label">(.*?)</label>', head, re.S,
        )
        assert labels, "找不到租客筛选的 label"
        assert "仅 " not in labels[-1], (
            f"租客已是通用维度，那一行不该再有范围徽标: {labels[-1]}"
        )

    def test_badge_carries_the_full_note_as_tooltip(self, admin_client):
        html = admin_client.get("/listings").get_data(as_text=True)
        assert "其余平台不提供该属性" in html, "徽标没有带完整说明"

    def test_universal_dimensions_have_no_badge(self, admin_client):
        """全平台生效的维度不该加徽标，否则满屏都是废话。"""
        html = admin_client.get("/listings").get_data(as_text=True)
        # 城市/平台/租金/面积四个通用维度所在的 label 里不应出现徽标
        for chunk in re.findall(r'<label class="form-label">(.*?)</label>', html):
            if "filter_city" in chunk or "filter_source" in chunk:
                assert "仅 " not in chunk

    def test_finishing_is_a_multi_select(self, seeded):
        """四档互斥，单选就只能一次看一档。"""
        html = seeded.get("/listings").get_data(as_text=True)
        assert 'name="finishing"' in html
        assert 'type="checkbox" name="finishing"' in html, "装修还是单选"
        assert '<select name="finishing"' not in html

    def test_route_reads_every_finishing_value(self, seeded):
        """路由必须用 getlist。

        只读单值的话，多选在界面上勾得进去、URL 里也带着，后端却只认第一个——
        用户看到的结果比他勾的少，而且看不出哪里错了。
        """
        one = seeded.get("/listings?finishing=Furnished").get_data(as_text=True)
        two = seeded.get(
            "/listings?finishing=Furnished&finishing=Fully+furnished"
        ).get_data(as_text=True)
        assert "S0" in one and "S2" not in one
        assert "S0" in two and "S2" in two, "第二个 finishing 值被丢掉了"
        assert "S1" not in two, "Semi furnished 不该被带进来"


class TestUserForm:
    def test_scope_rule_is_stated_once(self, admin_client):
        html = admin_client.get("/users/new").get_data(as_text=True)
        text = _text(html)
        assert "带「仅 …」标记的条件只对部分平台生效" in text
        # 整句只出现一次；各字段靠徽标
        assert text.count("其余平台不提供该属性，它们的房源不受该条件影响") == 1

    def test_fields_carry_badges(self, admin_client):
        html = admin_client.get("/users/new").get_data(as_text=True)
        assert html.count("仅 Holland2Stay") >= 4
        assert "仅 3 个平台" in html

    def test_no_per_field_sentences(self, admin_client):
        """逐字段整句提示已经收掉——同一句话一页出现八遍太吵。"""
        html = admin_client.get("/users/new").get_data(as_text=True)
        # 徽标的 tooltip 里仍有完整句子（title 属性），但正文里不该再有 <div> 版
        assert '<div class="text-xs text-secondary mt-1"><i class="bi bi-info-circle mr-1"></i>仅对' not in html


class TestEnglishUi:
    def test_english_badge_and_note(self, admin_client):
        admin_client.get("/set-lang?lang=en&next=/listings")
        html = admin_client.get("/listings").get_data(as_text=True)
        assert "Holland2Stay only" in html
        assert "unaffected" in html
        assert "仅 Holland2Stay" not in html
