import sys
from types import ModuleType

import pytest

from mcore.sources import KNOWN_SOURCES, SOURCE_DISPLAY_NAMES, SPECS, SourceSpec
from scrapers import SCRAPER_REGISTRY, _load_scrapers
from scrapers.base import AbstractScraper


def test_source_registries_cover_the_same_platforms():
    assert set(SPECS) == set(KNOWN_SOURCES) == set(SOURCE_DISPLAY_NAMES) == set(SCRAPER_REGISTRY)
    assert all(name == spec.name for name, spec in SPECS.items())


def test_source_order_is_stable():
    assert KNOWN_SOURCES == (
        "holland2stay", "ourdomain", "ourcampus", "xior", "magis",
        "studentexperience", "plaza",
    )


def test_new_registration_loads_its_scraper(monkeypatch):
    class ExampleScraper(AbstractScraper):
        source = "example"

        def scrape(self, task):
            raise NotImplementedError

    module = ModuleType("test_example_scraper")
    module.ExampleScraper = ExampleScraper
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec = SourceSpec("example", "Example", "EX", "test_example_scraper:ExampleScraper")
    assert _load_scrapers({"example": spec}) == {"example": ExampleScraper}
    with pytest.raises(ValueError, match="Invalid scraper registration"):
        _load_scrapers({"wrong": spec})


def test_frontend_receives_the_registered_names(test_app):
    from flask import render_template

    with test_app.test_request_context("/"):
        html = render_template("base.html")
    for source, label in SOURCE_DISPLAY_NAMES.items():
        assert f'"{source}": "{label}"' in html
