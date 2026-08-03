"""RENTCafe 选房页解析测试。

fixture 里的按钮是 2026-08-03 从真实页面（Vaals / Katzensprung）抄下来的，
不是编的——这个解析器最容易因为上游改版悄悄失效，用真实形状测才有意义。

最关键的一条契约：**找不到目标单元时返回 None，绝不退而求其次选别的**。
用户是冲着某个具体房号来的，抢到别的等于替他做了个他没同意的决定。
"""
from __future__ import annotations

import pytest

from bookers.rentcafe_units import UnitOption, find_unit, parse_unit_options


# 实测抄录（缩短了尾部无关参数）
REAL_BUTTON = (
    '<button class="btn  UnitSelect btn btn-primary" name="1.S127" id="1.S127" '
    "onclick=\"ContinueClick('398336','1111515','185795','16-8-2026','',"
    "'oleapplication.aspx?myLeaseCafeType=2&amp;stepname=ApplicantInfo&amp;FromUnitSelection=1',"
    "'0','0','648','3281','1','16-8-2026','1-11-2026','','0','0','0','0','')\">"
    "Reserve this room</button>"
)


#: 服务端**实际发**的样子：onclick 里的单引号是 HTML 实体。
#: 上面那份是解码后的形状（当初是从浏览器 DevTools 里抄的，DevTools 显示的
#: 已经解码过了）。两份都要能解析。
REAL_BUTTON_ENTITY_ENCODED = REAL_BUTTON.replace("'", "&#39;")


def _button(unit_id: str, label: str) -> str:
    return REAL_BUTTON.replace("398336", unit_id).replace("1.S127", label)


class TestEntityEncodedOnclick:
    """服务端发的 onclick 里引号是 ``&#39;``，不是 ``'``。

    2026-08-03 踩过：正则按真引号写，真实页面上 20 个单元一个都没解析出来。
    表现极具误导性——``find_unit()`` 返回 None，流程报「该单元已被他人选走」，
    而单元其实好端端地就在页面上。这类「解析失败伪装成业务结果」的 bug 不会
    有任何报错，只能靠拿服务端原样的字节测。
    """

    def test_parses_entity_encoded_button(self):
        opts = parse_unit_options(REAL_BUTTON_ENTITY_ENCODED)
        assert len(opts) == 1, "实体编码的按钮必须能解析"
        assert opts[0].unit_id == "398336"
        assert opts[0].label == "1.S127"

    def test_both_encodings_give_the_same_result(self):
        assert parse_unit_options(REAL_BUTTON) == parse_unit_options(
            REAL_BUTTON_ENTITY_ENCODED
        )

    def test_find_unit_works_on_entity_encoded_page(self):
        assert find_unit(REAL_BUTTON_ENTITY_ENCODED, "xr_398336") is not None


class TestParse:
    def test_parses_the_real_button(self):
        opts = parse_unit_options(REAL_BUTTON)
        assert len(opts) == 1
        o = opts[0]
        assert o.unit_id == "398336"
        assert o.floor_plan_id == "1111515"
        assert o.property_id == "185795"
        assert o.available_date == "16-8-2026"
        assert o.label == "1.S127"
        assert "stepname=ApplicantInfo" in o.next_url

    def test_listing_id_bridges_to_scraper_side(self):
        """抓取侧存的是 xr_<unitId>——两侧标识符天然对齐，不需要映射表。"""
        assert parse_unit_options(REAL_BUTTON)[0].listing_id == "xr_398336"

    def test_parses_many_units(self):
        html = "".join(_button(str(400000 + i), f"1.S{i:03d}") for i in range(8))
        assert len(parse_unit_options(html)) == 8

    def test_ignores_non_unit_buttons(self):
        html = '<button class="btn btn-primary" onclick="doSomethingElse()">GO</button>' + REAL_BUTTON
        assert len(parse_unit_options(html)) == 1

    def test_button_without_continue_click_is_skipped(self):
        html = '<button class="UnitSelect" name="x" id="x">Reserve this room</button>'
        assert parse_unit_options(html) == []

    def test_too_few_arguments_is_skipped(self):
        html = ('<button class="UnitSelect" name="x" '
                "onclick=\"ContinueClick('1','2','3')\">x</button>")
        assert parse_unit_options(html) == []

    def test_blank_unit_id_is_skipped(self):
        html = ('<button class="UnitSelect" name="x" '
                "onclick=\"ContinueClick('','2','3','4','','u')\">x</button>")
        assert parse_unit_options(html) == []

    @pytest.mark.parametrize("html", ["", None, "<html>nothing here</html>"])
    def test_no_units_returns_empty_not_error(self, html):
        """上游改版时应表现为「没找到可订单元」，而不是让预订链路崩掉。"""
        assert parse_unit_options(html) == []


class TestFind:
    def test_finds_by_prefixed_id(self):
        html = _button("398336", "1.S127") + _button("398371", "1.S209")
        assert find_unit(html, "xr_398336").label == "1.S127"

    def test_finds_by_bare_id(self):
        html = _button("398336", "1.S127")
        assert find_unit(html, "398336").unit_id == "398336"

    def test_missing_unit_returns_none_not_a_substitute(self):
        """核心契约：不退而求其次。用户冲着某个具体房号来的。"""
        html = _button("398336", "1.S127") + _button("398371", "1.S209")
        assert find_unit(html, "xr_999999") is None

    @pytest.mark.parametrize("bad", ["", None, "xr_", "   "])
    def test_blank_target_returns_none(self, bad):
        assert find_unit(_button("398336", "1.S127"), bad) is None

    def test_picks_the_right_one_among_many(self):
        html = "".join(_button(str(400000 + i), f"1.S{i:03d}") for i in range(20))
        got = find_unit(html, "xr_400007")
        assert got is not None and got.label == "1.S007"
