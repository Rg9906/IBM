"""Robust Random Cut Forest alternative for the RM-RRCF ablation (blueprint 11.3, 11.8).

Pure Python (package `rrcf`). Same interface as IForestModel: fit(X), score(X) in [0, 1].
Each score inserts the point into every tree, reads its collusive displacement and removes it again,
so scoring is much slower than Isolation Forest; it is used for the ablation arm only.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


class RRCFModel:
    kind = "rrcf"

    def __init__(self, num_trees: int = 20, tree_size: int = 128, seed: int = 0, ews: bool = False,
                 version: str = "rrcf-dev") -> None:
        self.num_trees, self.tree_size, self.seed, self.ews, self.version = num_trees, tree_size, seed, ews, version
        self.mean: Optional[np.ndarray] = None
        self.std: Optional[np.ndarray] = None
        self.trees: list = []
        self.train_scores: Optional[np.ndarray] = None

    def _scale(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean) / self.std

    def fit(self, X: np.ndarray) -> "RRCFModel":
        import rrcf
        X = np.asarray(X, dtype=float)
        self.mean, self.std = X.mean(axis=0), np.maximum(X.std(axis=0), 1e-6)
        Xs = self._scale(X)
        rng = np.random.default_rng(self.seed)
        # the rrcf package draws cuts from numpy's global RNG, so it is seeded and restored around use
        state = np.random.get_state()
        np.random.seed(self.seed)
        try:
            self.trees = []
            for _ in range(self.num_trees):
                tree = rrcf.RCTree()
                idx = rng.choice(len(Xs), size=min(self.tree_size, len(Xs)), replace=False)
                for i in idx:
                    tree.insert_point(Xs[i], index=int(i))
                self.trees.append(tree)
            hold = rng.choice(len(Xs), size=min(300, len(Xs)), replace=False)
            self.train_scores = np.sort(self._raw(Xs[hold]))
        finally:
            np.random.set_state(state)
        return self

    def _raw(self, Xs: np.ndarray) -> np.ndarray:
        out = np.zeros(len(Xs))
        for k, x in enumerate(Xs):
            tot = 0.0
            for tree in self.trees:
                tree.insert_point(x, index="tmp")
                tot += tree.codisp("tmp")
                tree.forget_point("tmp")
            out[k] = tot / len(self.trees)
        return out

    def score(self, X: np.ndarray) -> np.ndarray:
        try:
            Xs = self._scale(np.atleast_2d(np.asarray(X, dtype=float)))
            state = np.random.get_state()
            np.random.seed(self.seed + 1)
            try:
                r = self._raw(Xs)
            finally:
                np.random.set_state(state)
            return np.searchsorted(self.train_scores, r, side="right") / len(self.train_scores)
        except Exception:
            return np.full(len(np.atleast_2d(X)), np.nan)
