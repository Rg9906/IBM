"""Model input vector (blueprint 10.5) and the Isolation Forest anomaly model (blueprint 11.3).

Scores are mapped to [0, 1] through the empirical CDF of training scores, so 0.99 means "more
unusual than 99 percent of normal training windows". Training uses normal windows only.
"""
from __future__ import annotations

import hashlib
import math
import pickle
from typing import Optional

import numpy as np

BASE_DIM = 8
EWS_DIM = 2
# Neutral values used for NaN inputs, in vector order.
NEUTRAL = np.array([0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0])
FEATURE_NAMES = ["log_arrival_ratio", "timeout_ratio", "reject_ratio", "log_latency_ratio", "raf",
                 "queue_growth_per_cap", "queue_delay_per_timeout", "pending_frac"]


def _pos(x) -> bool:
    return x is not None and not math.isnan(x) and x > 0


def model_vector(row, client_timeout_s: float = 3.0) -> Optional[np.ndarray]:
    """Eight scaled features for one FeatureRow, NaN where unavailable. Returns None if over half are NaN."""
    f, b = row.f, row.base
    v = np.full(BASE_DIM, np.nan)
    if _pos(f["arrival_rate"]) and _pos(b["arrival_rate"]):
        v[0] = math.log(f["arrival_rate"] / b["arrival_rate"])
    v[1], v[2] = f["timeout_ratio"], f["reject_ratio"]
    if _pos(f["latency_ratio"]):
        v[3] = math.log(f["latency_ratio"])
    v[4] = f["raf"]
    load = f["load_over_capacity"]
    if _pos(load) and _pos(f["arrival_rate"]) and f["queue_growth"] is not None:
        v[5] = f["queue_growth"] / (f["arrival_rate"] / load)
    v[6] = f["queue_delay_est"] / client_timeout_s
    v[7] = f["pending_frac"]
    if np.isnan(v).sum() > BASE_DIM // 2:
        return None
    return np.where(np.isnan(v), NEUTRAL, v)


def ews_features(history: list) -> np.ndarray:
    """Rolling variance and lag-1 autocorrelation of queue_depth (critical-slowing-down signals, ablation RM-EWS)."""
    h = np.asarray(history, dtype=float)
    if len(h) < 6 or np.isnan(h).any():
        return np.zeros(EWS_DIM)
    var = float(h.var())
    d = h - h.mean()
    denom = float((d * d).sum())
    ac = float((d[1:] * d[:-1]).sum() / denom) if denom > 0 else 0.0
    return np.array([math.log1p(var), ac])


class IForestModel:
    kind = "iforest"

    def __init__(self, n_estimators: int = 200, max_samples: int = 256, seed: int = 0, ews: bool = False,
                 version: str = "if-dev") -> None:
        self.n_estimators, self.max_samples, self.seed, self.ews, self.version = n_estimators, max_samples, seed, ews, version
        self.mean: Optional[np.ndarray] = None
        self.std: Optional[np.ndarray] = None
        self.clf = None
        self.train_scores: Optional[np.ndarray] = None

    def _scale(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean) / self.std

    def fit(self, X: np.ndarray) -> "IForestModel":
        from sklearn.ensemble import IsolationForest
        X = np.asarray(X, dtype=float)
        self.mean = X.mean(axis=0)
        self.std = np.maximum(X.std(axis=0), 1e-6)      # statistics from training windows only
        self.clf = IsolationForest(n_estimators=self.n_estimators, max_samples=min(self.max_samples, len(X)),
                                   random_state=self.seed, contamination="auto").fit(self._scale(X))
        self.train_scores = np.sort(-self.clf.score_samples(self._scale(X)))
        return self

    def raw(self, X: np.ndarray) -> np.ndarray:
        return -self.clf.score_samples(self._scale(np.atleast_2d(np.asarray(X, dtype=float))))

    def score(self, X: np.ndarray) -> np.ndarray:
        """Empirical-CDF score in [0, 1]; NaN if the model call fails (the caller falls back to rules)."""
        try:
            r = self.raw(X)
            if np.isnan(r).any():
                raise ValueError("NaN score")
            return np.searchsorted(self.train_scores, r, side="right") / len(self.train_scores)
        except Exception:
            return np.full(len(np.atleast_2d(X)), np.nan)

    def sha256(self) -> str:
        return hashlib.sha256(pickle.dumps(self.__dict__, protocol=4)).hexdigest()

    def save(self, path: str) -> str:
        with open(path, "wb") as fh:
            pickle.dump(self.__dict__, fh, protocol=4)
        return self.sha256()

    @classmethod
    def load(cls, path: str) -> "IForestModel":
        m = cls()
        with open(path, "rb") as fh:
            m.__dict__.update(pickle.load(fh))
        return m
