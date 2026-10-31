"""Durable protection for a single Plaza account and listing."""
from __future__ import annotations

import os
import uuid

from mcore.booking_guard import BookingGuard, account_key, process_alive


def _listing_key(listing_id):
    value = str(listing_id or '').strip()
    return value[3:] if value.startswith('pz_') else value


class PlazaGuard(BookingGuard):
    def __init__(self, storage):
        super().__init__(storage)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS plaza_attempts (
                account TEXT NOT NULL, listing_id TEXT NOT NULL, attempt TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL, listing TEXT NOT NULL, pid INTEGER NOT NULL,
                state TEXT NOT NULL, mutated INTEGER NOT NULL DEFAULT 0,
                timed_out INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account, listing_id))''')

    def acquire(self, username, user_id, listing_id, listing):
        attempt = uuid.uuid4().hex
        with self.connect() as db:
            cur = db.execute('''INSERT OR IGNORE INTO plaza_attempts
                (account, listing_id, attempt, user_id, listing, pid, state)
                VALUES (?, ?, ?, ?, ?, ?, 'running')''',
                (account_key(username), _listing_key(listing_id), attempt, user_id,
                 listing, os.getpid()))
        return attempt if cur.rowcount else None

    def update(self, attempt, **values):
        allowed = {'state', 'mutated', 'timed_out', 'phase', 'listing'}
        if not values or not values.keys() <= allowed:
            raise ValueError('invalid Plaza journal update')
        with self.connect() as db:
            cur = db.execute('UPDATE plaza_attempts SET ' + ', '.join(k + '=?' for k in values) +
                             ' WHERE attempt=?', (*values.values(), attempt))
            if not cur.rowcount:
                raise RuntimeError('应征保护记录不存在')

    def get_attempt(self, attempt):
        with self.connect() as db:
            row = db.execute('SELECT * FROM plaza_attempts WHERE attempt=?', (attempt,)).fetchone()
            return dict(row) if row else None

    def get_listing(self, username, listing_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM plaza_attempts WHERE account=? AND listing_id=?',
                             (account_key(username), _listing_key(listing_id))).fetchone()
            return dict(row) if row else None

    def get_visible(self, username, user_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT * FROM plaza_attempts
                WHERE user_id=? OR account=? ORDER BY created, attempt''',
                (user_id, account_key(username) if (username or '').strip() else ''))]

    def get_unnotified(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM plaza_attempts WHERE timed_out=0 ORDER BY created, attempt')]

    def claim_notice(self, attempt, *, title, body, user_id):
        # Atomically persist the in-app alert and claim it for external delivery.
        # A crash cannot mark the alert sent without leaving a visible record.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute('UPDATE plaza_attempts SET timed_out=1 WHERE attempt=? AND timed_out=0', (attempt,))
            if not cur.rowcount:
                return False
            db.execute('''INSERT INTO web_notifications (type, title, body, url, user_id)
                VALUES ('booking', ?, ?, ?, ?)''',
                (title, body, f'/users/{user_id}', user_id))
            return True

    def clear_completed(self, attempt):
        with self.connect() as db:
            db.execute("DELETE FROM plaza_attempts WHERE attempt=? AND state='finished' AND timed_out=0", (attempt,))

    def acknowledge(self, attempt, user_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM plaza_attempts WHERE attempt=? AND user_id=?', (attempt, user_id)).fetchone()
            if not row:
                return False
            if row['state'] == 'running' and process_alive(row['pid']):
                raise RuntimeError('后台应征仍在运行，请等待其结束或先停止监控。')
            db.execute('DELETE FROM plaza_attempts WHERE attempt=? AND user_id=?', (attempt, user_id))
            return True
