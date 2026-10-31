"""Durable account-level protection for uncertain Holland2Stay bookings."""
from __future__ import annotations

import contextvars
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager

_current = contextvars.ContextVar('booking_attempt', default=None)


def account_key(email):
    return hashlib.sha256(email.strip().casefold().encode()).hexdigest()


class BookingGuard:
    def __init__(self, storage):
        self.path = storage.conn.execute('PRAGMA database_list').fetchone()[2]
        if not self.path:
            raise RuntimeError('持久化数据库不可用')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS booking_attempts (
                account TEXT PRIMARY KEY, attempt TEXT NOT NULL, user_id TEXT NOT NULL,
                listing TEXT NOT NULL, pid INTEGER NOT NULL, state TEXT NOT NULL,
                child_pid INTEGER NOT NULL DEFAULT 0,
                mutated INTEGER NOT NULL DEFAULT 0, timed_out INTEGER NOT NULL DEFAULT 0,
                cart TEXT NOT NULL DEFAULT '', order_number TEXT NOT NULL DEFAULT '',
                pay_url TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(booking_attempts)')}
            if 'child_pid' not in columns:
                try:
                    db.execute('ALTER TABLE booking_attempts ADD COLUMN child_pid INTEGER NOT NULL DEFAULT 0')
                except sqlite3.OperationalError:
                    if 'child_pid' not in {row['name'] for row in db.execute('PRAGMA table_info(booking_attempts)')}:
                        raise

    @classmethod
    def from_path(cls, path):
        instance = cls.__new__(cls)
        instance.path = path
        return instance

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def acquire(self, email, user_id, listing):
        attempt = uuid.uuid4().hex
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO booking_attempts (account, attempt, user_id, listing, pid, state) VALUES (?, ?, ?, ?, ?, ?)',
                                (account_key(email), attempt, user_id, listing, os.getpid(), 'running'))
        return attempt if result.rowcount else None

    def update(self, attempt, **values):
        allowed = {'state', 'mutated', 'timed_out', 'cart', 'order_number', 'pay_url', 'phase', 'listing', 'pid', 'child_pid'}
        if not values or not values.keys() <= allowed:
            raise ValueError('invalid booking journal update')
        with self.connect() as db:
            result = db.execute('UPDATE booking_attempts SET ' + ', '.join(k + '=?' for k in values) + ' WHERE attempt=?', (*values.values(), attempt))
            if not result.rowcount:
                raise RuntimeError('预订保护记录不存在')

    def claim_notice(self, attempt):
        with self.connect() as db:
            result = db.execute("UPDATE booking_attempts SET timed_out=1 WHERE attempt=? AND timed_out=0", (attempt,))
            return bool(result.rowcount)

    def get_attempt(self, attempt):
        with self.connect() as db:
            row = db.execute('SELECT * FROM booking_attempts WHERE attempt=?', (attempt,)).fetchone()
            return dict(row) if row else None

    def get_account(self, email):
        with self.connect() as db:
            row = db.execute('SELECT * FROM booking_attempts WHERE account=?', (account_key(email),)).fetchone()
            return dict(row) if row else None

    def get_user(self, user_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM booking_attempts WHERE user_id=? ORDER BY created LIMIT 1", (user_id,)).fetchone()
            return dict(row) if row else None

    def clear_completed(self, attempt):
        with self.connect() as db:
            db.execute("DELETE FROM booking_attempts WHERE attempt=? AND state='finished' AND timed_out=0", (attempt,))

    def acknowledge(self, attempt, user_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM booking_attempts WHERE attempt=? AND user_id=?', (attempt, user_id)).fetchone()
            if not row:
                return False
            if row['state'] == 'running' and (process_alive(row['pid']) or
                    (row['child_pid'] and process_alive(row['child_pid']))):
                raise RuntimeError('后台预订仍在运行，请等待其结束或先停止监控。')
            db.execute('DELETE FROM booking_attempts WHERE attempt=? AND user_id=?', (attempt, user_id))
            return True


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


@contextmanager
def attempt_context(guard, attempt):
    token = _current.set((guard, attempt))
    try:
        yield
    finally:
        _current.reset(token)


def record_mutation(listing):
    context = _current.get()
    if context:
        guard, attempt = context
        guard.update(attempt, mutated=1, listing=listing)
        return True
    return False


def record_progress(**values):
    context = _current.get()
    if context:
        context[0].update(context[1], **values)


def mutation_started():
    context = _current.get()
    if not context:
        return False
    row = context[0].get_attempt(context[1])
    return bool(row and row['mutated'])
