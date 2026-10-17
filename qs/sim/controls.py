"""Server-side controls that approved actions switch on (blueprint 9.4)."""
from __future__ import annotations

from typing import Optional


def scope_match(scope: tuple, gw: str, bank: str) -> bool:
    _, sg, sb = scope
    return (sg in ("*", gw)) and (sb in ("*", bank))


class Throttle:
    __slots__ = ("scope", "rate_per_s", "retry_after_ms", "tokens", "last_t", "burst")

    def __init__(self, scope: tuple, rate_per_s: float, retry_after_ms: int, now: int) -> None:
        self.scope = scope
        self.rate_per_s = rate_per_s
        self.retry_after_ms = retry_after_ms
        self.burst = max(2.0, 2.0 * rate_per_s)
        self.tokens = self.burst
        self.last_t = now

    def take(self, now: int) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.last_t) * self.rate_per_s / 1000.0)
        self.last_t = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Controls:
    def __init__(self) -> None:
        self.throttles: dict = {}          # scope -> Throttle
        self.shed: set = set()             # gateway names
        self.fresh_first: set = set()      # gateway names
        self.governed: set = set()         # scopes with the Verification Governor enabled

    def check_retry(self, gw: str, bank: str, now: int) -> Optional[int]:
        """None if the retry may proceed, else the Retry-After in ms."""
        for th in self.throttles.values():
            if scope_match(th.scope, gw, bank) and not th.take(now):
                return th.retry_after_ms
        return None

    def is_governed(self, gw: str, bank: str) -> bool:
        return any(scope_match(s, gw, bank) for s in self.governed)
