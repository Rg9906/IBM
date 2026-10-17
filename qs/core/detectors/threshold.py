"""D0 platform fixed threshold and D1 segment fixed thresholds (blueprint 11.1)."""
from __future__ import annotations

import math

from qs.config import DetectorConfig
from qs.schema import Alert, FeatureRow, Level


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


class PlatformThreshold:
    """D0: alert when platform success_ratio (60 s) is below theta0 for 2 consecutive ticks."""

    name = "D0"

    def __init__(self, cfg: DetectorConfig, consecutive: int = 2) -> None:
        self.theta0 = cfg.theta0
        self.consecutive = consecutive
        self._run = 0
        self.active = False

    def update(self, now_ms: int, rows: list) -> list:
        row = next(r for r in rows if r.segment[0] == Level.PLATFORM.value)
        sr = row.f["success_ratio"]
        if not _nan(sr) and sr < self.theta0:
            self._run += 1
        else:
            self._run = 0
            self.active = False
        if self._run >= self.consecutive and not self.active:
            self.active = True
            return [Alert(ts=now_ms, detector=self.name, segment=row.segment, kind="platform", score=1.0 - sr,
                          reasons=[f"SUCCESS_LOW {sr:.3f} < {self.theta0:.3f}"])]
        return []


class SegmentThresholds:
    """D1: per segment, timeout_ratio above a or success_ratio below b for 2 consecutive ticks.

    One sensitivity knob s scales both: a = a0 / s and b = 1 - fail0 / s (higher s alerts earlier).
    """

    name = "D1"

    def __init__(self, cfg: DetectorConfig) -> None:
        self.a = cfg.d1_timeout_a / cfg.sensitivity
        self.b = 1.0 - cfg.d1_fail_b / cfg.sensitivity
        self.consecutive = cfg.d1_consecutive
        self._run: dict = {}
        self.active: set = set()

    def update(self, now_ms: int, rows: list) -> list:
        out = []
        for r in rows:
            if r.segment[0] == Level.PLATFORM.value or "low_volume" in r.flags or "no_data" in r.flags:
                continue
            to, sr = r.f["timeout_ratio"], r.f["success_ratio"]
            hit = (not _nan(to) and to > self.a) or (not _nan(sr) and sr < self.b)
            run = self._run.get(r.segment, 0) + 1 if hit else 0
            self._run[r.segment] = run
            if not hit:
                self.active.discard(r.segment)
            elif run >= self.consecutive and r.segment not in self.active:
                self.active.add(r.segment)
                out.append(Alert(ts=now_ms, detector=self.name, segment=r.segment, score=to if not _nan(to) else None,
                                 reasons=[f"TIMEOUT_UP {to:.3f} > {self.a:.3f}" if not _nan(to) and to > self.a
                                          else f"SUCCESS_LOW {sr:.3f} < {self.b:.3f}"]))
        return out
