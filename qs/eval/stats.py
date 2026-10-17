"""Paired statistics for the evaluation (blueprint 17.5)."""
from __future__ import annotations

import numpy as np


def iqr(x) -> tuple:
    x = np.asarray(x, float)
    if len(x) == 0:
        return (None, None, None)
    return (float(np.median(x)), float(np.percentile(x, 25)), float(np.percentile(x, 75)))


def paired_bootstrap(a, b, n: int = 10_000, seed: int = 0) -> dict:
    """Median of (a - b) with a 95 percent bootstrap interval over pairs, plus a Wilcoxon sanity check."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = a - b
    if len(d) == 0:
        return {"n": 0, "median_diff": None, "lo": None, "hi": None, "wilcoxon_p": None, "effect_r": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n, len(d)))
    meds = np.median(d[idx], axis=1)
    out = {"n": int(len(d)), "median_diff": float(np.median(d)), "lo": float(np.percentile(meds, 2.5)),
           "hi": float(np.percentile(meds, 97.5)), "wilcoxon_p": None, "effect_r": None}
    nz = d[d != 0]
    if len(nz) >= 6:
        try:
            from scipy.stats import wilcoxon
            out["wilcoxon_p"] = float(wilcoxon(nz).pvalue)
            out["effect_r"] = float(abs(np.mean(np.sign(nz))))          # matched-pairs rank-biserial style sign effect
        except ImportError:
            pass
    return out
