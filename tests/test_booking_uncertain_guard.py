"""A timed-out booking must not cause a second reservation."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from booker import BookingResult
from mcore import booking_round
from mcore.booking_guard import BookingGuard, attempt_context, record_mutation
from models import Listing
from storage import Storage


@pytest.fixture
def db(tmp_path):
    st = Storage(tmp_path / 'booking.sqlite')
    yield st
    st.close()


def test_guard_is_durable_shared_by_account_and_owner_only(db):
    guard = BookingGuard(db)
    attempt = guard.acquire(' Test@Example.COM ', 'u1', 'Room')
    assert attempt
    assert BookingGuard(db).acquire('test@example.com', 'u2', 'Other') is None
    with pytest.raises(RuntimeError):
        guard.acknowledge(attempt, 'u1')
    assert not guard.acknowledge(attempt, 'u2')
    guard.update(attempt, state='finished')
    assert guard.acknowledge(attempt, 'u1')
    assert guard.acquire('test@example.com', 'u2', 'Other')


def test_stale_attempt_cannot_modify_new_attempt(db):
    guard = BookingGuard(db)
    first = guard.acquire('a@b.c', 'u1', 'Room')
    guard.update(first, state='finished')
    assert guard.acknowledge(first, 'u1')
    second = guard.acquire('a@b.c', 'u1', 'Room 2')
    with pytest.raises(RuntimeError):
        guard.update(first, mutated=1)
    assert guard.get_attempt(second)['mutated'] == 0


def test_timeout_preserves_guard_until_worker_finishes_and_user_acknowledges(db, monkeypatch):
    listing = Listing(id='1', name='Room', status='Available to book', price_raw='€1',
                      available_from='', features=[], url='', city='', source='holland2stay')
    user = SimpleNamespace(id='u1', name='User', auto_book=SimpleNamespace(
        enabled=True, dry_run=False, email='x@example.com'))
    notifications = []
    class Notifier:
        async def send_booking_uncertain(self, title, body):
            notifications.append((title, body))
    class Push:
        async def dispatch_announcement_to_user(self, *args):
            return 1
    started, finish = threading.Event(), threading.Event()
    def book(*args, **kwargs):
        record_mutation('Room')
        started.set()
        assert finish.wait(5)
        return BookingResult(listing, True, 'done', pay_url='https://payment.example', phase='success')
    class FakeWorker:
        def run(self, action, payload, *, timeout, guard, job):
            with attempt_context(*guard):
                return book()
    monkeypatch.setattr(booking_round, 'get_booking_worker', lambda uid: FakeWorker())
    monkeypatch.setattr(booking_round, '_BOOKING_RESULT_TIMEOUT_SEC', 0.02)
    monkeypatch.setattr(booking_round, 'h2s_login_suppressed_remaining', lambda: 0)

    async def exercise():
        loop = asyncio.get_running_loop()
        futures = booking_round.submit_bookings(loop, {'u1': [listing]}, [(user, Notifier())], {}, {}, {}, float('inf'), db)
        assert started.wait(2)
        await booking_round.process_booking_results(futures, None, db, Push())
        guard = BookingGuard(db)
        row = guard.get_account(user.auto_book.email)
        assert row['timed_out'] == 1 and row['state'] == 'running'
        assert notifications and '不确定' in notifications[0][1]
        assert booking_round.submit_bookings(loop, {'u1': [listing]}, [(user, Notifier())], {}, {}, {}, float('inf'), db) == []
        with pytest.raises(RuntimeError):
            guard.acknowledge(row['attempt'], user.id)
        finish.set()
        await futures[0][3]
        row = guard.get_account(user.auto_book.email)
        assert row['state'] == 'finished' and row['pay_url'] == 'available'
        assert guard.acknowledge(row['attempt'], user.id)
        assert guard.get_account(user.auto_book.email) is None
    try:
        asyncio.run(exercise())
    finally:
        finish.set()


def test_mutation_failure_does_not_try_fallback(db, monkeypatch):
    from mcore import booking
    listing = SimpleNamespace(source='holland2stay', name='Room')
    guard = BookingGuard(db)
    attempt = guard.acquire('x@example.com', 'u1', 'Room')
    calls = []
    def dispatch(request):
        calls.append(request.listing)
        record_mutation('Room')
        return BookingResult(request.listing, False, 'connection lost', phase='race_lost')
    monkeypatch.setattr(booking, 'dispatch_book', dispatch)
    monkeypatch.setattr(booking, 'supports_booking', lambda source: True)
    user = SimpleNamespace(name='User', auto_book=SimpleNamespace(dry_run=False))
    with attempt_context(guard, attempt):
        result = booking.book_with_fallback([listing, listing], user, float('inf'))
    assert result.phase == 'uncertain'
    assert len(calls) == 1


def test_uncertain_create_is_reported_without_retrying(db, monkeypatch):
    import time
    from unittest.mock import MagicMock
    import booker
    listing = Listing(id='1', name='Room', status='Available to book', price_raw='€1',
                      available_from='', features=[], url='', city='', source='holland2stay',
                      sku='SKU1')
    now = time.monotonic()
    session = booker.PrewarmedSession(fetcher=MagicMock(), token='token', created_at=now,
                                     token_expiry=now + 100, email='x@example.com')
    calls = []
    def rejected(*args):
        calls.append(args)
        raise RuntimeError('account already has a reserved booking')
    monkeypatch.setattr(booker, 'create_booking', rejected)
    guard = BookingGuard(db)
    attempt = guard.acquire('x@example.com', 'u1', 'Room')
    with attempt_context(guard, attempt):
        result = booker.try_book(listing, 'x@example.com', 'pw', prewarmed=session)
    assert not result.success
    assert guard.get_attempt(attempt)['mutated'] == 1
    assert len(calls) == 1


def test_review_page_requires_explicit_confirmation(admin_client, test_app):
    from users import load_users
    from app.db import storage
    response = admin_client.post('/users/new', data={'csrf_token': 'test_csrf',
                                                      'name': 'Booking review', 'enabled': 'true'})
    assert response.status_code == 302
    with test_app.app_context():
        uid = next(u.id for u in load_users() if u.name == 'Booking review')
        guard = BookingGuard(storage())
        attempt = guard.acquire('x@example.com', uid, 'Room')
        guard.update(attempt, state='finished')
    admin_client.set_cookie('h2s-lang', 'zh')
    page = admin_client.get(f'/users/{uid}')
    assert '已核实，恢复自动预订' in page.get_data(as_text=True)
    assert admin_client.post(f'/users/{uid}/booking-acknowledge', data={'attempt': attempt}).status_code in (400, 403)
    assert guard.get_attempt(attempt)
    response = admin_client.post(f'/users/{uid}/booking-acknowledge',
                                 data={'csrf_token': 'test_csrf', 'attempt': attempt})
    assert response.status_code == 302
    assert guard.get_attempt(attempt) is None


def test_dead_monitor_cannot_clear_while_booking_child_lives(db):
    import os
    guard = BookingGuard(db)
    attempt = guard.acquire('x@example.com', 'u1', 'Room')
    guard.update(attempt, pid=99999999, child_pid=os.getpid())
    with pytest.raises(RuntimeError):
        guard.acknowledge(attempt, 'u1')
