"""Per-cell (gateway x bank) bad/expected counts for the Fault Localizer (blueprint 14.2).

bad = timeout or overload reject, or a SUCCESS/DECLINED slower than 1.5 x the cell's baseline p95.
expected = outcomes x the cell's baseline bad rate (floor 0.005). Baselines learn only while the platform is calm.
"""
from __future__ import annotations

from collections import deque

import numpy as np

from qs.core.windows import BANKS, GATEWAYS
from qs.schema import Event, EventType, Status

NG, NB = len(GATEWAYS), len(BANKS)
GI, BI = {g: i for i, g in enumerate(GATEWAYS)}, {b: i for i, b in enumerate(BANKS)}
HARD = (Status.CLIENT_TIMEOUT, Status.REJECTED_OVERLOAD)
OK = (Status.SUCCESS, Status.DECLINED)
SLOW = 1.5
RATE_FLOOR = 0.005


class CellTracker:
    def __init__(self, window_ms: int = 10_000, tick_ms: int = 5000, ewma: float = 0.1, keep: int = 400) -> None:
        self.window_ms, self.tick_ms, self.ewma = window_ms, tick_ms, ewma
        self._ev = [[deque() for _ in range(NB)] for _ in range(NG)]      # (ts, is_hard, is_ok, rt)
        self._lat = [[deque(maxlen=keep) for _ in range(NB)] for _ in range(NG)]
        self.p95 = np.full((NG, NB), np.inf)
        self.rate = np.full((NG, NB), RATE_FLOOR)

    def on_event(self, e: Event, late: bool = False) -> None:
        if e.event_type is not EventType.ATTEMPT_OUTCOME or late:
            return
        self._ev[GI[e.gateway_id]][BI[e.bank_id]].append(
            (e.event_timestamp, e.status in HARD, e.status in OK, float(e.response_time_ms or 0)))

    def _trim(self, now_ms: int) -> None:
        lo = now_ms - self.window_ms
        for row in self._ev:
            for d in row:
                while d and d[0][0] < lo:
                    d.popleft()

    def matrices(self, now_ms: int) -> tuple:
        """(bad, expected) arrays over the short window ending at now_ms."""
        self._trim(now_ms)
        bad, n = np.zeros((NG, NB)), np.zeros((NG, NB))
        for g in range(NG):
            for b in range(NB):
                for ts, hard, ok, rt in self._ev[g][b]:
                    if ts >= now_ms:
                        continue
                    n[g, b] += 1
                    bad[g, b] += hard or (ok and rt > SLOW * self.p95[g, b])
        return bad, n * np.maximum(self.rate, RATE_FLOOR)

    def learn(self, now_ms: int, calm: bool) -> None:
        """Update baselines from the last tick's outcomes, only when nothing is alerting."""
        if not calm:
            return
        lo = now_ms - self.tick_ms
        for g in range(NG):
            for b in range(NB):
                ev = [x for x in self._ev[g][b] if lo <= x[0] < now_ms]
                if not ev:
                    continue
                self._lat[g][b].extend(x[3] for x in ev if x[2])
                if len(self._lat[g][b]) >= 20:
                    self.p95[g, b] = float(np.percentile(self._lat[g][b], 95))
                nb = sum(1 for x in ev if x[1] or (x[2] and x[3] > SLOW * self.p95[g, b]))
                self.rate[g, b] = (1 - self.ewma) * self.rate[g, b] + self.ewma * nb / len(ev)
