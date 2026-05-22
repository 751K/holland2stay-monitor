"""monitor 自动预订候选分配测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from booker import BookingResult
from config import AutoBookConfig, AvailabilityFilter, CityFilter, Config
from models import Listing
from monitor import _assign_auto_book_candidates, run_once
from notifier import BaseNotifier, WebNotifier
from storage import Storage
from users import UserConfig


def _listing(lid: str = "L-1") -> Listing:
    return Listing(
        id=lid,
        name=f"Listing {lid}",
        status="Available to book",
        price_raw="€700",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{lid}",
        city="E",
        source="holland2stay",
        sku=f"SKU-{lid}",
        contract_id=42,
        contract_start_date="2030-01-01",
    )


def _user(uid: str, name: str, email: str) -> UserConfig:
    return UserConfig(
        id=uid,
        name=name,
        enabled=True,
        notifications_enabled=True,
        notification_channels=[],
        auto_book=AutoBookConfig(
            enabled=True,
            email=email,
            password="pw",
            dry_run=False,
        ),
    )


class _Notifier(BaseNotifier):
    has_channels = True

    def __init__(self) -> None:
        self.booking_success: list[str] = []
        self.new_listings: list[str] = []

    async def _send(self, text):
        return True

    async def send_new_listing(self, listing):
        self.new_listings.append(listing.id)
        return True

    async def send_booking_success(self, listing, msg, pay_url="", contract_start_date=""):
        self.booking_success.append(listing.id)
        return True

    async def close(self):
        pass


def _cfg(tmp_path) -> Config:
    return Config(
        check_interval=300,
        cities=[CityFilter(name="E", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=Path(tmp_path) / "test.db",
        log_level="WARNING",
    )


def test_assign_auto_book_candidates_dedupes_same_listing():
    u1 = _user("u1", "Alice", "a@example.test")
    u2 = _user("u2", "Bob", "b@example.test")
    notifier_pairs = [(u1, _Notifier()), (u2, _Notifier())]
    listing = _listing("same")

    assigned = _assign_auto_book_candidates(
        {"u1": [listing], "u2": [listing]},
        notifier_pairs,
    )

    assert sum(len(v) for v in assigned.values()) == 1
    assert assigned["u1"] == [listing]
    assert assigned["u2"] == []


def test_assign_auto_book_candidates_balances_multiple_listings():
    u1 = _user("u1", "Alice", "a@example.test")
    u2 = _user("u2", "Bob", "b@example.test")
    notifier_pairs = [(u1, _Notifier()), (u2, _Notifier())]
    listings = [_listing("L1"), _listing("L2"), _listing("L3")]

    assigned = _assign_auto_book_candidates(
        {"u1": listings, "u2": listings},
        notifier_pairs,
    )

    assert [l.id for l in assigned["u1"]] == ["L1", "L3"]
    assert [l.id for l in assigned["u2"]] == ["L2"]


def test_run_once_books_same_listing_once_for_multiple_matching_users(tmp_path):
    u1 = _user("u1", "Alice", "a@example.test")
    u2 = _user("u2", "Bob", "b@example.test")
    n1 = _Notifier()
    n2 = _Notifier()
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")
    called_emails: list[str] = []

    def fake_try_book(listing, email, *args, **kwargs):
        called_emails.append(email)
        return BookingResult(
            listing,
            success=True,
            message="ok",
            pay_url="https://pay.example.test",
            phase="success",
        )

    async def go():
        with patch("monitor.dispatch_scrape_tasks", return_value=[_listing("shared")]), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None), \
             patch("bookers.holland2stay.try_book", side_effect=fake_try_book):
            await run_once(_cfg(tmp_path), storage, [(u1, n1), (u2, n2)], dry_run=False)

    try:
        asyncio.run(go())
    finally:
        storage.close()

    assert called_emails == ["a@example.test"]
    assert n1.booking_success == ["shared"]
    assert n2.booking_success == []
    assert n1.new_listings == ["shared"]
    assert n2.new_listings == ["shared"]


def test_run_once_web_booking_notification_is_scoped_to_assigned_user(tmp_path):
    u1 = _user("u1", "Alice", "a@example.test")
    u2 = _user("u2", "Bob", "b@example.test")
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")

    def fake_try_book(listing, email, *args, **kwargs):
        return BookingResult(
            listing,
            success=True,
            message="ok",
            pay_url="https://pay.example.test",
            phase="success",
        )

    async def go():
        with patch("monitor.dispatch_scrape_tasks", return_value=[_listing("shared")]), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None), \
             patch("bookers.holland2stay.try_book", side_effect=fake_try_book):
            await run_once(
                _cfg(tmp_path),
                storage,
                [(u1, _Notifier()), (u2, _Notifier())],
                web_notifier=WebNotifier(storage),
                dry_run=False,
            )

    try:
        asyncio.run(go())
        rows = storage.get_notifications(limit=20)
    finally:
        storage.close()

    booking_rows = [r for r in rows if r["type"] == "booking"]
    assert len(booking_rows) == 1
    assert booking_rows[0]["user_id"] == "u1"
