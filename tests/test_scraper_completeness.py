"""scraper 完整扫描信号测试（适配新 CloakBrowser 路径）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import scrapers.holland2stay as h2s
from models import Listing
from scrapers.base import ScrapeNetworkError, ScrapeTask
from scrapers.holland2stay import _scrape_city_pages
from scrapers import dispatch_scrape_tasks


_EMPTY_LABELS: dict[str, dict[str, str]] = {}


def _h2s_tasks(*pairs: tuple[str, str], availability_ids=("179",)) -> list[ScrapeTask]:
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


def _listing(item: dict, city: str, labels: dict = None) -> Listing:
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


_EMPTY_LABELS: dict[str, dict[str, str]] = {}


def _make_fetcher(*responses):
    """构造带 fetch_gql 响应的 mock fetcher。"""
    fetcher = MagicMock()
    fetcher.fetch_gql.side_effect = list(responses) if len(responses) > 1 else responses
    return fetcher


class TestScrapeCityCompleteness:
    def test_complete_true_when_all_pages_success_and_parse_rate_ok(self):
        with patch("scrapers.holland2stay._to_listing", side_effect=_listing):
            fetcher = _make_fetcher(
                _page(1, 2, [{"id": "a"}]),
                _page(2, 2, [{"id": "b"}]),
            )
            listings, complete = _scrape_city_pages(
                fetcher, "Eindhoven", ["29"], ["179"], _EMPTY_LABELS,
            )

        assert [l.id for l in listings] == ["a", "b"]
        assert complete is True

    def test_graphql_errors_return_incomplete_without_raise(self):
        fetcher = _make_fetcher({"errors": [{"message": "bad"}]})
        listings, complete = _scrape_city_pages(
            fetcher, "Eindhoven", ["29"], ["179"], _EMPTY_LABELS,
        )

        assert listings == []
        assert complete is False

    def test_later_page_network_error_keeps_partial_results_but_incomplete(self):
        with patch("scrapers.holland2stay._to_listing", side_effect=_listing):
            fetcher = _make_fetcher(
                _page(1, 2, [{"id": "a"}]),
                TimeoutError("timeout"),
            )
            listings, complete = _scrape_city_pages(
                fetcher, "Eindhoven", ["29"], ["179"], _EMPTY_LABELS,
            )

        assert [l.id for l in listings] == ["a"]
        assert complete is False

    def test_max_pages_truncation_marks_incomplete(self, monkeypatch):
        monkeypatch.setattr(h2s, "_MAX_PAGES", 1)
        with patch("scrapers.holland2stay._to_listing", side_effect=_listing):
            fetcher = _make_fetcher(_page(1, 2, [{"id": "a"}]))
            listings, complete = _scrape_city_pages(
                fetcher, "Eindhoven", ["29"], ["179"], _EMPTY_LABELS,
            )

        assert [l.id for l in listings] == ["a"]
        assert complete is False

    def test_parse_failure_rate_above_five_percent_marks_incomplete(self):
        items = [{"id": str(i)} for i in range(20)]

        def parse(item: dict, city: str, labels: dict):
            if item["id"] in {"0", "1"}:
                return None
            return _listing(item, city)

        with patch("scrapers.holland2stay._to_listing", side_effect=parse):
            fetcher = _make_fetcher(_page(1, 1, items))
            listings, complete = _scrape_city_pages(
                fetcher, "Eindhoven", ["29"], ["179"], _EMPTY_LABELS,
            )

        assert len(listings) == 18
        assert complete is False

    def test_parse_failure_rate_at_five_percent_stays_complete(self):
        items = [{"id": str(i)} for i in range(20)]

        def parse(item: dict, city: str, labels: dict):
            if item["id"] == "0":
                return None
            return _listing(item, city)

        with patch("scrapers.holland2stay._to_listing", side_effect=parse):
            fetcher = _make_fetcher(_page(1, 1, items))
            listings, complete = _scrape_city_pages(
                fetcher, "Eindhoven", ["29"], ["179"], _EMPTY_LABELS,
            )

        assert len(listings) == 19
        assert complete is True


class TestDispatchCompleteness:
    """多城市编排 + completeness 聚合。

    patch _scrape_city_pages 注入各城市结果；patch BrowserFetcher
    避免真启动 CloakBrowser（保持测试 hermetic）。
    """

    _PATCH_SCRAPE = "scrapers.holland2stay._scrape_city_pages"

    def test_returns_city_completeness_map(self):
        with patch(self._PATCH_SCRAPE, side_effect=[
            ([_listing({"id": "a"}, "Eindhoven")], True),
            ([], False),
        ]), patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            listings, completeness = dispatch_scrape_tasks(
                _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
            )

        assert [l.id for l in listings] == ["a"]
        assert completeness == {"Eindhoven": True, "Amsterdam": False}

    def test_city_with_first_page_network_failure_is_omitted_from_completeness(self):
        with patch(self._PATCH_SCRAPE, side_effect=[
            ([], True),
            ScrapeNetworkError("page 1 failed"),
        ]), patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            listings, completeness = dispatch_scrape_tasks(
                _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
            )

        assert listings == []
        assert completeness == {"Eindhoven": True}

    def test_all_first_page_network_failures_still_raise(self):
        with patch(
            self._PATCH_SCRAPE,
            side_effect=ScrapeNetworkError("page 1 failed"),
        ), patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            with pytest.raises(ScrapeNetworkError):
                dispatch_scrape_tasks(
                    _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
                )
