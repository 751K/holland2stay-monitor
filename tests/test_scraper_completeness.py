"""scraper 完整扫描信号测试。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import scraper
from models import Listing
from scraper import ScrapeNetworkError, _scrape_city_pages
from scrapers import dispatch_scrape_tasks
from scrapers.base import ScrapeTask


def _h2s_tasks(*pairs: tuple[str, str], availability_ids=("179",)) -> list[ScrapeTask]:
    """构造 H2S ScrapeTask 列表（city_display, city_key）。"""
    return [
        ScrapeTask(
            source="holland2stay",
            city_key=city_id,
            city_display=city_name,
            extra={"availability_ids": list(availability_ids)},
        )
        for city_name, city_id in pairs
    ]


def _page(page: int, total_pages: int, items: list[dict] | None = None) -> dict:
    return {
        "data": {
            "products": {
                "items": items if items is not None else [{"id": f"{page}-1"}],
                "page_info": {"current_page": page, "total_pages": total_pages},
            }
        }
    }


def _listing(item: dict, city: str) -> Listing:
    listing_id = str(item.get("id", "x"))
    return Listing(
        id=listing_id,
        name=f"Listing {listing_id}",
        status="Available to book",
        price_raw="€1000",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{listing_id}",
        city=city,
    )


class TestScrapeCityCompleteness:
    def test_complete_true_when_all_pages_success_and_parse_rate_ok(self):
        with patch("scraper._post_gql", side_effect=[
            _page(1, 2, [{"id": "a"}]),
            _page(2, 2, [{"id": "b"}]),
        ]), patch("scraper._to_listing", side_effect=_listing):
            listings, complete = _scrape_city_pages(
                MagicMock(), "Eindhoven", ["29"], ["179"]
            )

        assert [l.id for l in listings] == ["a", "b"]
        assert complete is True

    def test_graphql_errors_return_incomplete_without_raise(self):
        with patch("scraper._post_gql", return_value={"errors": [{"message": "bad"}]}):
            listings, complete = _scrape_city_pages(
                MagicMock(), "Eindhoven", ["29"], ["179"]
            )

        assert listings == []
        assert complete is False

    def test_later_page_network_error_keeps_partial_results_but_incomplete(self):
        with patch("scraper._post_gql", side_effect=[
            _page(1, 2, [{"id": "a"}]),
            TimeoutError("timeout"),
        ]), patch("scraper._to_listing", side_effect=_listing):
            listings, complete = _scrape_city_pages(
                MagicMock(), "Eindhoven", ["29"], ["179"]
            )

        assert [l.id for l in listings] == ["a"]
        assert complete is False

    def test_max_pages_truncation_marks_incomplete(self, monkeypatch):
        monkeypatch.setattr(scraper, "_MAX_PAGES", 1)
        with patch("scraper._post_gql", return_value=_page(1, 2, [{"id": "a"}])), \
             patch("scraper._to_listing", side_effect=_listing):
            listings, complete = _scrape_city_pages(
                MagicMock(), "Eindhoven", ["29"], ["179"]
            )

        assert [l.id for l in listings] == ["a"]
        assert complete is False

    def test_parse_failure_rate_above_five_percent_marks_incomplete(self):
        items = [{"id": str(i)} for i in range(20)]

        def parse(item: dict, city: str):
            if item["id"] in {"0", "1"}:
                return None
            return _listing(item, city)

        with patch("scraper._post_gql", return_value=_page(1, 1, items)), \
             patch("scraper._to_listing", side_effect=parse):
            listings, complete = _scrape_city_pages(
                MagicMock(), "Eindhoven", ["29"], ["179"]
            )

        assert len(listings) == 18
        assert complete is False

    def test_parse_failure_rate_at_five_percent_stays_complete(self):
        items = [{"id": str(i)} for i in range(20)]

        def parse(item: dict, city: str):
            if item["id"] == "0":
                return None
            return _listing(item, city)

        with patch("scraper._post_gql", return_value=_page(1, 1, items)), \
             patch("scraper._to_listing", side_effect=parse):
            listings, complete = _scrape_city_pages(
                MagicMock(), "Eindhoven", ["29"], ["179"]
            )

        assert len(listings) == 19
        assert complete is True


class TestDispatchCompleteness:
    """多城市编排 + completeness 聚合 —— 现由 dispatch_scrape_tasks 负责
    （旧 scraper.scrape_all 已删除）。路径：
    dispatch → HollandStayScraper.scrape() → scraper._scrape_city_pages。

    patch _scrape_city_pages 注入各城市结果；patch _make_session 避免真建
    curl_cffi 会话（保持测试 hermetic + 快）。
    """

    def test_returns_city_completeness_map(self):
        with patch("scraper._scrape_city_pages", side_effect=[
            ([_listing({"id": "a"}, "Eindhoven")], True),
            ([], False),
        ]), patch("scrapers.holland2stay._make_session", return_value=MagicMock()):
            listings, completeness = dispatch_scrape_tasks(
                _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
            )

        assert [l.id for l in listings] == ["a"]
        assert completeness == {"Eindhoven": True, "Amsterdam": False}

    def test_city_with_first_page_network_failure_is_omitted_from_completeness(self):
        with patch("scraper._scrape_city_pages", side_effect=[
            ([], True),
            ScrapeNetworkError("page 1 failed"),
        ]), patch("scrapers.holland2stay._make_session", return_value=MagicMock()):
            listings, completeness = dispatch_scrape_tasks(
                _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
            )

        assert listings == []
        assert completeness == {"Eindhoven": True}

    def test_all_first_page_network_failures_still_raise(self):
        with patch(
            "scraper._scrape_city_pages",
            side_effect=ScrapeNetworkError("page 1 failed"),
        ), patch("scrapers.holland2stay._make_session", return_value=MagicMock()):
            with pytest.raises(ScrapeNetworkError):
                dispatch_scrape_tasks(
                    _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
                )
