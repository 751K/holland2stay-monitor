from __future__ import annotations

import json
from types import MethodType

import pytest

import browser_fetcher
from browser_fetcher import BrowserFetcher
from scrapers.base import UpstreamMaintenanceError


class _FakePage:
    def __init__(self, *responses: dict, title: str = "", content: str = ""):
        self.responses = list(responses)
        self.scripts: list[str] = []
        self._title = title
        self._content = content

    def evaluate(self, script: str):
        self.scripts.append(script)
        if self.responses:
            return self.responses.pop(0)
        return {
            "status": 200,
            "ok": True,
            "text": json.dumps({"data": {"ok": True}}),
            "headers": {},
        }

    def title(self):
        return self._title

    def content(self):
        return self._content


def _make_fetcher(page: _FakePage) -> BrowserFetcher:
    fetcher = BrowserFetcher()
    fetcher._initialized = True
    fetcher._page = page

    def ensure_initialized(self):
        self._initialized = True

    fetcher.ensure_initialized = MethodType(ensure_initialized, fetcher)
    return fetcher


def test_fetch_gql_uses_browser_like_same_origin_request():
    page = _FakePage({
        "status": 200,
        "ok": True,
        "text": json.dumps({"data": {"products": {"items": []}}}),
        "headers": {"content-type": "application/json"},
    })
    fetcher = _make_fetcher(page)

    data = fetcher.fetch_gql("query Test { products { items { sku } } }")

    assert data["data"]["products"]["items"] == []
    script = page.scripts[0]
    assert "credentials: 'include'" in script
    assert "mode: 'same-origin'" in script
    assert "referrer: window.location.href" in script
    assert '"Store": "default"' in script
    assert '"Content-Currency": "EUR"' in script


def test_fetch_gql_refreshes_status_after_403_retry_success():
    page = _FakePage(
        {
            "status": 403,
            "ok": False,
            "text": "Forbidden",
            "headers": {"cf-ray": "test-ray"},
        },
        {
            "status": 200,
            "ok": True,
            "text": json.dumps({"data": {"recovered": True}}),
            "headers": {"content-type": "application/json"},
        },
    )
    fetcher = _make_fetcher(page)

    data = fetcher.fetch_gql("query Test { ok }")

    assert data == {"data": {"recovered": True}}
    assert len(page.scripts) == 2


def test_maintenance_title_raises_upstream_maintenance():
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage(title="H2S-Maintenance")

    with pytest.raises(UpstreamMaintenanceError, match="维护"):
        fetcher._raise_if_maintenance_page()


def test_macos_headless_launch_uses_headed_directly(monkeypatch):
    calls: list[dict] = []

    class _FakeBrowser:
        def new_page(self):
            return _FakePage()

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return _FakeBrowser()

    import cloakbrowser

    monkeypatch.setattr(browser_fetcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cloakbrowser, "launch", fake_launch)

    fetcher = BrowserFetcher(headless=True)
    fetcher.__enter__()

    assert len(calls) == 1
    assert calls[0]["headless"] is False
    assert calls[0]["args"] == []
