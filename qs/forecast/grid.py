"""Explainable point-of-no-return grid (blueprint 13.5): 20 x 20 over (delay_frac, orbit_frac)."""
from __future__ import annotations

import numpy as np

N = 20


class StuckGrid:
    def __init__(self, delay_max: float = 5.0, orbit_max: float = 5.0, n: int = N) -> None:
        self.n, self.delay_max, self.orbit_max = n, delay_max, orbit_max
        self.p = np.full((n, n), 0.5)

    def _idx(self, delay: np.ndarray, orbit: np.ndarray) -> tuple:
        i = np.clip((np.asarray(delay) / self.delay_max * self.n).astype(int), 0, self.n - 1)
        j = np.clip((np.asarray(orbit) / self.orbit_max * self.n).astype(int), 0, self.n - 1)
        return i, j

    def fit(self, delay: np.ndarray, orbit: np.ndarray, stuck: np.ndarray, weights=None, monotone: bool = True):
        """Smoothed stuck share per cell with a Beta(1, 1) prior; class weights balance the two classes."""
        stuck = np.asarray(stuck, float)
        w = np.ones_like(stuck) if weights is None else np.asarray(weights, float)
        i, j = self._idx(delay, orbit)
        pos, tot = np.zeros((self.n, self.n)), np.zeros((self.n, self.n))
        np.add.at(pos, (i, j), w * stuck)
        np.add.at(tot, (i, j), w)
        self.p = (pos + 1.0) / (tot + 2.0)
        if monotone:
            # isotonic envelope: running max along both axes keeps p from falling as either axis rises
            self.p = np.maximum.accumulate(np.maximum.accumulate(self.p, axis=0), axis=1)
        return self

    def predict(self, delay, orbit) -> np.ndarray:
        i, j = self._idx(np.atleast_1d(delay), np.atleast_1d(orbit))
        return self.p[i, j]

    def to_dict(self) -> dict:
        return {"n": self.n, "delay_max": self.delay_max, "orbit_max": self.orbit_max, "p": self.p.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "StuckGrid":
        g = cls(d["delay_max"], d["orbit_max"], d["n"])
        g.p = np.array(d["p"])
        return g
