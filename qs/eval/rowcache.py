"""Compact on-disk cache of per-tick feature rows so detectors can be re-run without re-simulating."""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

from qs.core.features import FEATURES
from qs.core.windows import SEGMENT_KEYS
from qs.schema import FeatureRow


def pack(ticks: list) -> dict:
    """ticks: [(tick_ts, [FeatureRow ...])] -> arrays."""
    n, s, f = len(ticks), len(SEGMENT_KEYS), len(FEATURES)
    F, Z, B = (np.full((n, s, f), np.nan) for _ in range(3))
    elig = np.zeros((n, s), bool)
    flags: list = []
    ts = np.zeros(n, np.int64)
    for i, (t, rows) in enumerate(ticks):
        ts[i] = t
        fl = []
        for j, r in enumerate(rows):
            F[i, j] = [r.f[k] for k in FEATURES]
            Z[i, j] = [r.z[k] for k in FEATURES]
            B[i, j] = [r.base.get(k, np.nan) for k in FEATURES]
            elig[i, j] = r.eligible
            fl.append(tuple(r.flags))
        flags.append(fl)
    return {"ts": ts, "F": F.astype(np.float32), "Z": Z.astype(np.float32), "B": B.astype(np.float32),
            "elig": elig, "flags": flags}


def rows_at(pk: dict, i: int) -> list:
    out = []
    for j, seg in enumerate(SEGMENT_KEYS):
        out.append(FeatureRow.model_construct(
            tick_ts=int(pk["ts"][i]), segment=seg, eligible=bool(pk["elig"][i, j]), flags=list(pk["flags"][i][j]),
            f=dict(zip(FEATURES, pk["F"][i, j].astype(float).tolist())),
            z=dict(zip(FEATURES, pk["Z"][i, j].astype(float).tolist())),
            base=dict(zip(FEATURES, pk["B"][i, j].astype(float).tolist()))))
    return out


def save(path: str, cfg: dict, truth: list, ticks: list, extra: dict | None = None) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump({"config": cfg, "truth": truth, "pk": pack(ticks), **(extra or {})}, fh, protocol=4)


def load(path: str) -> dict:
    with open(path, "rb") as fh:
        return pickle.load(fh)
