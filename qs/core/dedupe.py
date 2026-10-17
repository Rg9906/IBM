"""Event-id deduplication with a bounded LRU (blueprint 8.5). SQLite adds a unique index in WP-06."""
from __future__ import annotations

from collections import OrderedDict


class DedupeLRU:
    def __init__(self, capacity: int = 200_000) -> None:
        self.capacity = capacity
        self._seen: OrderedDict = OrderedDict()

    def seen_before(self, event_id: str) -> bool:
        """True if event_id was already seen; otherwise records it and returns False."""
        s = self._seen
        if event_id in s:
            s.move_to_end(event_id)
            return True
        s[event_id] = None
        if len(s) > self.capacity:
            s.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._seen)
