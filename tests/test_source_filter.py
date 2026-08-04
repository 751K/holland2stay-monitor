"""按平台过滤：通知和自动预订都要能只盯某几个平台。

2026-08-04 发现的问题不是「没做」，是**做了一半**：``ListingFilter.allowed_sources``
一直存在，``passes()`` 一直在判它，手机 API 也一直暴露它——唯独面板表单没有
解析它。而表单每次都是构造一个**全新的** ListingFilter，所以从面板保存一次，
用户在手机上设过的平台过滤就被清空了。静默丢数据比没这功能更糟。

同一天还发现 ourcampus 被漏了三次（前端 sourceLabel 的三份实现、monitoring
页、全局设置的平台白名单），原因都一样：每处各自维护一份平台清单。所以加了
``config.KNOWN_SOURCES`` 作为唯一出处，这里也钉住它。
"""
from __future__ import annotations

import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.forms.user_form import build_user_from_form
from config import KNOWN_SOURCES, ListingFilter, source_display_name
from models import Listing


def _listing(source: str) -> Listing:
    return Listing(
        id="x1", name="n", status="Available to book", price_raw="€1",
        available_from="2026-09-01", features=[], url="u", city="Eindhoven",
        source=source,
    )


class TestMatching:
    def test_empty_allows_every_platform(self):
        f = ListingFilter()
        assert all(f.passes(_listing(s)) for s in KNOWN_SOURCES)

    def test_only_listed_platforms_pass(self):
        f = ListingFilter(allowed_sources=["xior"])
        assert f.passes(_listing("xior")) is True
        assert f.passes(_listing("holland2stay")) is False

    def test_match_is_case_insensitive(self):
        assert ListingFilter(allowed_sources=["XIOR"]).passes(_listing("xior"))


class TestFormRoundTrip:
    """表单必须解析 allowed_sources——否则保存一次就把它清空。"""

    def _form(self, **kw):
        items = [("name", "u")]
        for k, vals in kw.items():
            for v in vals:
                items.append((k, v))
        return ImmutableMultiDict(items)

    def test_notification_filter_keeps_platforms(self):
        u = build_user_from_form(self._form(ALLOWED_SOURCES=["xior", "ourcampus"]))
        assert u.listing_filter.allowed_sources == ["xior", "ourcampus"]

    def test_auto_book_filter_keeps_platforms(self):
        u = build_user_from_form(self._form(AUTO_BOOK_ALLOWED_SOURCES=["holland2stay"]))
        assert u.auto_book.listing_filter.allowed_sources == ["holland2stay"]

    def test_the_two_filters_are_independent(self):
        """只想被通知全部平台、但只自动预订 H2S，是完全合理的组合。"""
        u = build_user_from_form(self._form(
            ALLOWED_SOURCES=["xior", "holland2stay"],
            AUTO_BOOK_ALLOWED_SOURCES=["holland2stay"],
        ))
        assert u.listing_filter.allowed_sources == ["xior", "holland2stay"]
        assert u.auto_book.listing_filter.allowed_sources == ["holland2stay"]

    def test_not_submitting_it_means_no_restriction(self):
        u = build_user_from_form(self._form())
        assert u.listing_filter.allowed_sources == []


class TestKnownSourcesIsTheSingleSource:
    def test_ourcampus_is_in_the_list(self):
        """它被漏过三次，各处都是自己维护一份清单。"""
        assert "ourcampus" in KNOWN_SOURCES

    def test_every_source_has_a_display_name(self):
        for s in KNOWN_SOURCES:
            assert source_display_name(s) != s, f"{s} 缺显示名"

    def test_unknown_source_is_not_mislabelled_as_a_known_one(self):
        """把未知 source 显示成某个已知平台，会让人以为数据是那边来的。"""
        got = source_display_name("brandnew")
        assert got == "Brandnew"
        assert got not in ("Holland2Stay", "OurDomain", "OurCampus", "Xior")

    def test_settings_accepts_every_known_source(self):
        """全局设置的白名单曾写死三个，勾了 OurCampus 会被静默丢弃。"""
        import inspect

        from app.routes import settings as settings_mod

        src = inspect.getsource(settings_mod)
        assert "KNOWN_SOURCES" in src
        assert '{"holland2stay", "ourdomain", "xior"}' not in src
