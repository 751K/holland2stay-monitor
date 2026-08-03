"""影子 source 测试。

影子 source = 照常抓取入库，但**不发任何通知**。用于新平台上线前的静默验证：
先确认它抓得对、数据长什么样，再决定是否对用户开放。

被拦掉的只有「告诉谁」这一步——`storage.diff()` 照常执行，房源进库、状态变更
进流水、参与 stale 收敛和面板统计。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from config import AvailabilityFilter, CityFilter, Config
from models import Listing
from monitor import _drop_shadow_sources, run_once
from notifier import BaseNotifier
from storage import Storage


def _listing(lid: str, *, source: str, status: str = "Available to book") -> Listing:
    return Listing(
        id=lid, name=f"L {lid}", status=status, price_raw="€700",
        available_from="2030-01-01", features=[], url=f"https://e.test/{lid}",
        city="C", source=source,
    )


class _Cfg:
    def __init__(self, shadow):
        self.shadow_sources = shadow


# ── 纯函数 ──────────────────────────────────────────────────────────

class TestDropShadowSources:
    def test_no_shadow_is_passthrough(self):
        new = [_listing("a", source="ourcampus")]
        sc = [(_listing("b", source="ourcampus"), "Occupied", "Available to book")]
        kn, ks = _drop_shadow_sources(_Cfg([]), new, sc)
        assert kn is new and ks is sc, "未配置影子时应零开销直接返回原对象"

    def test_filters_only_shadow_source(self):
        new = [_listing("a", source="ourcampus"), _listing("b", source="holland2stay")]
        sc = [
            (_listing("c", source="ourcampus"), "Occupied", "Available to book"),
            (_listing("d", source="ourdomain"), "Occupied", "Available to book"),
        ]
        kn, ks = _drop_shadow_sources(_Cfg(["ourcampus"]), new, sc)
        assert [l.id for l in kn] == ["b"]
        assert [t[0].id for t in ks] == ["d"]

    def test_source_match_is_case_insensitive(self):
        new = [_listing("a", source="OurCampus")]
        kn, _ = _drop_shadow_sources(_Cfg(["ourcampus"]), new, [])
        assert kn == []

    def test_multiple_shadow_sources(self):
        new = [_listing(s, source=s) for s in ("ourcampus", "xior", "holland2stay")]
        kn, _ = _drop_shadow_sources(_Cfg(["ourcampus", "xior"]), new, [])
        assert [l.id for l in kn] == ["holland2stay"]

    def test_missing_attr_is_tolerated(self):
        """cfg 没有 shadow_sources 属性时不能炸（旧配置对象 / 测试桩）。"""
        class _Bare:
            pass
        new = [_listing("a", source="ourcampus")]
        kn, _ = _drop_shadow_sources(_Bare(), new, [])
        assert kn is new


# ── 配置解析 ────────────────────────────────────────────────────────

class TestShadowConfig:
    def test_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "holland2stay,ourcampus")
        monkeypatch.setenv("SHADOW_SOURCES", "ourcampus")
        import config
        assert config.load_config().shadow_sources == ["ourcampus"]

    def test_entries_not_in_sources_are_dropped(self, monkeypatch):
        """影子列表必须是 SOURCES 的子集——不在里面就压根不会抓，写了是笔误。"""
        monkeypatch.setenv("SOURCES", "holland2stay")
        monkeypatch.setenv("SHADOW_SOURCES", "ourcampus,typo")
        import config
        assert config.load_config().shadow_sources == []

    def test_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("SHADOW_SOURCES", raising=False)
        monkeypatch.setenv("SOURCES", "holland2stay")
        import config
        assert config.load_config().shadow_sources == []

    def test_sources_still_defaults_when_empty(self, monkeypatch):
        """拆出 _parse_sources_raw 后，SOURCES 的默认值行为不能变。"""
        monkeypatch.setenv("SOURCES", "")
        import config
        assert config.load_config().sources == ["holland2stay"]


# ── run_once 端到端：入库但不通知 ───────────────────────────────────

class _Notifier(BaseNotifier):
    has_channels = True

    def __init__(self) -> None:
        self.new_listings: list[str] = []
        self.status_changes: list[str] = []

    async def _send(self, text):
        return True

    async def send_new_listing(self, listing):
        self.new_listings.append(listing.id)
        return True

    async def send_status_change(self, listing, old, new):
        self.status_changes.append(listing.id)
        return True

    async def close(self):
        pass


def _cfg(tmp_path, shadow) -> Config:
    return Config(
        check_interval=300,
        cities=[CityFilter(name="C", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=Path(tmp_path) / "t.db",
        log_level="WARNING",
        sources=["holland2stay", "ourcampus"],
        shadow_sources=shadow,
    )


def _round(tmp_path, storage, shadow, fresh):
    notifier = _Notifier()

    async def go():
        with patch("monitor.dispatch_scrape_tasks", return_value=(fresh, {"C": True})), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None):
            await run_once(_cfg(tmp_path, shadow), storage, [], dry_run=False,
                           web_notifier=notifier)
    asyncio.run(go())
    return notifier


class TestRunOnceShadow:
    def test_shadow_listing_is_stored_but_not_notified(self, tmp_path):
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            n = _round(tmp_path, storage, ["ourcampus"], [
                _listing("oc_1", source="ourcampus"),
                _listing("h2s_1", source="holland2stay"),
            ])
            # 通知只发了非影子的那条
            assert n.new_listings == ["h2s_1"]
            # 但两条都进了库
            assert storage.get_listing("oc_1") is not None
            assert storage.get_listing("h2s_1") is not None
            assert sorted(storage.get_distinct_sources()) == ["holland2stay", "ourcampus"]
        finally:
            storage.close()

    def test_shadow_status_change_is_recorded_but_not_notified(self, tmp_path):
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            _round(tmp_path, storage, ["ourcampus"],
                   [_listing("oc_1", source="ourcampus", status="Occupied")])
            n = _round(tmp_path, storage, ["ourcampus"],
                       [_listing("oc_1", source="ourcampus", status="Available to book")])
            assert n.status_changes == [], "影子 source 的状态变更不该通知"
            # 但库里的状态确实更新了
            assert storage.get_listing("oc_1")["status"] == "Available to book"
        finally:
            storage.close()

    def test_without_shadow_everything_notifies(self, tmp_path):
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            n = _round(tmp_path, storage, [], [
                _listing("oc_1", source="ourcampus"),
                _listing("h2s_1", source="holland2stay"),
            ])
            assert sorted(n.new_listings) == ["h2s_1", "oc_1"]
        finally:
            storage.close()

    def test_scrape_count_includes_shadow(self, tmp_path):
        """last_scrape_count 回答「抓到多少」，不是「通知了多少」。"""
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            _round(tmp_path, storage, ["ourcampus"], [
                _listing("oc_1", source="ourcampus"),
                _listing("h2s_1", source="holland2stay"),
            ])
            assert storage.get_meta("last_scrape_count") == "2"
        finally:
            storage.close()
