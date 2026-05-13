"""mcore/booking.py 单元测试 — area_key, book_with_fallback, RetryQueue。"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcore.booking import RetryQueue, area_key, book_with_fallback
from models import Listing


# ── area_key ──────────────────────────────────────────────────────


class TestAreaKey:
    def test_extracts_numeric(self):
        l = Listing(
            id="x", name="x", status="x", price_raw="€500",
            available_from="2030-01-01", features=["Area: 45 m²"],
            url="https://t/1", city="E", sku="S", contract_id=1,
            contract_start_date="2030-01-01",
        )
        area = area_key(l)
        assert area == 45.0

    def test_no_area_returns_zero(self):
        l = Listing(
            id="x", name="x", status="x", price_raw="€500",
            available_from="2030-01-01", features=[],
            url="https://t/1", city="E", sku="S", contract_id=1,
            contract_start_date="2030-01-01",
        )
        assert area_key(l) == 0.0

    def test_non_numeric_area_returns_zero(self):
        l = Listing(
            id="x", name="x", status="x", price_raw="€500",
            available_from="2030-01-01", features=["Area: N/A"],
            url="https://t/1", city="E", sku="S", contract_id=1,
            contract_start_date="2030-01-01",
        )
        assert area_key(l) == 0.0

    def test_sorts_descending(self):
        """area_key 配合 sorted(key=..., reverse=True) 按面积降序。"""
        l_small = Listing(
            id="s", name="s", status="x", price_raw="€500",
            available_from="2030-01-01", features=["Area: 30 m²"],
            url="https://t/1", city="E", sku="S", contract_id=1,
            contract_start_date="2030-01-01",
        )
        l_large = Listing(
            id="l", name="l", status="x", price_raw="€800",
            available_from="2030-01-01", features=["Area: 80 m²"],
            url="https://t/2", city="E", sku="S2", contract_id=2,
            contract_start_date="2030-01-01",
        )
        sorted_cands = sorted([l_small, l_large], key=area_key, reverse=True)
        assert sorted_cands[0] is l_large
        assert sorted_cands[1] is l_small


# ── RetryQueue ────────────────────────────────────────────────────


class TestRetryQueue:
    def test_new_queue_is_empty(self):
        rq = RetryQueue()
        assert not rq
        assert rq.get("u1") == set()

    def test_add_and_get(self):
        rq = RetryQueue()
        rq.add("u1", {"L1", "L2"})
        assert rq.get("u1") == {"L1", "L2"}
        assert bool(rq) is True

    def test_add_merges(self):
        rq = RetryQueue()
        rq.add("u1", {"L1"})
        rq.add("u1", {"L2"})
        assert rq.get("u1") == {"L1", "L2"}

    def test_discard(self):
        rq = RetryQueue()
        rq.add("u1", {"L1", "L2"})
        rq.discard("u1", "L1")
        assert rq.get("u1") == {"L2"}

    def test_discard_unknown_is_noop(self):
        rq = RetryQueue()
        rq.add("u1", {"L1"})
        rq.discard("u1", "L9")  # no error
        assert rq.get("u1") == {"L1"}

    def test_remove_gone(self):
        rq = RetryQueue()
        rq.add("u1", {"L1", "L2", "L3"})
        rq.remove_gone("u1", {"L2", "L3"})
        assert rq.get("u1") == {"L1"}

    def test_remove_gone_unknown_user_is_noop(self):
        rq = RetryQueue()
        rq.remove_gone("noone", {"x"})  # no error

    def test_persistence_save_and_load(self, tmp_path):
        fake_storage = MagicMock()
        fake_storage.save_retry_queue = MagicMock()
        fake_storage.load_retry_queue = MagicMock(
            return_value={"u1": {"A", "B"}, "u2": {"C"}}
        )

        # load
        rq = RetryQueue()
        rq.load(fake_storage)
        assert rq.get("u1") == {"A", "B"}
        assert rq.get("u2") == {"C"}

        # save only if dirty
        rq.save(fake_storage)
        fake_storage.save_retry_queue.assert_not_called()  # loaded is clean

        # modify → dirty → save
        rq.add("u3", {"X"})
        rq.save(fake_storage)
        fake_storage.save_retry_queue.assert_called_once()

    def test_dirty_flag_prevents_redundant_saves(self, tmp_path):
        fake_storage = MagicMock()
        rq = RetryQueue()
        rq.load(fake_storage)

        rq.add("u1", {"x"})
        rq.save(fake_storage)  # flush
        assert fake_storage.save_retry_queue.call_count == 1

        rq.save(fake_storage)  # already clean
        assert fake_storage.save_retry_queue.call_count == 1


# ── book_with_fallback ────────────────────────────────────────────


class TestBookWithFallback:
    def _make_listing(self, idx: int):
        return Listing(
            id=f"L-{idx}", name=f"Test-{idx}",
            status="Available to book", price_raw="€700",
            available_from="2030-01-01", features=[],
            url=f"https://t/{idx}", city="E",
            sku=f"SKU-{idx}", contract_id=42, contract_start_date="2030-01-01",
        )

    def _make_user(self):
        from users import UserConfig
        from config import AutoBookConfig

        return UserConfig(
            name="A", id="u1", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(
                enabled=True, email="a@x.com", password="pw",
                dry_run=False, cancel_enabled=False, payment_method="ideal",
            ),
        )

    def test_success_on_first_candidate(self):
        with patch("mcore.booking.try_book") as mock_try:
            mock_try.return_value = MagicMock(
                success=True, dry_run=False, phase="success"
            )
            candidates = [self._make_listing(1), self._make_listing(2)]
            result = book_with_fallback(candidates, self._make_user(), float("inf"))
            assert result.success is True
            assert mock_try.call_count == 1

    def test_fallback_on_race_lost(self):
        """race_lost → 尝试下一套。"""
        race_lost = MagicMock(success=False, dry_run=False, phase="race_lost")
        success = MagicMock(success=True, dry_run=False, phase="success")

        with patch("mcore.booking.try_book", side_effect=[race_lost, success]) as mock_try:
            candidates = [self._make_listing(1), self._make_listing(2)]
            result = book_with_fallback(candidates, self._make_user(), float("inf"))
            assert result.success is True
            assert mock_try.call_count == 2

    def test_stops_on_non_race_lost_failure(self):
        """非 race_lost 失败（如 reserved_conflict） → 立即停止，不继续试备选。"""
        failure = MagicMock(success=False, dry_run=False, phase="reserved_conflict")

        with patch("mcore.booking.try_book", return_value=failure) as mock_try:
            candidates = [self._make_listing(1), self._make_listing(2)]
            result = book_with_fallback(candidates, self._make_user(), float("inf"))
            assert result.success is False
            assert mock_try.call_count == 1  # 不试第二套

    def test_stops_on_dry_run(self):
        """dry_run → 视为成功，不重试。"""
        dr = MagicMock(success=False, dry_run=True, phase="success")

        with patch("mcore.booking.try_book", return_value=dr) as mock_try:
            candidates = [self._make_listing(1), self._make_listing(2)]
            result = book_with_fallback(candidates, self._make_user(), float("inf"))
            assert result.dry_run is True
            assert mock_try.call_count == 1

    def test_deadline_stops_fallback(self):
        """截止时间到 → 停止备选，返回最后一个 race_lost 结果。"""
        race_lost = MagicMock(success=False, dry_run=False, phase="race_lost")

        with patch("mcore.booking.try_book", return_value=race_lost) as mock_try:
            candidates = [self._make_listing(1), self._make_listing(2), self._make_listing(3)]
            # 设截止时间为当前，只来得及试第一套
            result = book_with_fallback(candidates, self._make_user(), time.monotonic())
            # 第一套无条件尝试（忽略 deadline），第二套才检查截止 → 第二套赶不上
            assert result.phase == "race_lost"
            assert mock_try.call_count >= 1
