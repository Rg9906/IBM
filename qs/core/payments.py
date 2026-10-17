"""Platform-view payment state machine (blueprint 8.3), derived only from events."""
from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from qs.core.windows import N_SEGMENTS, event_segments
from qs.schema import BANKS, GATEWAYS, Event, EventType, PaymentState as S, Status

_PENDING = (S.PENDING_UNKNOWN, S.STATUS_CHECKING)
_TERMINAL = (S.CONFIRMED_SUCCESS, S.ESCALATED_RECONCILE, S.ABANDONED)


class PaymentRecord:
    __slots__ = ("state", "attempts", "status_checks", "updated_ts", "gw", "bank", "orbit_counted")

    def __init__(self) -> None:
        self.state = S.INITIATED
        self.attempts = 0
        self.status_checks = 0
        self.updated_ts = -1
        self.gw = 0
        self.bank = 0
        self.orbit_counted = False


class PaymentTracker:
    def __init__(self, orbit_window_ms: int = 15000, retention_ms: int = 300_000) -> None:
        self.records: dict = {}
        self.pending = np.zeros(N_SEGMENTS)
        self._timeouts: deque = deque()   # (ts, pid, segments)
        self.orbit_window_ms = orbit_window_ms
        self.retention_ms = retention_ms
        self.metrics = {"retry_while_unknown": 0, "stale_events": 0, "attempt_after_terminal": 0}

    # -- helpers
    def state(self, payment_id: str) -> Optional[S]:
        r = self.records.get(payment_id)
        return r.state if r else None

    def _set(self, r: PaymentRecord, new: S) -> None:
        if (r.state in _PENDING) != (new in _PENDING):
            delta = 1.0 if new in _PENDING else -1.0
            for s in event_segments(r.gw, r.bank):
                self.pending[s] += delta
        r.state = new

    # -- event application
    def apply(self, ev: Event, late: bool = False) -> None:
        et = ev.event_type
        if et not in (EventType.PAYMENT_INITIATED, EventType.ATTEMPT_SENT, EventType.ATTEMPT_OUTCOME,
                      EventType.STATUS_CHECK_SENT, EventType.STATUS_CHECK_RESULT):
            return
        pid = ev.payment_id
        r = self.records.get(pid)
        if r is None:
            r = self.records[pid] = PaymentRecord()
            r.gw = GATEWAYS.index(ev.gateway_id)
            r.bank = BANKS.index(ev.bank_id)
        ts = ev.event_timestamp
        if ts < r.updated_ts:
            self.metrics["stale_events"] += 1
            return
        r.updated_ts = ts
        if et is EventType.PAYMENT_INITIATED:
            return
        if et is EventType.ATTEMPT_SENT:
            if r.state in _TERMINAL:
                self.metrics["attempt_after_terminal"] += 1
                return
            if r.state in _PENDING:
                self.metrics["retry_while_unknown"] += 1
            self._set(r, S.IN_FLIGHT)   # leave pending first so the old segment is decremented
            r.gw, r.bank = GATEWAYS.index(ev.gateway_id), BANKS.index(ev.bank_id)
            r.attempts += 1
            return
        if r.state in (S.CONFIRMED_SUCCESS, S.ESCALATED_RECONCILE):
            return
        if et is EventType.ATTEMPT_OUTCOME:
            st = ev.status
            if st is Status.SUCCESS:
                self._set(r, S.CONFIRMED_SUCCESS)
            elif st in (Status.DECLINED, Status.REJECTED_OVERLOAD):
                self._set(r, S.CONFIRMED_FAILED)
            elif st is Status.CLIENT_TIMEOUT:
                self._set(r, S.PENDING_UNKNOWN)
                self._timeouts.append((ts, pid, event_segments(GATEWAYS.index(ev.gateway_id), BANKS.index(ev.bank_id))))
        elif et is EventType.STATUS_CHECK_SENT:
            r.status_checks += 1
            if r.state is S.PENDING_UNKNOWN:
                self._set(r, S.STATUS_CHECKING)
        elif et is EventType.STATUS_CHECK_RESULT:
            st = ev.status
            if st is Status.CONFIRMED_SUCCESS:
                self._set(r, S.CONFIRMED_SUCCESS)
            elif st is Status.CONFIRMED_FAILED:
                self._set(r, S.CONFIRMED_FAILED)
            elif r.state is S.STATUS_CHECKING:
                self._set(r, S.PENDING_UNKNOWN)

    def escalate(self, payment_id: str, ts: int) -> bool:
        """Governor hook: check budget exhausted while still unknown (blueprint 12.3 step 5)."""
        r = self.records.get(payment_id)
        if r is None or r.state not in _PENDING:
            return False
        self._set(r, S.ESCALATED_RECONCILE)
        r.updated_ts = max(r.updated_ts, ts)
        return True

    # -- tick-time queries
    def pending_by_segment(self) -> np.ndarray:
        return self.pending.copy()

    def orbit_by_segment(self, now_ms: int) -> np.ndarray:
        """Payments with a timeout in the last window that are not yet confirmed successful."""
        out = np.zeros(N_SEGMENTS)
        tq = self._timeouts
        cutoff = now_ms - self.orbit_window_ms
        while tq and tq[0][0] < cutoff:
            tq.popleft()
        seen = set()
        for ts, pid, segs in tq:
            if ts >= now_ms or pid in seen:
                continue
            seen.add(pid)
            if self.records[pid].state is not S.CONFIRMED_SUCCESS:
                for s in segs:
                    out[s] += 1
        return out

    def prune(self, now_ms: int) -> int:
        """Drop settled payments older than the retention period."""
        dead = [p for p, r in self.records.items()
                if r.state in (S.CONFIRMED_SUCCESS, S.CONFIRMED_FAILED, S.ABANDONED)
                and now_ms - r.updated_ts > self.retention_ms]
        for p in dead:
            del self.records[p]
        return len(dead)
