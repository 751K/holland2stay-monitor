"""判据是「有没有可下单的按钮」，不是状态格的样式类。

起因（2026-08-27 生产）
----------------------
OurCampus Diemen 当天 11:22 挂出三套单元，我们全部报成 Occupied，一条通知都没
发；12:19 之后它们从 feed 里消失——多半是被别人订走了。

原实现只看状态格里第一个 span 的 class：``success`` → 可订，``warning`` → 抽签，
**其余一律 Occupied**。而当天的行长这样::

    <span class='muted'>31-8-2026</span>
    <input class='UnitSelect btn btn-primary' value='Book Now'
           onclick='ApplyNowClick("457252",…,"10-9-2026",…)'>

``muted`` 不在判据里，掉进兜底判了 Occupied——而那个按钮和当天判为 Available
的单元**一模一样**。

快照实测（24 天累计）：34 个 UnitSelect 按钮形态完全一致、**0 个 disabled**，
状态格 21 个 ``text-success`` + 13 个 ``muted``，正好一一对应。也就是说
``muted`` 的含义是「**某日起**可订」，不是「已出租」。

这和 2026-08-25 Xior 那次是同一个错误的两个方向：那次「类名说能订、文字写着
Rented Out」判宽了，这次「按钮明明能订、类名不认识」判严了。共同点是**判据看
的是外观标记，而不是那个决定「能不能订」的东西**。
"""
from __future__ import annotations

import pytest

from scrapers.ourdomain import _extract_status, _has_bookable_control

#: 生产快照里逐字抄来的按钮（id / aria 号做了脱敏）。
REAL_BUTTON = (
    "<input type='button' data-selenium-id='btnUnitSelect1' "
    "class='UnitSelect btn btn-primary' id='3249' value ='Book Now' "
    "aria-describedby='457252' "
    "onclick='return ApplyNowClick(\"457252\",\"1113259\",\"186609\",\"10-9-2026\")'>"
)
MUTED_CELL = "<span class='muted'>31-8-2026</span>"
SUCCESS_CELL = "<span class='text-success'>Available</span>"


class TestTheBugItself:
    def test_muted_with_a_book_button_is_bookable(self):
        """这就是 08-27 丢掉的那三套。"""
        assert _extract_status(MUTED_CELL, MUTED_CELL + REAL_BUTTON) == "Available to book"

    def test_old_criterion_would_have_said_occupied(self):
        """没有整行可看时仍是旧判据——这条钉住「差别确实来自按钮」。"""
        assert _extract_status(MUTED_CELL) == "Occupied"

    def test_success_still_bookable(self):
        assert _extract_status(SUCCESS_CELL, SUCCESS_CELL + REAL_BUTTON) == "Available to book"


class TestBookableControl:
    @pytest.mark.parametrize("html", [
        REAL_BUTTON,
        "<input class='UnitSelect'>",
        "<a onclick='ApplyNowClick(1)'>x</a>",
        "<input value='Book Now'>",
        "<input value = ' Book Now '>",
    ])
    def test_recognised(self, html):
        assert _has_bookable_control(html)

    @pytest.mark.parametrize("html", [
        "", "<td>#3249</td>", "<input value='Join Waitlist'>",
        "<span class='muted'>31-8-2026</span>",
    ])
    def test_not_recognised(self, html):
        assert not _has_bookable_control(html)

    def test_disabled_button_does_not_count(self):
        """实测 0 个 disabled，但不能指望它永远不出现。"""
        disabled = REAL_BUTTON.replace("<input ", "<input disabled ")
        assert not _has_bookable_control(disabled)
        assert _extract_status(MUTED_CELL, MUTED_CELL + disabled) == "Occupied"


class TestTwoIndependentPieces:
    """明确标记和按钮**任取其一**即可，不能要求同时成立。

    要求同时成立会把只有 ``<span class='success'>Available</span>`` 而不带按钮的
    老主题整体判成不可订——合成 fixture
    ``test_southeast_style_row_extracts_all_fields`` 当场抓到过这个回归。
    """

    def test_explicit_marker_without_a_button(self):
        assert _extract_status(SUCCESS_CELL, "<tr>" + SUCCESS_CELL + "</tr>") == "Available to book"

    def test_button_without_a_known_marker(self):
        assert _extract_status(MUTED_CELL, MUTED_CELL + REAL_BUTTON) == "Available to book"

    def test_neither_is_occupied(self):
        assert _extract_status("<span class='muted'>x</span>", "<tr><td>#1</td></tr>") == "Occupied"


class TestLotteryStaysSeparate:
    """抽签也有按钮，但点下去是进池子，不是直接订到房——不能被按钮判据吞掉。"""

    @pytest.mark.parametrize("cell", [
        "<span class='text-warning'>Waitlist</span>",
        "<span class='muted'>please wait</span>",
    ])
    def test_lottery_wins_over_the_button(self, cell):
        assert _extract_status(cell, cell + REAL_BUTTON) == "Available in lottery"


class TestUnknownMarkerWarns:
    def test_unseen_class_with_a_button_is_allowed_and_logged(self, caplog):
        """放行并告警，与 xior 对未知按钮文字的处理一致。

        默默归类正是这个 bug 的形态——「没见过」变成 Occupied，谁也不知道。
        """
        import logging

        cell = "<span class='brand-new-thing'>???</span>"
        with caplog.at_level(logging.WARNING, logger="scrapers.ourdomain"):
            got = _extract_status(cell, cell + REAL_BUTTON)
        assert got == "Available to book"
        assert any("没见过" in r.getMessage() for r in caplog.records), (
            f"未知样式没告警: {[r.getMessage() for r in caplog.records]}")

    def test_known_classes_do_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="scrapers.ourdomain"):
            _extract_status(MUTED_CELL, MUTED_CELL + REAL_BUTTON)
        assert not [r for r in caplog.records if "没见过" in r.getMessage()], (
            "muted 已经查清楚含义了，不该每轮刷告警")


class TestWiredIntoExtraction:
    def test_extract_unit_passes_the_whole_row(self):
        """接线断了上面全绿——_extract_status 拿不到整行就永远走旧判据。"""
        import inspect
        from scrapers import ourdomain
        src = inspect.getsource(ourdomain._extract_unit)
        assert "_extract_status(avail_html, row_html)" in src, (
            "没把整行传进去，按钮判据形同虚设")
