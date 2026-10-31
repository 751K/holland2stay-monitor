"""Isolated per-user browser for H2S warmup, prelogin and booking."""
from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace

from mcore.browser_worker import BrowserWorker, _PipeLogHandler, _exception_chain, _restore_exception

logger = logging.getLogger('monitor')


def _serve_booking(conn, source):
    if os.name == 'posix':
        os.setsid()
    send_lock = threading.Lock()
    handler = _PipeLogHandler(conn, send_lock)
    handler.setFormatter(logging.Formatter('%(message)s'))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    import config
    from booker import create_prewarmed_session
    from mcore.warm_browser import warm_lane
    from mcore.booking import book_with_fallback
    from mcore.booking_guard import BookingGuard, attempt_context

    def proxy_from_parent(source_name='', *, rotating=False):
        with send_lock:
            conn.send(('proxy', source_name, rotating))
        kind, value = conn.recv()
        if kind != 'proxy_result':
            raise RuntimeError('主进程代理选择失败')
        return value

    config.get_proxy_url = proxy_from_parent
    session = None
    with send_lock:
        conn.send(('ready',))
    while True:
        try:
            request = conn.recv()
        except EOFError:
            break
        action, payload, env = request
        os.environ.clear()
        os.environ.update(env)
        try:
            if action == 'heartbeat':
                borrowed_fetcher = (session.fetcher if session and
                                    not session.owns_fetcher else None)
                warm_lane.heartbeat(**payload)
                result = bool(borrowed_fetcher is not None and
                              warm_lane.fetcher is not borrowed_fetcher)
                if result:
                    session = None
            elif action == 'prewarm':
                if session:
                    session.close_if_owned()
                shared = None
                if payload['may_borrow'] and warm_lane.fetcher is not None and warm_lane.fetcher.is_alive():
                    shared = (warm_lane.fetcher, warm_lane.lock)
                session = create_prewarmed_session(payload['email'], payload['password'], shared=shared)
                result = (session.email, session.created_at, session.token_expiry)
            elif action == 'book':
                user = payload['user']
                prewarmed = session if (payload['use_prewarm'] and session
                                        and session.email == user.auto_book.email) else None
                if payload['guard']:
                    path, attempt = payload['guard']
                    guard = BookingGuard.from_path(path)
                    with attempt_context(guard, attempt):
                        result = book_with_fallback(payload['candidates'], user, payload['deadline'], prewarmed=prewarmed)
                else:
                    result = book_with_fallback(payload['candidates'], user, payload['deadline'], prewarmed=prewarmed)
            else:
                raise ValueError('未知预订工作进程请求')
            response = ('result', result, None)
        except Exception as exc:
            response = ('error', _exception_chain(exc), None)
        try:
            with send_lock:
                conn.send(response)
        except (BrokenPipeError, EOFError, OSError):
            break
    if session:
        session.close_if_owned()
    warm_lane.close(reason='booking worker exit')
    conn.close()


class RemoteSession:
    def __init__(self, worker, email, created_at, token_expiry):
        self.worker, self.email = worker, email
        self.created_at, self.token_expiry = created_at, token_expiry
        self.generation = worker.generation
        self.owns_fetcher = True

    def is_alive(self):
        process = self.worker.process
        try:
            return (self.generation == self.worker.generation and process is not None
                    and process.is_alive())
        except (ValueError, AssertionError):
            # Another thread may have just reaped and closed this process.
            return False

    def close_if_owned(self):
        # Browser belongs to its worker and can be reused; lifecycle is managed there.
        pass


class BookingWorker(BrowserWorker):
    def __init__(self, user_id, *, entrypoint=_serve_booking):
        super().__init__('booking:' + user_id, entrypoint=entrypoint)
        self._operation_lock = threading.Lock()
        self.generation = 0

    def _start(self):
        super()._start()
        self.generation += 1

    def run(self, action, payload, *, timeout, guard=None, job=None):
        from config import get_proxy_url
        deadline = time.monotonic() + timeout
        if action == 'heartbeat':
            acquired = self._operation_lock.acquire(blocking=False)
            if not acquired:
                return None
        else:
            acquired = self._operation_lock.acquire(timeout=max(0, timeout))
        if not acquired:
            raise TimeoutError('等待预订浏览器进程超时')
        try:
            if self.process is not None and not self.process.is_alive():
                self.stop()
            if self.process is None:
                self._start()
            conn = self.conn
            if not self._ready:
                self._receive(conn, deadline, get_proxy_url, expect='ready')
                self._ready = True
            if guard:
                guard[0].update(guard[1], child_pid=self.process.pid)
            if job is not None:
                job.safe = False
            conn.send((action, payload, dict(os.environ)))
            kind, result, _ = self._receive(conn, deadline, get_proxy_url, expect='result')
            if job is not None:
                job.safe = True
            if kind == 'error':
                raise _restore_exception(result)
            if action == 'heartbeat' and result:
                self.generation += 1
            return result
        except (TimeoutError, EOFError, BrokenPipeError, OSError):
            # If this raises, leave job.safe=False and keep the child PID in the
            # durable guard. A user cannot acknowledge while that process lives.
            self.stop()
            if job is not None:
                job.safe = True
            raise
        finally:
            self._operation_lock.release()


_workers = {}
_workers_lock = threading.Lock()


def get_booking_worker(user_id):
    with _workers_lock:
        worker = _workers.get(user_id)
        if worker is None:
            worker = _workers[user_id] = BookingWorker(user_id)
        return worker


def existing_booking_worker(user_id):
    with _workers_lock:
        return _workers.get(user_id)


def prune_booking_workers(active_user_ids):
    allowed = set(active_user_ids) | {uid + ':booking' for uid in active_user_ids}
    with _workers_lock:
        stale = [(uid, worker) for uid, worker in _workers.items() if uid not in allowed]
    for uid, worker in stale:
        if not worker._operation_lock.acquire(blocking=False):
            continue
        try:
            worker.close()
            with _workers_lock:
                if _workers.get(uid) is worker:
                    del _workers[uid]
        except Exception:
            logger.exception('无法回收停用用户的预订浏览器 user_id=%s', uid)
        finally:
            worker._operation_lock.release()


def close_booking_workers():
    with _workers_lock:
        workers = list(_workers.items())
    for uid, worker in workers:
        try:
            worker.close()
        except Exception:
            logger.exception('无法回收预订浏览器 user_id=%s', uid)
        else:
            with _workers_lock:
                if _workers.get(uid) is worker:
                    del _workers[uid]
