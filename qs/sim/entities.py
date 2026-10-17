"""Simulator entities. Ground truth (commits, true attempt states) lives here and never in events."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

QUEUED, SERVICE, ENDED, UNRESOLVED = 0, 1, 2, 3


class Attempt:
    __slots__ = ("pay", "k", "gw", "t_send", "t_enq", "seq", "state", "committed", "timed_out",
                 "client_done", "is_status", "blackholed", "resolve_ms")

    def __init__(self, pay: "Payment", k: int, gw: int, t_send: int, is_status: bool = False) -> None:
        self.pay = pay
        self.k = k
        self.gw = gw
        self.t_send = t_send
        self.t_enq = t_send
        self.seq = 0
        self.state = QUEUED
        self.committed = False
        self.timed_out = False
        self.client_done = False
        self.is_status = is_status
        self.blackholed = False
        self.resolve_ms = 0


class Payment:
    __slots__ = ("n", "pid", "bank", "profile", "row", "attempts", "commits", "done", "abandoned",
                 "awaiting", "checks", "ik", "last_gw", "cap", "t_start")

    def __init__(self, n: int, pid: str, bank: int, profile: str, row: dict, ik: Optional[str],
                 cap: int, t_start: int) -> None:
        self.n = n
        self.pid = pid
        self.bank = bank
        self.profile = profile
        self.row = row
        self.attempts: list[Attempt] = []
        self.commits = 0
        self.done = False          # client learned success or a hard decline
        self.abandoned = False
        self.awaiting: Optional[str] = None   # None | "verify" | "self" | "platform"
        self.checks = 0
        self.ik = ik
        self.last_gw = 0
        self.cap = cap
        self.t_start = t_start


class Gateway:
    def __init__(self, idx: int, workers: int, qmax: int) -> None:
        self.idx = idx
        self.workers = workers
        self.qmax = qmax
        self.busy = 0
        self.q_fresh: deque = deque()
        self.q_retry: deque = deque()
        # time-integrated statistics (exact, for the Little's law and utilisation tests)
        self.last_t = 0
        self.q_int = 0.0
        self.busy_int = 0.0
        self.wait_sum = 0.0
        self.entered = 0

    def qlen(self) -> int:
        return len(self.q_fresh) + len(self.q_retry)

    def touch(self, now: int) -> None:
        dt = now - self.last_t
        if dt > 0:
            self.q_int += self.qlen() * dt
            self.busy_int += self.busy * dt
            self.last_t = now


@dataclass
class Modifier:
    """A scheduled fault. Value ramps linearly from v0 at start_ms to v1 at end_ms.

    kind: capacity (gateway), gw_service (gateway), bank_latency (bank),
          route_latency (gateway and bank), blackhole (bank; v0 = fraction, resolve_ms).
    """
    kind: str
    start_ms: int
    end_ms: int
    v0: float = 1.0
    v1: Optional[float] = None
    gateway: Optional[str] = None
    bank: Optional[str] = None
    resolve_ms: int = 0

    def value(self, t: int) -> float:
        v1 = self.v0 if self.v1 is None else self.v1
        if self.end_ms <= self.start_ms:
            return self.v0
        f = (t - self.start_ms) / (self.end_ms - self.start_ms)
        f = 0.0 if f < 0 else 1.0 if f > 1 else f
        return self.v0 + (v1 - self.v0) * f

    def active(self, t: int) -> bool:
        return self.start_ms <= t < self.end_ms
