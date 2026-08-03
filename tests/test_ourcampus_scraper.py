"""OurCampus scraper 测试。

OurCampus 与 OurDomain 同属 Greystar、同一套 RentCafe/SecureRC 后端，所以
``OurCampusScraper`` 继承 ``OurDomainScraper``，只覆盖取单元表的请求形状。
本文件锁住的就是「继承了什么」和「改了什么」两件事。

fixtures 是 2026-08-03 从生产站点真实抓的：
- ``ourcampus_floorplans.html``            floorplans.aspx 里含 FP 锚点的片段
- ``ourcampus_availableunits_empty.html``  零可订时 availableunits 的真实响应

**没有「有房」的样本**——接入时该楼零可订。所以单元行解析走的是 OurDomain 的
实现，赌两边同模板；依据是两边空响应的结构指纹一致。真实有房时需人工核对一次。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scrapers
from scrapers.ourcampus import OurCampusScraper
from scrapers.ourdomain import (
    OurDomainScraper,
    _extract_floorplan_ids,
    _extract_floorplan_names,
    _extract_units,
    _looks_like_availability_panel,
)
from scrapers.base import ScrapeTask


_FIX = Path(__file__).parent / "fixtures"
_FP_HTML = (_FIX / "ourcampus_floorplans.html").read_text(encoding="utf-8")
_EMPTY_UNITS = (_FIX / "ourcampus_availableunits_empty.html").read_text(encoding="utf-8")


# ── 注册与配置 ──────────────────────────────────────────────────────

class TestRegistration:
    def test_registered_under_own_source(self):
        assert scrapers.SCRAPER_REGISTRY["ourcampus"] is OurCampusScraper
        assert OurCampusScraper.source == "ourcampus"

    def test_inherits_ourdomain(self):
        """继承是刻意的——指纹池/冷却/403 重试/单元解析全部复用。"""
        assert issubclass(OurCampusScraper, OurDomainScraper)

    def test_config_expands_task(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "ourcampus")
        monkeypatch.setenv("OURCAMPUS_CITIES", "OurCampus Amsterdam Diemen,diemen")
        import config
        tasks = config.load_config().scrape_tasks_v2()
        assert [(t.source, t.city_key) for t in tasks] == [("ourcampus", "diemen")]

    def test_id_prefix_differs_from_ourdomain(self):
        """不同 RentCafe property 的 unit id 是否全局唯一没有保证；
        撞车会让两条房源在 storage.diff() 里合并成同一行、互相覆盖。"""
        assert OurCampusScraper.ID_PREFIX == "oc_"
        assert OurDomainScraper.ID_PREFIX == "od_"
        assert OurCampusScraper.ID_PREFIX != OurDomainScraper.ID_PREFIX

    def test_building_registry(self):
        b = OurCampusScraper.BUILDINGS["diemen"]
        assert b["property_id"] == "186609"
        assert b["slug"] == "new-ourcampus-amsterdam-diemen"
        # 街道地址供 geocode 用；unit 名不可 geocode
        assert "Dalsteindreef" in b["street_address"]

    def test_unknown_key_names_ourcampus_not_ourdomain(self):
        """报错要指向本平台，否则配置写错时会把人引到另一个平台的文档。"""
        s = OurCampusScraper()
        with pytest.raises(ValueError, match="OurCampus"):
            s._building_for_task(ScrapeTask("ourcampus", "nope", "Nope"))


# ── 请求形状：POST + floorPlans[] ───────────────────────────────────

class TestRequestShape:
    def _capture(self, scraper):
        session = MagicMock()
        resp = MagicMock(status_code=200, ok=True, text=_EMPTY_UNITS)
        resp.raise_for_status = lambda: None
        session.get.return_value = resp
        session.post.return_value = resp
        scraper._fetch_units_html(
            session, base="https://x.test/onlineleasing", fp_id="1112904",
            property_id="186609", move_in_date="2026-09-01",
            floorplans_url="https://x.test/onlineleasing/slug/floorplans.aspx",
        )
        return session

    def test_ourcampus_posts_floorplans_array(self):
        """照抄它自己前端：jQuery .load(url, {floorPlans: names}) 走 POST。"""
        session = self._capture(OurCampusScraper())
        assert session.get.call_count == 0
        assert session.post.call_count == 1
        _, kwargs = session.post.call_args
        assert kwargs["data"] == [("floorPlans[]", "1112904")]

    def test_ourcampus_url_has_no_moveindate_or_propertyid(self):
        """它自己的调用不带这两个参数，由会话隐含。"""
        session = self._capture(OurCampusScraper())
        url = session.post.call_args[0][0]
        assert "contentclass=availableunits" in url
        assert "MoveInDate" not in url
        assert "myolePropertyID" not in url

    def test_ourdomain_still_uses_get(self):
        """基类不受影响——OurDomain 的 host 上 POST 会 403。"""
        session = self._capture(OurDomainScraper())
        assert session.post.call_count == 0
        assert session.get.call_count == 1
        url = session.get.call_args[0][0]
        assert "floorPlans=1112904" in url
        assert "MoveInDate=2026-09-01" in url
        assert "myolePropertyID=186609" in url


# ── 真实 fixture 解析 ───────────────────────────────────────────────

class TestRealFixtures:
    def test_floorplan_ids_extracted(self):
        assert _extract_floorplan_ids(_FP_HTML) == ["1113259", "1112904", "1112905"]

    def test_floorplan_names_extracted(self):
        names = _extract_floorplan_names(_FP_HTML)
        assert names["1112904"] == "Furnished Student Apartment - 1 Person"
        assert names["1112905"] == "Furnished Student Apartment - 2 Person"
        assert names["1113259"] == "Standard+ Studio Apartment - 1 Person"

    def test_empty_response_is_a_valid_panel(self):
        """零可订时的响应仍是一张结构完整的搜索结果页——不能当成故障。"""
        assert _looks_like_availability_panel(_EMPTY_UNITS) is True
        assert _extract_units(_EMPTY_UNITS) == []


# ── 完整性守卫（基类行为，两个 source 共享）─────────────────────────

class TestAvailabilityPanelGuard:
    """「解析不出单元」要能和「这栋楼没房」区分开。

    RentCafe 两种情况都回 HTTP 200，只看解析结果为空就会把「响应结构变了 /
    拿到别的页面」误报成「没有房」，进而让 stale 收敛把存量 listing 清掉。
    见 ARCHITECTURE.md §5.10。
    """

    def test_real_empty_panel_passes(self):
        assert _looks_like_availability_panel(_EMPTY_UNITS)

    @pytest.mark.parametrize("body", [
        "",
        "<html><body>Something else entirely</body></html>",
        "<html><head><title>Rentcafe Error</title></head><body>404</body></html>",
    ])
    def test_non_panel_bodies_rejected(self, body):
        assert _looks_like_availability_panel(body) is False

    def test_marker_is_case_insensitive(self):
        assert _looks_like_availability_panel("<div>apartment search result</div>")

    def test_scrape_marks_incomplete_when_panel_missing(self, monkeypatch):
        """守卫真的会把该轮标记为 incomplete，而不是返回「0 个单元 + 完整」。"""
        scraper = OurCampusScraper()
        monkeypatch.setattr(
            "scrapers.ourdomain._get_text",
            lambda session, url, **kw: (
                _FP_HTML if "floorplans.aspx" in url
                else "<html><body>不是单元面板</body></html>"
            ),
        )
        monkeypatch.setattr(
            OurCampusScraper, "_fetch_units_html",
            lambda self, session, **kw: "<html><body>不是单元面板</body></html>",
        )
        result = scraper.scrape(ScrapeTask("ourcampus", "diemen", "OurCampus Amsterdam Diemen"))
        assert result.listings == []
        assert result.complete is False, "响应不像单元面板时必须标记不完整"

    def test_scrape_stays_complete_on_genuine_empty(self, monkeypatch):
        """真的没房时要判完整，否则 stale 收敛永不执行。"""
        scraper = OurCampusScraper()
        monkeypatch.setattr(
            "scrapers.ourdomain._get_text",
            lambda session, url, **kw: _FP_HTML,
        )
        monkeypatch.setattr(
            OurCampusScraper, "_fetch_units_html",
            lambda self, session, **kw: _EMPTY_UNITS,
        )
        result = scraper.scrape(ScrapeTask("ourcampus", "diemen", "OurCampus Amsterdam Diemen"))
        assert result.listings == []
        assert result.complete is True


# ── Listing 映射（复用基类，验证品牌字段没串到 OurDomain）─────────────

class TestListingMapping:
    def test_id_prefix_and_source(self, monkeypatch):
        from scrapers.ourdomain import _to_listing
        unit = {"unit_id": "999", "apt": "#A1", "sqft": "22", "rent": "€ 1.500",
                "deposit": "€ 0", "detail": "", "floor": 0, "status": "Available to book",
                "avail_date": "2026-09-01", "fp_ids": ["1112904"]}
        l = _to_listing(
            unit, base_url="https://x.test/fp.aspx",
            city_display="OurCampus Amsterdam Diemen", source="ourcampus",
            building_label="OurCampus Diemen", default_type="Studio",
            street_address="Dalsteindreef 6002, 1112 XC Diemen",
            id_prefix=OurCampusScraper.ID_PREFIX,
        )
        assert l.id == "oc_999"
        assert l.source == "ourcampus"
        assert l.name == "OurCampus Diemen #A1"
        assert "Address: Dalsteindreef 6002, 1112 XC Diemen" in l.features
        # detail 为空时兜底用 source，不能写死成 "OurDomain"
        assert "Detail: ourcampus" in l.features
        assert not any("OurDomain" in f for f in l.features)


# ── 抓取留档 ────────────────────────────────────────────────────────

class TestCapture:
    """存在理由：它的单元表 HTML 至今没有真实样本。

    第一次真的有房时要拿原始 markup 核对解析器——只看日志里的「共抓取 N 个
    单元」不够，最危险的情况恰恰是「结构变了但仍是合法面板」，那会静默返回 0。
    """

    @pytest.fixture
    def cap(self, tmp_path, monkeypatch):
        path = tmp_path / "cap.txt"
        monkeypatch.setenv("OURCAMPUS_CAPTURE_PATH", str(path))
        return path

    def _read(self, path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_empty_response_logs_summary_only(self, cap):
        from scrapers.ourcampus import _record_capture
        _record_capture("1112904", _EMPTY_UNITS)
        txt = self._read(cap)
        assert "fp=1112904" in txt and "parsed=0" in txt and "panel=yes" in txt
        # 零可订是常态，不能每轮都塞一份 32KB HTML
        assert "完整响应" not in txt
        assert len(txt) < 500

    def test_response_with_units_keeps_full_html(self, cap):
        from scrapers.ourcampus import _record_capture
        html = _EMPTY_UNITS.replace(
            "Apartment Search Result",
            'Apartment Search Result</div><tr id="unitrow_999" data-selenium-id="urow1">'
            '<th data-selenium-id="Apt1" id="999">#OC1</th>'
            '<td data-selenium-id="SqFt1">24</td></tr>',
        )
        _record_capture("1113259", html)
        txt = self._read(cap)
        assert "parsed=1" in txt and "unitrow=yes" in txt
        assert "完整响应" in txt, "第一份真实样本必须完整留下来"
        assert "unitrow_999" in txt

    def test_unitrow_present_but_parsed_zero_is_kept(self, cap):
        """守卫兜不住的那种情况：仍是合法面板，但解析器对不上。"""
        from scrapers.ourcampus import _record_capture
        html = _EMPTY_UNITS.replace(
            "Apartment Search Result",
            'Apartment Search Result</div><tr id="unitrow_888" class="brand-new-theme"></tr>',
        )
        _record_capture("1112905", html)
        txt = self._read(cap)
        assert "unitrow=yes" in txt and "parsed=0" in txt
        assert "完整响应" in txt, "解析器可能失配时更要留原始 HTML"

    def test_non_panel_response_is_flagged(self, cap):
        from scrapers.ourcampus import _record_capture
        _record_capture("1112904", "<html><body>Rentcafe Error</body></html>")
        assert "panel=NO" in self._read(cap)

    def test_appends_across_calls(self, cap):
        from scrapers.ourcampus import _record_capture
        for fp in ("a", "b", "c"):
            _record_capture(fp, _EMPTY_UNITS)
        heads = [l for l in self._read(cap).splitlines() if l.startswith("=== ")]
        assert len(heads) == 3

    def test_size_cap_stops_writing(self, cap, monkeypatch):
        import scrapers.ourcampus as oc
        monkeypatch.setattr(oc, "_CAPTURE_MAX_BYTES", 10)  # 一行摘要就超
        oc._record_capture("a", _EMPTY_UNITS)
        first = self._read(cap)
        oc._record_capture("b", _EMPTY_UNITS)
        assert self._read(cap) == first, "超过上限后不再写入"

    def test_failure_never_breaks_scraping(self, cap, monkeypatch):
        """留档是排查辅助，绝不能因为它抓取失败。"""
        import scrapers.ourcampus as oc
        monkeypatch.setattr(oc, "_capture_path", lambda: (_ for _ in ()).throw(OSError("boom")))
        oc._record_capture("a", _EMPTY_UNITS)  # 不抛异常即通过

    def test_hooked_into_fetch(self, cap):
        """真实调用路径上确实会写——不是只有直接调 _record_capture 才写。"""
        session = MagicMock()
        resp = MagicMock(status_code=200, ok=True, text=_EMPTY_UNITS)
        resp.raise_for_status = lambda: None
        session.post.return_value = resp
        OurCampusScraper()._fetch_units_html(
            session, base="https://x.test/onlineleasing", fp_id="1112904",
            property_id="186609", move_in_date="2026-09-01",
            floorplans_url="https://x.test/onlineleasing/s/floorplans.aspx",
        )
        assert "fp=1112904" in self._read(cap)
