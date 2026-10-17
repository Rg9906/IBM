"""Per-segment, per-feature EWMA baselines with a freeze rule (blueprint 10.3)."""
from __future__ import annotations

from typing import Optional

import numpy as np

# Minimum spread per feature so a zero-variance baseline cannot give infinite z-scores.
SPREAD_FLOOR = {
    "arrival_rate": 1.0, "fresh_rate": 1.0, "raf": 0.02, "success_ratio": 0.01, "timeout_ratio": 0.005,
    "reject_ratio": 0.005, "p50": 20.0, "p95": 40.0, "p99": 60.0, "latency_ratio": 0.05, "pending_count": 1.0,
    "pending_frac": 0.005, "queue_depth": 1.0, "queue_growth": 0.1, "utilisation": 0.02, "queue_delay_est": 0.02,
    "orbit_proxy": 1.0, "status_check_rate": 0.1, "load_over_capacity": 0.02, "expired_ratio": 0.005,
}


class Baselines:
    def __init__(self, features: list, n_segments: int, alpha: float = 0.02, warm_ticks: int = 24,
                 z_clip: float = 6.0, prior_weight: int = 3) -> None:
        self.features = list(features)
        self.alpha = alpha
        self.warm_ticks = warm_ticks
        self.z_clip = z_clip
        self.prior_weight = prior_weight
        shape = (n_segments, len(self.features))
        self.mean = np.zeros(shape)
        self.spread = np.zeros(shape)
        self.n = np.zeros(shape, dtype=np.int64)
        self.ticks = np.zeros(n_segments, dtype=np.int64)     # ticks with data, per segment
        self.frozen = np.zeros(n_segments, dtype=bool)
        self.floor = np.array([SPREAD_FLOOR.get(f, 0.01) for f in self.features])

    def col(self, name: str) -> int:
        return self.features.index(name)

    def warming_up(self) -> np.ndarray:
        return self.ticks < self.warm_ticks

    def load_priors(self, priors: dict, seg_index: dict) -> None:
        """Training-set priors {segment_key: {feature: (mean, spread)}} for segments with sparse warm-up data."""
        for key, feats in priors.items():
            s = seg_index[tuple(key)]
            for f, (m, sp) in feats.items():
                j = self.col(f)
                if self.n[s, j] == 0:
                    self.mean[s, j], self.spread[s, j], self.n[s, j] = m, sp, self.prior_weight

    def z(self, x: np.ndarray) -> np.ndarray:
        """Clipped deviation score against the current baseline; NaN where x or the baseline is missing."""
        with np.errstate(invalid="ignore", divide="ignore"):
            z = (x - self.mean) / np.maximum(self.spread, self.floor)
        z = np.clip(z, -self.z_clip, self.z_clip)
        z[(self.n == 0) | np.isnan(x)] = np.nan
        return z

    def update(self, x: np.ndarray, update_rows: np.ndarray) -> None:
        """Fold this tick into the baselines for rows in update_rows (not frozen, with data)."""
        rows = update_rows & ~self.frozen
        ok = rows[:, None] & ~np.isnan(x)
        self.n[ok] += 1
        a = np.maximum(self.alpha, 1.0 / np.maximum(self.n, 1))   # cumulative mean while warming up
        dev = np.where(self.n <= 1, 0.0, np.abs(np.where(ok, x, 0.0) - self.mean))   # first sample has no deviation
        self.mean = np.where(ok, self.mean + a * (np.where(ok, x, 0.0) - self.mean), self.mean)
        self.spread = np.where(ok, self.spread + a * (dev - self.spread), self.spread)
        self.ticks[rows] += 1

    def set_frozen(self, rows: Optional[np.ndarray]) -> None:
        self.frozen = np.zeros_like(self.frozen) if rows is None else rows.astype(bool)
