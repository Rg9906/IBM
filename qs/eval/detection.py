"""Detector evaluation on cached feature rows: matching to ground truth, false-alert budget and bisection
(blueprint 11.6, 11.7). Detectors are rebuilt per call from a small spec so the work can run in a process pool.

spec = {"det": "D0"|"D1"|"D2"|"D3", "mode": "R"|"M"|"RM"|..., "param": float}
param is theta0 (D0), sensitivity (D1), cusum_h (D2) or tau (D3 with a model).
"""
from __future__ import annotations

import glob
import math
from dataclasses import replace
from typing import Optional

import numpy as np

from qs.config import DetectorConfig
from qs.core.detectors.adaptive import AdaptiveCusum
from qs.core.detectors.iforest import IForestModel, model_vector
from qs.core.detectors.persistence import QueueShield
from qs.core.detectors.threshold import PlatformThreshold, SegmentThresholds
from qs.core.lifecycle import overlaps
from qs.eval import rowcache

GRACE_MS = 30_000
VALID_MS = 300_000
MERGE_MS = 30_000
_MODEL: dict = {}


class BatchScored:
    """Wraps a fitted model; scores all of a run's vectors in one batch, then serves single lookups."""

    def __init__(self, model) -> None:
        self.model, self.cache = model, {}

    def warm(self, vectors: list) -> None:
        if not vectors:
            return
        X = np.vstack(vectors)
        for v, s in zip(vectors, self.model.score(X)):
            self.cache[v.tobytes()] = float(s)

    def score(self, X: np.ndarray) -> np.ndarray:
        k = X[0].tobytes()
        if k not in self.cache:
            self.cache[k] = float(self.model.score(X)[0])
        return np.array([self.cache[k]])


def _model(path: str):
    if path not in _MODEL:
        _MODEL[path] = IForestModel.load(path)
    return _MODEL[path]


def build(spec: dict, cfg: Optional[DetectorConfig] = None, model=None):
    cfg = cfg or DetectorConfig()
    d, p = spec["det"], spec.get("param")
    if d == "D0":
        return PlatformThreshold(replace(cfg, theta0=p) if p is not None else cfg)
    if d == "D1":
        return SegmentThresholds(replace(cfg, sensitivity=p) if p is not None else cfg)
    if d == "D2":
        return AdaptiveCusum(replace(cfg, cusum_h=p) if p is not None else cfg)
    mode = spec.get("mode", "RM")
    if p is not None:
        cfg = replace(cfg, tau=p, tau_hi=max(cfg.tau_hi, p))
    return QueueShield(cfg, model=model if mode != "R" else None, mode=mode, name=spec.get("name"))


def run_alerts(args: tuple) -> dict:
    """Worker: load one cached run, run one detector over it, return alert tuples and run metadata."""
    path, spec, model_path = args
    run = rowcache.load(path)
    pk = run["pk"]
    model = None
    if spec["det"] == "D3" and spec.get("mode", "RM") != "R":
        model = BatchScored(_model(model_path))
        vecs = []
        for i in range(len(pk["ts"])):
            for r in rowcache.rows_at(pk, i):
                if r.eligible:
                    v = model_vector(r)
                    if v is not None:
                        vecs.append(v)
        model.warm(vecs)
    det = build(spec, model=model)
    alerts = []
    for i in range(len(pk["ts"])):
        alerts += det.update(int(pk["ts"][i]), rowcache.rows_at(pk, i))
    return {"config": run["config"], "truth": run["truth"], "last_ts": int(pk["ts"][-1]),
            "alerts": [(a.ts, a.segment, a.kind) for a in alerts]}


# ------------------------------------------------------------------ matching
def covers(alert_seg: tuple, inc_seg: tuple) -> bool:
    if alert_seg[0] == "platform" or alert_seg == inc_seg:
        return True
    if alert_seg[0] == "gateway" and inc_seg[0] == "route":
        return alert_seg[1] == inc_seg[1]
    if alert_seg[0] == "bank" and inc_seg[0] == "route":
        return alert_seg[2] == inc_seg[2]
    return False


def incidents(truth: list) -> list:
    return [t for t in truth if t.get("is_incident")]


def inc_seg(t: dict) -> tuple:
    return (t["level"], t["gateway_id"], t["bank_id"])


def group(alerts: list) -> list:
    """Merge alerts on overlapping segments within MERGE_MS (platform-kind alerts group on their own)."""
    groups: list = []
    for ts, seg, kind in sorted(alerts, key=lambda a: a[0]):
        for g in groups:
            if g["kind"] == kind and ts - g["last"] <= MERGE_MS and any(overlaps(seg, s) for s in g["segs"]):
                g["last"] = ts
                g["segs"].append(seg)
                break
        else:
            groups.append({"start": ts, "last": ts, "segs": [seg], "kind": kind})
    return groups


def score_run(res: dict) -> dict:
    """Per-run delay per incident, false alert groups and the simulated time over which false alerts count."""
    inc = incidents(res["truth"])
    out = {"scenario": res["config"]["scenario"], "config_id": res["config"]["config_id"], "delays": [],
           "missed": 0, "n_incidents": len(inc)}
    for t in inc:
        s = inc_seg(t)
        valid = [a for a in res["alerts"] if covers(a[1], s) and t["onset_ms"] <= a[0] <= t["onset_ms"] + VALID_MS]
        if valid:
            out["delays"].append((min(a[0] for a in valid) - t["onset_ms"]) / 1000.0)
        else:
            out["missed"] += 1
    wins = [(t["onset_ms"], t["end_ms"] + GRACE_MS) for t in inc]
    first_onset = min([t["onset_ms"] for t in inc], default=None)
    if first_onset is None:
        horizon, counted = res["last_ts"], res["alerts"]
    else:                                      # only the quiet period before the first incident counts
        horizon, counted = first_onset, [a for a in res["alerts"] if a[0] < first_onset]
    fa = [a for a in counted if not any(lo <= a[0] <= hi for lo, hi in wins)]
    out["false_groups"] = len(group(fa))
    out["quiet_s"] = horizon / 1000.0
    return out


def fa_per_hour(scored: list) -> float:
    secs = sum(s["quiet_s"] for s in scored)
    return 3600.0 * sum(s["false_groups"] for s in scored) / max(secs, 1.0)


def paths(set_name: str, root: str = "artifacts/rows") -> list:
    return sorted(glob.glob(f"{root}/{set_name}/*.pkl"))


def bisect_param(f, lo: float, hi: float, target: float, increasing_fa_with_param: bool, iters: int = 10,
                 log: bool = False) -> tuple:
    """Find the most sensitive parameter value whose false-alert rate stays <= target.

    f(param) -> false alerts per hour. If FA grows with the parameter we search the largest feasible value,
    otherwise the smallest. Returns (param, fa). Falls back to the safe end of the range if nothing fits.
    """
    mid = (lambda a, b: math.sqrt(a * b)) if log else (lambda a, b: (a + b) / 2)
    safe = lo if increasing_fa_with_param else hi
    best, best_fa = safe, f(safe)
    a, b = lo, hi
    for _ in range(iters):
        m = mid(a, b)
        fa = f(m)
        if fa <= target:
            best, best_fa = m, fa
            if increasing_fa_with_param:
                a = m
            else:
                b = m
        else:
            if increasing_fa_with_param:
                b = m
            else:
                a = m
    return best, best_fa
