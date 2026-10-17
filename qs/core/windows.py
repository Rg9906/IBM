"""1 s bucket ring buffers per segment, plus a log-spaced latency histogram (blueprint 10.1).

Segment order: 0 platform, 1-3 gateways, 4-8 banks, 9-23 routes (route = 9 + g * 5 + b).
A window of W seconds ending at tick time T covers events with timestamps in [T - W s, T).
"""
from __future__ import annotations

import math

import numpy as np

from qs.schema import BANKS, GATEWAYS, Event, EventType, Level, Status, segment

N_GW, N_BANK = len(GATEWAYS), len(BANKS)
N_SEGMENTS = 1 + N_GW + N_BANK + N_GW * N_BANK

SEGMENT_KEYS: list = ([segment(Level.PLATFORM)]
                      + [segment(Level.GATEWAY, g) for g in GATEWAYS]
                      + [segment(Level.BANK, "*", b) for b in BANKS]
                      + [segment(Level.ROUTE, g, b) for g in GATEWAYS for b in BANKS])
SEGMENT_INDEX = {k: i for i, k in enumerate(SEGMENT_KEYS)}
ROUTE0 = 1 + N_GW + N_BANK

_EVENT_SEGS = [[(0, 1 + g, 1 + N_GW + b, ROUTE0 + g * N_BANK + b) for b in range(N_BANK)] for g in range(N_GW)]


def event_segments(gw: int, bank: int) -> tuple:
    return _EVENT_SEGS[gw][bank]


# counters
SENT, FRESH, RETRY, OUTCOME, SUCCESS, DECLINED, TIMEOUT, REJECT, STATUS = range(9)
N_COUNTERS = 9

N_BINS = 64
_LAT_LO, _LAT_HI = 10.0, 60000.0
_LOG_SPAN = math.log(_LAT_HI / _LAT_LO)
_EDGES = _LAT_LO * np.exp(_LOG_SPAN * np.arange(N_BINS + 1) / N_BINS)
BIN_MID = np.sqrt(_EDGES[:-1] * _EDGES[1:])


def lat_bin(ms: float) -> int:
    if ms <= _LAT_LO:
        return 0
    b = int(N_BINS * math.log(ms / _LAT_LO) / _LOG_SPAN)
    return N_BINS - 1 if b >= N_BINS else b


class Windows:
    def __init__(self, n_buckets: int = 120) -> None:
        self.nb = n_buckets
        self.counters = np.zeros((N_COUNTERS, N_SEGMENTS, n_buckets), dtype=np.int32)
        self.hist = np.zeros((N_SEGMENTS, n_buckets, N_BINS), dtype=np.int32)
        self.snap = np.zeros((4, N_GW, n_buckets))   # queue, active, configured, count
        self.cur = -1
        self.late_dropped = 0
        self.late_applied = 0
        self.events = 0

    def advance(self, sec: int) -> None:
        """Move the ring head to `sec`, clearing every bucket that becomes current."""
        if sec <= self.cur:
            return
        start = max(self.cur + 1, sec - self.nb + 1)
        for s in range(start, sec + 1):
            b = s % self.nb
            self.counters[:, :, b] = 0
            self.hist[:, b, :] = 0
            self.snap[:, :, b] = 0
        self.cur = sec

    def add(self, ev: Event, late: bool = False) -> None:
        sec = ev.event_timestamp // 1000
        if sec > self.cur:
            self.advance(sec)
        elif self.cur - sec >= self.nb:
            self.late_dropped += 1
            return
        elif late:
            self.late_applied += 1
        b = sec % self.nb
        et = ev.event_type
        gw = GATEWAYS.index(ev.gateway_id)
        if et is EventType.SERVICE_SNAPSHOT:
            s = self.snap
            s[0, gw, b] += ev.queue_depth or 0
            s[1, gw, b] += ev.active_requests or 0
            s[2, gw, b] += ev.configured_capacity or 0
            s[3, gw, b] += 1
            return
        if et not in (EventType.ATTEMPT_SENT, EventType.ATTEMPT_OUTCOME, EventType.STATUS_CHECK_SENT):
            return
        segs = _EVENT_SEGS[gw][BANKS.index(ev.bank_id)]
        c = self.counters
        if et is EventType.ATTEMPT_SENT:
            kind = RETRY if ev.is_retry else FRESH
            for s in segs:
                c[SENT, s, b] += 1
                c[kind, s, b] += 1
        elif et is EventType.STATUS_CHECK_SENT:
            for s in segs:
                c[STATUS, s, b] += 1
        else:
            st = ev.status
            col = (SUCCESS if st is Status.SUCCESS else DECLINED if st is Status.DECLINED
                   else TIMEOUT if st is Status.CLIENT_TIMEOUT else REJECT if st is Status.REJECTED_OVERLOAD else -1)
            if col < 0:
                return
            for s in segs:
                c[OUTCOME, s, b] += 1
                c[col, s, b] += 1
            if not late and ev.response_time_ms is not None and col in (SUCCESS, DECLINED, TIMEOUT):
                lb = lat_bin(ev.response_time_ms)
                for s in segs:
                    self.hist[s, b, lb] += 1
        self.events += 1

    # -- window reads (call advance(T // 1000) first at tick time)
    def idx(self, end_sec: int, w_s: int) -> np.ndarray:
        return np.arange(end_sec - w_s, end_sec) % self.nb

    def sum(self, counter: int, end_sec: int, w_s: int) -> np.ndarray:
        return self.counters[counter][:, self.idx(end_sec, w_s)].sum(axis=1).astype(float)

    def hist_sum(self, end_sec: int, w_s: int) -> np.ndarray:
        return self.hist[:, self.idx(end_sec, w_s), :].sum(axis=1)

    def snap_series(self, end_sec: int, w_s: int) -> np.ndarray:
        """Per-bucket mean snapshot values, shape (3 fields, gateways, w_s); NaN where no snapshot."""
        ix = self.idx(end_sec, w_s)
        cnt = self.snap[3][:, ix]
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(cnt > 0, self.snap[:3][:, :, ix] / np.where(cnt > 0, cnt, 1), np.nan)


def quantiles(h: np.ndarray, qs: tuple, min_samples: int) -> list:
    """Quantile estimates (geometric bin midpoints, ms) per segment; NaN below min_samples."""
    cum = np.cumsum(h, axis=1)
    total = cum[:, -1]
    out = []
    for q in qs:
        k = (cum >= q * total[:, None]).argmax(axis=1)
        v = BIN_MID[k].astype(float)
        v[total < min_samples] = np.nan
        out.append(v)
    return out
