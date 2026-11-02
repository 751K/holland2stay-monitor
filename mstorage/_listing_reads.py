"""Per-user listing read state, independent of notification delivery and status."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


class ListingReadOps:
    def read_listing_ids(self, user_id: str) -> set[str]:
        return {row[0] for row in self._conn.execute(
            'SELECT listing_id FROM listing_reads WHERE user_id = ?', (user_id,),
        )}

    def mark_listings_read(self, user_id: str, listing_ids: Iterable[str]) -> int:
        """Store first read time once. Only existing users/listings are accepted."""
        now = datetime.now(timezone.utc).isoformat()
        changed = 0
        with self._conn:
            for listing_id in dict.fromkeys(listing_ids):
                cursor = self._conn.execute(
                    'INSERT OR IGNORE INTO listing_reads (user_id, listing_id, read_at) '
                    'SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM listings WHERE id = ?) '
                    'AND EXISTS (SELECT 1 FROM user_configs WHERE id = ?)',
                    (user_id,listing_id,now,listing_id,user_id),
                )
                changed += cursor.rowcount
        return changed
