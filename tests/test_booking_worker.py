"""Spawned booking workers retain sessions and reap timed-out browsers."""
import os
import time
from types import SimpleNamespace

import pytest

from mcore.booking_guard import BookingGuard
from mcore.booking_worker import BookingWorker, RemoteSession
from storage import Storage


class _Session:
    email = 'a@example.test'
    created_at = 1.0
    token_expiry = 10**12
    def close_if_owned(self):
        pass


def _entry(conn, source):
    import booker
    import mcore.booking
    from mcore.booking_guard import record_mutation
    from mcore.booking_worker import _serve_booking
    from booker import BookingResult
    booker.create_prewarmed_session = lambda *a, **kw: _Session()
    def book(candidates, user, deadline, prewarmed=None):
        record_mutation('Room')
        if user.name == 'hang':
            time.sleep(30)
        return BookingResult(candidates[0], True, str(bool(prewarmed)), phase='success')
    mcore.booking.book_with_fallback = book
    _serve_booking(conn, source)


def _user(name='normal'):
    return SimpleNamespace(name=name, auto_book=SimpleNamespace(email='a@example.test'))


def _payload(user, attempt, path, use_prewarm=True):
    return {'user': user, 'candidates': [SimpleNamespace(name='Room')],
            'deadline': float('inf'), 'guard': (path, attempt), 'use_prewarm': use_prewarm}


def test_prewarm_and_booking_share_worker_and_persist_guard(tmp_path):
    storage = Storage(tmp_path / 'test.db')
    guard = BookingGuard(storage)
    attempt = guard.acquire('a@example.test', 'u1', 'Room')
    worker = BookingWorker('u1', entrypoint=_entry)
    try:
        email, created, expires = worker.run('prewarm', {'email': 'a@example.test',
            'password': 'pw', 'may_borrow': False}, timeout=10)
        remote = RemoteSession(worker, email, created, expires)
        pid = worker.process.pid
        job = SimpleNamespace(safe=True)
        result = worker.run('book', _payload(_user(), attempt, guard.path),
                            timeout=10, guard=(guard, attempt), job=job)
        assert result.success and result.message == 'True'
        assert worker.process.pid == pid and remote.is_alive()
        assert guard.get_attempt(attempt)['mutated'] == 1
        assert guard.get_attempt(attempt)['pid'] == os.getpid()
        assert guard.get_attempt(attempt)['child_pid'] == pid
        assert job.safe
    finally:
        worker.close()
        storage.close()
    assert not remote.is_alive()


def test_booking_timeout_kills_worker_and_preserves_mutation_record(tmp_path):
    storage = Storage(tmp_path / 'test.db')
    guard = BookingGuard(storage)
    attempt = guard.acquire('a@example.test', 'u1', 'Room')
    worker = BookingWorker('u1', entrypoint=_entry)
    job = SimpleNamespace(safe=True)
    try:
        with pytest.raises(TimeoutError):
            worker.run('book', _payload(_user('hang'), attempt, guard.path),
                       timeout=2, guard=(guard, attempt), job=job)
        assert job.safe and worker.process is None
        assert guard.get_attempt(attempt)['mutated'] == 1
        result = worker.run('book', _payload(_user(), attempt, guard.path, False),
                            timeout=10, guard=(guard, attempt), job=job)
        assert result.success and result.message == 'False'
    finally:
        worker.close()
        storage.close()


def _hanging_prewarm(conn, source):
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    conn.recv()
    time.sleep(30)


def test_prewarm_timeout_kills_worker_and_invalidates_session(tmp_path):
    worker = BookingWorker('u1', entrypoint=_hanging_prewarm)
    try:
        with pytest.raises(TimeoutError):
            worker.run('prewarm', {'email': 'a@example.test', 'password': 'pw',
                                    'may_borrow': False}, timeout=1)
        assert worker.process is None
    finally:
        worker.close()


def test_failed_cleanup_keeps_guard_unresolved(tmp_path, monkeypatch):
    storage = Storage(tmp_path / 'test.db')
    guard = BookingGuard(storage)
    attempt = guard.acquire('a@example.test', 'u1', 'Room')
    worker = BookingWorker('u1', entrypoint=_hanging_prewarm)
    job = SimpleNamespace(safe=True)
    real_stop = worker.stop
    monkeypatch.setattr(worker, 'stop', lambda: (_ for _ in ()).throw(RuntimeError('cleanup failed')))
    try:
        with pytest.raises(RuntimeError, match='cleanup failed'):
            worker.run('book', _payload(_user(), attempt, guard.path), timeout=1,
                       guard=(guard, attempt), job=job)
        assert not job.safe
        with pytest.raises(RuntimeError):
            guard.acknowledge(attempt, 'u1')
    finally:
        monkeypatch.setattr(worker, 'stop', real_stop)
        worker.close()
        storage.close()


def _heartbeat_invalidates(conn, source):
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    while True:
        try:
            action, payload, env = conn.recv()
        except EOFError:
            return
        conn.send(('result', action == 'heartbeat', None))


def test_heartbeat_rebuild_invalidates_cached_remote_session():
    worker = BookingWorker('u1', entrypoint=_heartbeat_invalidates)
    try:
        worker.run('prepare', {}, timeout=10)
        session = RemoteSession(worker, 'a@example.test', 1, 10**12)
        assert session.is_alive()
        worker.run('heartbeat', {'wanted': True, 'may_rebuild': True}, timeout=10)
        assert not session.is_alive()
    finally:
        worker.close()


def test_pending_prewarm_uses_independent_booking_worker(tmp_path, monkeypatch):
    import asyncio
    from booker import BookingResult
    from models import Listing
    from mcore import booking_round
    storage = Storage(tmp_path / 'test.db')
    listing = Listing(id='1', name='Room', status='Available to book', price_raw='€1',
                      available_from='', features=[], url='', city='', source='holland2stay')
    user = SimpleNamespace(id='u1', name='User', auto_book=SimpleNamespace(
        enabled=True, dry_run=False, email='a@example.test'))
    chosen = []
    class FakeWorker:
        def run(self, action, payload, **kwargs):
            assert action == 'book'
            return BookingResult(listing, True, 'ok', phase='success')
    def get_worker(uid):
        chosen.append(uid)
        return FakeWorker()
    monkeypatch.setattr(booking_round, 'get_booking_worker', get_worker)
    monkeypatch.setattr(booking_round, 'h2s_login_suppressed_remaining', lambda: 0)
    async def run():
        loop = asyncio.get_running_loop()
        prewarm = loop.create_future()
        futures = booking_round.submit_bookings(loop, {'u1': [listing]}, [(user, object())],
                                                {}, {'u1': prewarm}, {}, float('inf'), storage)
        assert (await futures[0][3]).success
        prewarm.cancel()
    try:
        asyncio.run(run())
        assert chosen == ['u1:booking']
    finally:
        storage.close()


def test_session_is_invalid_if_process_was_closed_concurrently():
    class ClosedProcess:
        def is_alive(self):
            raise ValueError('process object is closed')
    session = RemoteSession(SimpleNamespace(generation=1, process=ClosedProcess()),
                            'a@example.test', 1, 10**12)
    assert not session.is_alive()
