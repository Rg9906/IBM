"""Ingest: validation, deduplication and watermark ordering (blueprint 8.5)."""
from __future__ import annotations

import heapq
from typing import Callable, Iterable, Union

from pydantic import ValidationError

from qs.core.dedupe import DedupeLRU
from qs.schema import Event, IngestResult


class Ingestor:
    """Validates, deduplicates and orders events, then calls each handler as handler(event, late).

    Events are held until the watermark (max timestamp seen minus watermark_ms) passes them, so
    handlers see an ordered stream. An event that arrives after the watermark already passed its
    timestamp is delivered immediately with late=True. release_until()/flush() force delivery.
    """

    def __init__(self, handlers: Iterable[Callable[[Event, bool], None]], dedupe_capacity: int = 200_000,
                 watermark_ms: int = 2000) -> None:
        self.handlers = list(handlers)
        self.dedupe = DedupeLRU(dedupe_capacity)
        self.watermark_ms = watermark_ms
        self.max_ts = -1
        self._heap: list = []
        self._n = 0
        self.metrics = {"accepted": 0, "duplicates": 0, "rejected": 0, "late": 0}

    def _deliver(self, ev: Event, late: bool) -> None:
        for h in self.handlers:
            h(ev, late)

    def ingest(self, items: Iterable[Union[Event, dict]]) -> IngestResult:
        res = IngestResult()
        m = self.metrics
        for it in items:
            try:
                ev = it if isinstance(it, Event) else Event.model_validate(it)
            except (ValidationError, TypeError, ValueError) as exc:
                res.rejected += 1
                m["rejected"] += 1
                if len(res.errors) < 20:
                    res.errors.append(str(exc).splitlines()[0][:200])
                continue
            if self.dedupe.seen_before(ev.event_id):
                res.duplicates += 1
                m["duplicates"] += 1
                continue
            res.accepted += 1
            m["accepted"] += 1
            ts = ev.event_timestamp
            if self.max_ts >= 0 and ts < self.max_ts - self.watermark_ms:
                m["late"] += 1
                self._deliver(ev, True)
                continue
            if ts > self.max_ts:
                self.max_ts = ts
            self._n += 1
            heapq.heappush(self._heap, (ts, self._n, ev))
        self.release_until(self.max_ts - self.watermark_ms)
        return res

    def release_until(self, ts_ms: int) -> int:
        """Deliver buffered events with timestamp <= ts_ms, in timestamp order."""
        h, n = self._heap, 0
        while h and h[0][0] <= ts_ms:
            _, _, ev = heapq.heappop(h)
            self._deliver(ev, False)
            n += 1
        return n

    def flush(self) -> int:
        return self.release_until(1 << 62)

    def pending(self) -> int:
        return len(self._heap)
