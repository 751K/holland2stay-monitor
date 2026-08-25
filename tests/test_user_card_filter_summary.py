"""用户卡片上的过滤条件摘要：长列表只报个数，且不许溢出卡片。

2026-08-25 反馈：有用户勾了 28 个片区，模板写的是 ``allowed_neighborhoods|join('/')``
——连成一个**没有空格的长串**，浏览器只能在连字符处断行，于是文字直接从卡片右缘
溢出去（截图里 ``Schalkwijk/Sphin`` 断在卡片外面，隔壁卡片更是整行漂在外面）。

两处一起改，各管一件事：

1. 超过 4 项就只显示「28 个」，完整清单挂 ``title``。列全了会把卡片撑成一堵墙，
   而且没人会在列表页逐个读片区名——要看有编辑页。
   中间做过一版「前 3 项 +25」，被否了：那个写法要心算才知道总数，用户看到的
   第一反应是「+25 是啥意思」。
2. ``.filter-tags`` 加 ``overflow-wrap:anywhere`` 兜底。摘要之外还有别的长串
   （自动预订那行的邮箱），而且阈值将来若被调大也不该重新溢出。

阈值取 4 而不是 3：H2S 的户型常态就是「1 / 2 / Loft (open bedroom area) / Studio」
四项，再低一档就会把这个日常情况也折叠成一个数字。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from flask import Flask

from app.jinja_filters import register, summarize_list

_ROOT = Path(__file__).resolve().parent.parent
_CSS = _ROOT / "static" / "design.css"
_USERS = _ROOT / "templates" / "users.html"


@pytest.fixture
def zh():
    """summarize_list 折叠时要读当前语言，得在请求上下文里跑。"""
    app = Flask(__name__)
    with app.test_request_context("/?lang=zh"):
        yield


@pytest.fixture
def en():
    app = Flask(__name__)
    with app.test_request_context("/?lang=en"):
        yield


class TestSummarizeList:
    def test_short_list_is_kept_whole(self):
        assert summarize_list(["a", "b"]) == "a / b"

    def test_the_four_h2s_types_still_show_their_names(self):
        """阈值就是照着这个日常情况定的，别把它折叠掉。"""
        types = ["1", "2", "Loft (open bedroom area)", "Studio"]
        assert summarize_list(types) == "1 / 2 / Loft (open bedroom area) / Studio"

    def test_long_list_collapses_to_a_count(self, zh):
        assert summarize_list([str(i) for i in range(28)]) == "28 个"

    def test_long_list_in_english(self, en):
        assert summarize_list([str(i) for i in range(28)]) == "28 selected"

    def test_separator_has_spaces(self):
        """溢出的根源就是 "/".join 造出的无空格长串——分隔符必须留断行机会。"""
        assert " / " in summarize_list(["Binnenstad-Oost", "Woensel-Zuid"])

    @pytest.mark.parametrize("value", [None, [], ["", "  "]])
    def test_empty_input_gives_empty_string(self, value):
        assert summarize_list(value) == ""

    def test_blanks_are_dropped_not_counted(self, zh):
        """空项计进个数会报出一个用户在编辑页数不出来的数字。"""
        assert summarize_list(["a", " ", "b", "", "c", "d", "e"]) == "5 个"

    def test_non_strings_are_coerced(self):
        assert summarize_list([1, 2]) == "1 / 2"

    def test_zero_limit_means_no_collapsing(self):
        assert summarize_list(list("abcdef"), 0) == "a / b / c / d / e / f"

    def test_registered_as_a_template_filter(self):
        app = Flask(__name__)
        register(app)
        assert "summarize_list" in app.jinja_env.filters


class TestUsersTemplate:
    def _html(self) -> str:
        return _USERS.read_text(encoding="utf-8")

    @pytest.mark.parametrize("field", ["allowed_types", "allowed_neighborhoods"])
    def test_lists_go_through_the_summary_filter(self, field):
        html = self._html()
        m = re.search(re.escape("lf." + field) + r"\s*\|\s*(\w+)", html)
        assert m, f"users.html 里找不到 lf.{field} 的输出"
        assert "summarize_list" in html
        assert not re.search(
            re.escape("lf." + field) + r"\s*\|\s*join\('/'\)", html), (
            f"lf.{field} 又用 join('/') 直出了——无空格长串会溢出卡片")

    @pytest.mark.parametrize("field", ["allowed_types", "allowed_neighborhoods"])
    def test_full_list_stays_available_on_hover(self, field):
        """截断不能让信息消失，完整清单要挂在 title 上。"""
        html = self._html()
        assert re.search(
            r'title="\{\{\s*lf\.' + re.escape(field) + r"\s*\|\s*join\(' / '\)\s*\}\}\"",
            html), f"lf.{field} 的完整清单没有挂到 title"


class TestFilterTagsCannotOverflow:
    def test_overflow_wrap_is_set(self):
        css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
        m = re.search(r"\.filter-tags\s*\{(.*?)\}", css, re.S)
        assert m, "design.css 里找不到 .filter-tags"
        assert re.search(r"(?<![\w-])overflow-wrap\s*:\s*(anywhere|break-word)", m.group(1)), (
            ".filter-tags 没有 overflow-wrap——没有空格的长串会直接溢出卡片")
