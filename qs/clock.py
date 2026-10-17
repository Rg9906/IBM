"""Time source. Only this module (and benchmark code) may read the wall clock (blueprint 19.6)."""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SimClock:
    """Simulator clock in milliseconds. Advances only when told to; never moves backwards."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = int(start_ms)

    def now_ms(self) -> int:
        return self._now

    def advance_to(self, t_ms: int) -> int:
        t_ms = int(t_ms)
        if t_ms < self._now:
            raise ValueError(f"clock cannot move backwards: {t_ms} < {self._now}")
        self._now = t_ms
        return self._now

    def advance_by(self, dt_ms: int) -> int:
        if dt_ms < 0:
            raise ValueError("dt_ms must be >= 0")
        return self.advance_to(self._now + int(dt_ms))


class WallClock:
    """Monotonic wall clock scaled by a speed factor; reports ms since construction."""

    def __init__(self, speed: float = 1.0) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self._speed = float(speed)
        self._t0 = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000.0 * self._speed)


def utc_iso() -> str:
    """UTC timestamp for registry records (wall time is only read in this module)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
