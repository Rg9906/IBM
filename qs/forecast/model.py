"""CascadeForecaster inference (blueprint 13.6, 13.10). No ground truth is used here."""
from __future__ import annotations

import json
import pickle
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
from threadpoolctl import threadpool_limits

from qs.forecast.dataset import DELTA_TICKS, EST_SERVICE_S, FEATURES
from qs.forecast.grid import StuckGrid
from qs.schema import ForecastState

EMA = 0.5
WATCH, CRITICAL = 0.3, 0.5
# The training data holds only ticks after a trigger starts, so healthy states are out of domain. Below these
# levels there is no congestion to forecast from and p_stuck is reported as 0.
CALM_DELAY, CALM_Q, CALM_TIMEOUT = 0.1, 0.02, 0.02


def state_of(p: float, watch: float = WATCH, critical: float = CRITICAL) -> ForecastState:
    return ForecastState.CRITICAL if p >= critical else ForecastState.WATCH if p >= watch else ForecastState.CALM


class CascadeForecaster:
    def __init__(self, client_timeout_s: float = 3.0, qmax: float = 3000.0, workers: int = 40) -> None:
        self.timeout_s, self.qmax, self.cap = client_timeout_s, qmax, workers / EST_SERVICE_S
        self.grid: Optional[StuckGrid] = None
        self.ensemble = None
        self.watch, self.critical = WATCH, CRITICAL
        self._ema: dict = {}
        self._hist: dict = {}
        self._delay: dict = {}

    def load(self, path: str) -> None:
        d = Path(path)
        meta = json.loads((d / "forecaster.json").read_text())
        self.grid = StuckGrid.from_dict(meta["grid"])
        self.watch, self.critical = meta.get("watch", WATCH), meta.get("critical", CRITICAL)
        if meta.get("use") == "ensemble" and (d / "ensemble.pkl").exists():
            with open(d / "ensemble.pkl", "rb") as fh:
                self.ensemble = pickle.load(fh)

    @staticmethod
    def _nz(x) -> float:
        return 0.0 if x is None or x != x else float(x)

    def phase_point(self, row) -> tuple:
        f = row.f
        return (min(self._nz(f["queue_delay_est"]) / self.timeout_s, 5.0), self._nz(f["orbit_proxy"]) / self.cap)

    def _vector(self, row) -> np.ndarray:
        f, seg = row.f, row.segment
        q, orb = self._nz(f["queue_depth"]) / self.qmax, self._nz(f["orbit_proxy"]) / self.cap
        h = self._hist.setdefault(seg, deque(maxlen=DELTA_TICKS + 1))
        h.append((q, orb))
        pq, po = h[0]
        delay, _ = self.phase_point(row)
        return np.array([q, delay, orb, self._nz(f["load_over_capacity"]), self._nz(f["raf"]),
                         self._nz(f["timeout_ratio"]), q - pq, orb - po, self._nz(f["expired_ratio"])])

    def predict(self, rows: list) -> dict:
        """Gateway segment -> smoothed p_stuck. Skips non-gateway and ineligible rows."""
        out = {}
        for r in rows:
            if r.segment[0] != "gateway" or self.grid is None:
                continue
            v = self._vector(r)
            if v[1] < CALM_DELAY and v[0] < CALM_Q and v[5] < CALM_TIMEOUT:
                p = 0.0
            elif self.ensemble is not None:
                with threadpool_limits(limits=1):          # one row: thread start-up costs more than the work
                    p = float(self.ensemble.predict_proba(v[None, :])[0, 1])
            else:
                p = float(self.grid.predict(v[FEATURES.index("delay_frac")], v[FEATURES.index("orbit_frac")])[0])
            e = self._ema.get(r.segment)
            e = p if e is None else EMA * p + (1 - EMA) * e
            self._ema[r.segment] = e
            dh = self._delay.setdefault(r.segment, deque(maxlen=4))
            dh.append((r.tick_ts, v[1]))
            out[r.segment] = e
        return out

    def state(self, p: float) -> ForecastState:
        return state_of(p, self.watch, self.critical)

    def countdown_s(self, segment: tuple) -> Optional[float]:
        """Rough linear extrapolation of delay_frac over the last four ticks to 1.0. Not a model output."""
        h = self._delay.get(segment)
        if not h or len(h) < 4:
            return None
        t = np.array([x[0] for x in h], float) / 1000.0
        d = np.array([x[1] for x in h], float)
        slope = np.polyfit(t, d, 1)[0]
        if d[-1] >= 1.0:
            return 0.0
        return float((1.0 - d[-1]) / slope) if slope > 1e-6 else None


class CalibratedEnsemble:
    """HistGradientBoosting plus isotonic calibration fitted on validation (blueprint 13.5)."""

    def __init__(self, clf, iso) -> None:
        self.clf, self.iso = clf, iso

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        p = self.iso.predict(self.clf.predict_proba(X)[:, 1])
        return np.column_stack([1 - p, p])
