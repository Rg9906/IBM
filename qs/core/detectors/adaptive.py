"""D2 adaptive statistical detector: CUSUM against each segment's own EWMA baseline (blueprint 11.1)."""
from __future__ import annotations

import math

from qs.config import DetectorConfig
from qs.schema import Alert


class AdaptiveCusum:
    """One-sided CUSUM on the baseline z-scores of timeout_ratio and latency_ratio.

    S_t = max(0, S_{t-1} + z_t - k). An alert is raised when either statistic reaches h. The statistic
    stays elevated until it returns to zero, so one episode gives one alert. Only eligible rows count;
    a NaN z-score is no information and leaves the statistic unchanged. The latency z-score is taken on
    latency_ratio (p95 over its baseline), which is already a ratio, so no extra log is applied.
    """

    name = "D2"
    FEATURES = ("timeout_ratio", "latency_ratio")

    def __init__(self, cfg: DetectorConfig) -> None:
        self.h = cfg.cusum_h
        self.k = cfg.d2_k
        self._s: dict = {}
        self.active: set = set()

    def update(self, now_ms: int, rows: list) -> list:
        out = []
        for r in rows:
            if not r.eligible:
                continue
            s = self._s.setdefault(r.segment, {f: 0.0 for f in self.FEATURES})
            hit = None
            for f in self.FEATURES:
                z = r.z[f]
                if z is None or math.isnan(z):
                    continue
                s[f] = min(max(0.0, s[f] + z - self.k), 4 * self.h)
                if s[f] >= self.h and hit is None:
                    hit = f
            if all(v <= 0.0 for v in s.values()):
                self.active.discard(r.segment)
            if hit and r.segment not in self.active:
                self.active.add(r.segment)
                out.append(Alert(ts=now_ms, detector=self.name, segment=r.segment, score=s[hit],
                                 reasons=[f"{'TIMEOUT_UP' if hit == 'timeout_ratio' else 'LATENCY_UP'} "
                                          f"cusum={s[hit]:.1f} >= {self.h:.1f}"]))
        return out
