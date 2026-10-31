"""Uncertain Plaza applications must not be submitted twice."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import bookers.plaza as plaza
from mcore import booking_round
from mcore.plaza_guard import PlazaGuard
from models import Listing
from storage import Storage


@pytest.fixture
def db(tmp_path):
    st = Storage(tmp_path / 'plaza.db')
    yield st
    st.close()


def listing(lid):
    return Listing(id=lid, name='Room ' + lid, status='Available to book',
                   price_raw='€1', available_from='', features=[], url='',
                   city='', source='plaza')


def user(uid='u1', name='User', username='shared'):
    return SimpleNamespace(id=uid, name=name, language='en', auto_book=SimpleNamespace(
        enabled=True, dry_run=False, plaza_username=username, plaza_password='pw'))


class Notifier:
    def __init__(self):
        self.uncertain, self.failed, self.success = [], [], []
    async def send_booking_uncertain(self, title, body):
        self.uncertain.append((title, body))
        return True
    async def send_booking_success(self, obj, *args):
        self.success.append(obj.id)
        return True
    async def send_booking_failed(self, obj, reason):
        self.failed.append((obj.id, reason))
        return True


class Push:
    async def dispatch_announcement_to_user(self, *args):
        return 1
    async def dispatch(self, *args, **kwargs):
        return 1


def mock_plaza(monkeypatch, submit, *, active=True):
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(plaza.PlazaBooker, '_session', staticmethod(lambda: Session()))
    monkeypatch.setattr(plaza.PlazaBooker, '_login', lambda *args: None)
    monkeypatch.setattr(plaza.PlazaBooker, '_object', lambda *args: {})
    monkeypatch.setattr(plaza.PlazaBooker, '_reaction_data', lambda *args: {
        'action': 'add', 'kanReageren': True, 'url': '?add=1&dwellingID=1'})
    monkeypatch.setattr(plaza, 'is_dth', lambda obj: False)
    monkeypatch.setattr(plaza.PlazaBooker, '_form_envelope', lambda *args: {})
    monkeypatch.setattr(plaza.PlazaBooker, '_portal_post', staticmethod(submit))
    monkeypatch.setattr(plaza.PlazaBooker, '_has_active_reaction', lambda *args: active)


def test_account_listing_scope_durability_and_authorization(db):
    guard = PlazaGuard(db)
    attempt = guard.acquire(' Shared ', 'u1', 'pz_42', 'Room')
    assert PlazaGuard(db).acquire('shared', 'u2', '42', 'Room') is None
    assert guard.acquire('shared', 'u2', 'pz_43', 'Other')
    assert guard.acquire('other', 'u2', 'pz_42', 'Room')
    assert not guard.acknowledge(attempt, 'u2')
    with pytest.raises(RuntimeError):
        guard.acknowledge(attempt, 'u1')
    guard.update(attempt, state='finished')
    assert guard.acknowledge(attempt, 'u1')


def test_timeout_blocks_only_same_listing_until_thread_ends(db, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    def submit(*args, **kwargs):
        if not started.is_set():
            assert PlazaGuard(db).get_listing('shared', 'pz_1')['mutated'] == 1
            started.set()
            assert finish.wait(5)
        return {'success': True}
    mock_plaza(monkeypatch, submit)
    monkeypatch.setattr(booking_round, '_BOOKING_RESULT_TIMEOUT_SEC', 0.05)
    owner, notifier = user(), Notifier()
    room = listing('pz_1')
    async def run():
        loop = asyncio.get_running_loop()
        futures = booking_round.submit_bookings(loop, {'u1': [room]}, [(owner, notifier)], {}, {}, {}, float('inf'), db)
        assert started.wait(2)
        await booking_round.process_booking_results(futures, None, db, Push())
        guard = PlazaGuard(db)
        row = guard.get_listing('shared', '1')
        assert row['mutated'] and row['timed_out'] and row['state'] == 'running'
        assert notifier.uncertain and not notifier.failed
        assert booking_round.submit_bookings(loop, {'u1': [room]}, [(owner, notifier)], {}, {}, {}, float('inf'), db) == []
        shared = user('u2')
        assert booking_round.submit_bookings(loop, {'u2': [room]}, [(shared, Notifier())], {}, {}, {}, float('inf'), db) == []
        other_room = listing('pz_2')
        other_futures = booking_round.submit_bookings(loop, {'u1': [other_room]},
                           [(owner, notifier)], {}, {}, {}, float('inf'), db)
        assert len(other_futures) == 1
        assert (await other_futures[0][3]).success
        other_tasks = await booking_round.process_booking_results(other_futures, None, db, Push())
        await asyncio.gather(*other_tasks)
        assert notifier.success == ['pz_2']
        assert guard.get_listing('shared', 'pz_2') is None
        with pytest.raises(RuntimeError):
            guard.acknowledge(row['attempt'], 'u1')
        finish.set()
        await futures[0][3]
        row = guard.get_listing('shared', 'pz_1')
        assert row['state'] == 'finished' and row['timed_out']
        assert guard.acknowledge(row['attempt'], 'u1')
        assert guard.get_listing('shared', 'pz_1') is None
    try:
        asyncio.run(run())
    finally:
        finish.set()


@pytest.mark.parametrize('response,expected', [({'success': False}, 'failed'), (RuntimeError('lost'), 'uncertain')])
def test_submit_rejection_vs_unknown_response(db, monkeypatch, response, expected):
    def submit(*args, **kwargs):
        if isinstance(response, Exception):
            raise plaza.PlazaTransportError(str(response))
        return response
    mock_plaza(monkeypatch, submit)
    owner, notifier, room = user(), Notifier(), listing('pz_1')
    async def run():
        loop = asyncio.get_running_loop()
        futures = booking_round.submit_bookings(loop, {'u1': [room]}, [(owner, notifier)], {}, {}, {}, float('inf'), db)
        tasks = await booking_round.process_booking_results(futures, None, db, Push())
        await asyncio.gather(*tasks)
    asyncio.run(run())
    row = PlazaGuard(db).get_listing('shared', room.id)
    if expected == 'failed':
        assert notifier.failed and not notifier.uncertain and row is None
    else:
        assert notifier.uncertain and not notifier.failed and row and row['mutated']


def test_unavailable_journal_never_submits(db, monkeypatch):
    calls = []
    def broken(_storage):
        raise OSError('disk unavailable')
    monkeypatch.setattr(booking_round, 'PlazaGuard', broken)
    monkeypatch.setattr(booking_round, 'book_with_fallback', lambda *a, **kw: calls.append(True))
    owner = user()
    async def run():
        return booking_round.submit_bookings(asyncio.get_running_loop(),
                    {'u1': [listing('pz_1')]}, [(owner, Notifier())], {}, {}, {}, float('inf'), db)
    assert asyncio.run(run()) == []
    assert calls == []


def test_confirm_page_is_scoped_and_requires_csrf(admin_client, test_app):
    from app.db import storage
    from users import load_users
    r = admin_client.post('/users/new', data={'csrf_token': 'test_csrf',
                         'name': 'Plaza review', 'enabled': 'true'})
    assert r.status_code == 302
    with test_app.app_context():
        uid = next(u.id for u in load_users() if u.name == 'Plaza review')
        guard = PlazaGuard(storage())
        first = guard.acquire('shared', uid, 'pz_1', 'Room 1')
        second = guard.acquire('shared', uid, 'pz_2', 'Room 2')
        guard.update(first, state='finished')
        guard.update(second, state='finished')
    admin_client.set_cookie('h2s-lang', 'en')
    html = admin_client.get(f'/users/{uid}').get_data(as_text=True)
    assert 'Room 1' in html and 'Room 2' in html
    assert 'resume applications for this listing' in html
    assert admin_client.post(f'/users/{uid}/plaza-acknowledge', data={'attempt': first}).status_code in (400, 403)
    assert guard.get_attempt(first)
    r = admin_client.post(f'/users/{uid}/plaza-acknowledge', data={'csrf_token': 'test_csrf', 'attempt': first})
    assert r.status_code == 302
    assert guard.get_attempt(first) is None and guard.get_attempt(second)


def test_record_failure_prevents_react_request(db, monkeypatch):
    calls = []
    mock_plaza(monkeypatch, lambda *args, **kwargs: calls.append(True))
    original = PlazaGuard.update
    def fail_marker(self, attempt, **values):
        if values.get('mutated'):
            raise OSError('journal unavailable')
        return original(self, attempt, **values)
    monkeypatch.setattr(PlazaGuard, 'update', fail_marker)
    owner, notifier, room = user(), Notifier(), listing('pz_1')
    async def run():
        futures = booking_round.submit_bookings(asyncio.get_running_loop(), {'u1': [room]},
                                               [(owner, notifier)], {}, {}, {}, float('inf'), db)
        tasks = await booking_round.process_booking_results(futures, None, db, Push())
        await asyncio.gather(*tasks)
    asyncio.run(run())
    assert not calls
    assert PlazaGuard(db).get_listing('shared', room.id) is None


def test_after_monitor_death_attempt_stays_blocked_and_is_reported(db):
    owner, notifier, room = user(), Notifier(), listing('pz_1')
    guard = PlazaGuard(db)
    attempt = guard.acquire('shared', owner.id, room.id, room.name)
    guard.update(attempt, mutated=1)
    with guard.connect() as conn:
        conn.execute('UPDATE plaza_attempts SET pid=? WHERE attempt=?', (99999999, attempt))
    async def run():
        futures = booking_round.submit_bookings(asyncio.get_running_loop(), {'u1': [room]},
                                               [(owner, notifier)], {}, {}, {}, float('inf'), db)
        assert futures == []
        for _ in range(10):
            if notifier.uncertain:
                break
            await asyncio.sleep(0.01)
        assert notifier.uncertain
    asyncio.run(run())
    row = PlazaGuard(db).get_listing('shared', room.id)
    assert row['timed_out'] and row['mutated']
    assert PlazaGuard(db).acknowledge(attempt, owner.id)


def test_shared_account_user_cannot_see_details_or_clear_anothers_attempt(test_app):
    from app.db import storage
    from config import AutoBookConfig
    from users import UserConfig, save_users
    first = UserConfig(id='first', name='Owner', enabled=True, app_login_enabled=True,
                       auto_book=AutoBookConfig(plaza_username='shared'))
    second = UserConfig(id='second', name='Other', enabled=True, app_login_enabled=True,
                        auto_book=AutoBookConfig(plaza_username='shared'))
    save_users([first, second])
    with test_app.app_context():
        guard = PlazaGuard(storage())
        attempt = guard.acquire('shared', 'first', 'pz_1', 'PRIVATE-ROOM-NAME')
        guard.update(attempt, state='finished')
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['role'] = 'user'
        sess['user_id'] = 'second'
        sess['csrf_token'] = 'test_csrf'
    html = client.get('/users/second').get_data(as_text=True)
    assert 'PRIVATE-ROOM-NAME' not in html and attempt not in html
    assert client.post('/users/first/plaza-acknowledge', data={'csrf_token': 'test_csrf', 'attempt': attempt}).status_code == 302
    assert client.post('/users/second/plaza-acknowledge', data={'csrf_token': 'test_csrf', 'attempt': attempt}).status_code == 302
    assert guard.get_attempt(attempt)


def test_startup_recovers_notice_without_new_listing(db):
    owner, notifier = user(), Notifier()
    guard = PlazaGuard(db)
    attempt = guard.acquire('shared', owner.id, 'pz_1', 'Old room')
    guard.update(attempt, mutated=1)
    with guard.connect() as conn:
        conn.execute('UPDATE plaza_attempts SET pid=? WHERE attempt=?', (99999999, attempt))
    async def run():
        booking_round.notify_interrupted_plaza(db, [(owner, notifier)], Push())
        for _ in range(10):
            if notifier.uncertain:
                break
            await asyncio.sleep(0.01)
        assert notifier.uncertain and 'Old room' in notifier.uncertain[0][1]
        booking_round.notify_interrupted_plaza(db, [(owner, notifier)], Push())
        await asyncio.sleep(0.01)
        assert len(notifier.uncertain) == 1
    asyncio.run(run())
    assert guard.get_attempt(attempt)['timed_out'] == 1


def test_notification_claim_rolls_back_if_web_alert_fails(db):
    import sqlite3
    guard = PlazaGuard(db)
    attempt = guard.acquire('shared', 'u1', 'pz_1', 'Room')
    with guard.connect() as conn:
        conn.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON web_notifications "
                     "BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        guard.claim_notice(attempt, title='title', body='body', user_id='u1')
    assert guard.get_attempt(attempt)['timed_out'] == 0


def test_success_response_with_unconfirmed_reaction_requires_review(db, monkeypatch):
    mock_plaza(monkeypatch, lambda *args, **kwargs: {'success': True}, active=False)
    owner, notifier, room = user(), Notifier(), listing('pz_1')
    async def run():
        futures = booking_round.submit_bookings(asyncio.get_running_loop(), {'u1': [room]},
                                               [(owner, notifier)], {}, {}, {}, float('inf'), db)
        await booking_round.process_booking_results(futures, None, db, Push())
    asyncio.run(run())
    row = PlazaGuard(db).get_listing('shared', room.id)
    assert row and row['mutated'] and row['timed_out']
    assert notifier.uncertain and not notifier.success and not notifier.failed


def test_unreadable_guard_is_reported_and_not_cleared(db, monkeypatch):
    mock_plaza(monkeypatch, lambda *args, **kwargs: {'success': True})
    owner, notifier, room = user(), Notifier(), listing('pz_1')
    original = PlazaGuard.get_attempt
    def unreadable(*args):
        raise OSError('disk read failure')
    async def run():
        futures = booking_round.submit_bookings(asyncio.get_running_loop(), {'u1': [room]},
                                               [(owner, notifier)], {}, {}, {}, float('inf'), db)
        assert (await futures[0][3]).success
        monkeypatch.setattr(PlazaGuard, 'get_attempt', unreadable)
        await booking_round.process_booking_results(futures, None, db, Push())
        monkeypatch.setattr(PlazaGuard, 'get_attempt', original)
    asyncio.run(run())
    assert notifier.uncertain and not notifier.success
    assert PlazaGuard(db).get_listing('shared', room.id)
