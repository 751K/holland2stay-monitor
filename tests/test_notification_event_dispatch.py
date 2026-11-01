"""Verify event semantics through the same multi-channel path used by the monitor."""
import asyncio
from unittest.mock import AsyncMock

import notifier
from mcore.notify_round import notify_new_listings
from models import Listing
from notifier import BaseNotifier, EmailNotifier, MultiNotifier, ResendNotifier
from users import UserConfig


def _listing(lid, status="Available in lottery"):
    return Listing(
        id=lid, name=f"House {lid}", status=status, price_raw="€900",
        available_from="2030-01-01", features=[], url=f"https://example.com/{lid}",
        city="Eindhoven", source="holland2stay",
    )


class _Channel(BaseNotifier):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.messages = []
        self.batches = []

    async def _send(self, text):
        self.messages.append(text)
        return True

    async def send_new_listings_batch(self, listings):
        self.batches.append([listing.id for listing in listings])
        return True

    async def close(self):
        pass


class _NoPush:
    @staticmethod
    def get_client(): return None

    @staticmethod
    def get_fcm_client(): return None


def test_factory_and_monitor_preserve_batch_event_and_user_language(monkeypatch, temp_db):
    monkeypatch.setattr(notifier, "TelegramNotifier", _Channel)
    user = UserConfig(name="test", notification_channels=["telegram"],
                      telegram_token="token", telegram_chat_id="chat", language="zh")
    multi = notifier.create_user_notifier(user)
    channel = multi._notifiers[0]
    listings = [_listing("a"), _listing("b"), _listing("c", "Available to book")]
    temp_db.diff(listings)

    result = asyncio.run(notify_new_listings(listings, [(user, multi)], None, temp_db, _NoPush))

    assert channel.batches == [["a", "b"]]
    assert channel.messages == [notifier._format_new(listings[2], lang="zh")]
    assert result.total_notified == 3
    assert temp_db.pending_new_listings() == []


def test_heartbeat_reaches_channel_override_without_sending_email():
    smtp = EmailNotifier.__new__(EmailNotifier)
    resend = ResendNotifier.__new__(ResendNotifier)
    smtp._send = AsyncMock(return_value=True)
    resend._send = AsyncMock(return_value=True)
    channel = _Channel()

    assert asyncio.run(MultiNotifier([smtp, resend, channel]).send_heartbeat(10, 2))
    smtp._send.assert_not_awaited()
    resend._send.assert_not_awaited()
    assert len(channel.messages) == 1 and "Heartbeat" in channel.messages[0]


def test_event_failures_are_isolated_and_disabled_notifier_is_silent():
    failing, good = _Channel(), _Channel()
    failing.send_status_change = AsyncMock(side_effect=ValueError("bad channel"))
    listing = _listing("a")
    assert asyncio.run(MultiNotifier([failing, good]).send_status_change(listing, "old", "new"))
    assert len(good.messages) == 1
    assert not asyncio.run(MultiNotifier([good], enabled=False).send_status_change(listing, "old", "new"))
    assert len(good.messages) == 1


def test_event_retry_only_for_transport_exceptions(monkeypatch):
    monkeypatch.setattr(notifier.asyncio, "sleep", AsyncMock())
    transient, rejected = _Channel(), _Channel()
    transient.send_booking_uncertain = AsyncMock(side_effect=[OSError("reset"), True])
    rejected.send_booking_uncertain = AsyncMock(return_value=False)

    assert asyncio.run(MultiNotifier([transient, rejected]).send_booking_uncertain("Check", "Details"))
    assert transient.send_booking_uncertain.await_count == 2
    assert rejected.send_booking_uncertain.await_count == 1
