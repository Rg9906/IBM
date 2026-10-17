"""Detector sanity check on non-payment server metrics (blueprint 17.8): Numenta Anomaly Benchmark, realAWSCloudwatch.

Compares a CUSUM baseline (D2 style), a windowed Isolation Forest and RRCF, all with the same k-of-n persistence rule.
Thresholds are bisected on tuning series to an equal false-alert budget and then scored once on disjoint test series.
Never mixed with the payment results (blueprint 17.8, 27).

Data are downloaded by this script into artifacts/nab (git-ignored) from github.com/numenta/NAB (AGPL-3.0); they are
read as CSV/JSON data only and are not redistributed.

Usage: public_benchmark.py [--budget 1.0] [--workers 6]   (--download is implicit when files are missing)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qs.core.detectors.iforest import IForestModel  # noqa: E402
from qs.core.detectors.persistence import CLEAR_TICKS, Persistence  # noqa: E402
from qs.core.detectors.rrcf import RRCFModel  # noqa: E402

BASE = "https://raw.githubusercontent.com/numenta/NAB/master/"
DATA = ROOT / "artifacts" / "nab"
GROUP = "realAWSCloudwatch/"
TEST = ["ec2_cpu_utilization_24ae8d", "ec2_cpu_utilization_5f5533", "ec2_network_in_257a54",
        "elb_request_count_8c0756", "rds_cpu_utilization_cc0c53"]
TUNE = ["ec2_cpu_utilization_53ea38", "ec2_cpu_utilization_825cc2", "ec2_disk_write_bytes_1ef3de",
        "rds_cpu_utilization_e47b3b", "ec2_network_in_5abac7"]
PROBATION = 0.15          # NAB convention: first 15 percent is training and is never scored
SHINGLE = 6
GRACE = timedelta(minutes=60)
K, N = 3, 4               # same persistence rule as the payment detector (DetectorConfig)
FMT = "%Y-%m-%d %H:%M:%S"


def fetch(rel: str) -> Path:
    dst = DATA / rel.replace("/", "_")
    if not dst.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BASE + rel, timeout=60) as r:
            dst.write_bytes(r.read())
    return dst


def load_series(name: str) -> tuple:
    ts, v = [], []
    with open(fetch(f"data/{GROUP}{name}.csv"), newline="") as fh:
        for row in csv.DictReader(fh):
            ts.append(datetime.strptime(row["timestamp"][:19], FMT))
            v.append(float(row["value"]))
    labels = json.loads(fetch("labels/combined_windows.json").read_text())[f"{GROUP}{name}.csv"]
    wins = [(datetime.strptime(a[:19], FMT), datetime.strptime(b[:19], FMT)) for a, b in labels]
    return ts, np.asarray(v, float), wins


def shingles(v: np.ndarray, w: int) -> np.ndarray:
    pad = np.concatenate([np.full(w - 1, v[0]), v])
    return np.lib.stride_tricks.sliding_window_view(pad, w).copy()


def cusum_scores(v: np.ndarray, n_train: int, alpha: float = 0.02, k: float = 0.5) -> np.ndarray:
    """Two-sided CUSUM on z-scores against an EWMA mean/variance baseline; returns the larger statistic."""
    mu, var = float(np.mean(v[:n_train])), float(np.var(v[:n_train])) + 1e-9
    hi = lo = 0.0
    out = np.zeros(len(v))
    for i, x in enumerate(v):
        z = (x - mu) / math.sqrt(var)
        hi, lo = max(0.0, hi + z - k), max(0.0, lo - z - k)
        out[i] = max(hi, lo)
        d = x - mu
        mu += alpha * d
        var = (1 - alpha) * (var + alpha * d * d)
    return out


def score_series(job: tuple) -> dict:
    name, det = job
    ts, v, wins = load_series(name)
    n_train = int(PROBATION * len(v))
    if det == "CUSUM":
        s = cusum_scores(v, n_train)
    else:
        X = shingles(v, SHINGLE)
        model = IForestModel(seed=0) if det == "IForest" else RRCFModel(seed=0)
        model.fit(X[:n_train])
        # raw scores relative to the largest training score: unlike the empirical-CDF score they do not saturate at 1,
        # so the threshold can be tuned to the budget
        if det == "IForest":
            s = model.raw(X) / float(model.train_scores[-1])
        else:
            s = np.array([model._raw(model._scale(x[None, :]))[0] for x in X]) / float(model.train_scores[-1])
    return {"name": name, "det": det, "scores": s.tolist()}


def alerts_from(scores: np.ndarray, thr: float, n_train: int) -> list:
    pers, active, quiet, out = Persistence(K, N), False, 0, []
    for i, s in enumerate(scores):
        hit = (not math.isnan(s)) and s >= thr
        ok = pers.push(hit)
        if ok:
            quiet = 0
            if not active and i >= n_train:
                active = True
                out.append(i)
        else:
            quiet += 1
            if quiet >= CLEAR_TICKS:
                active = False
    return out


def evaluate(series: dict, scores: dict, thr: float) -> dict:
    """series: name -> (ts, v, wins); scores: name -> array. Pooled counts over the series."""
    fa = det = n_win = 0
    days = 0.0
    delays = []
    for name, (ts, v, wins) in series.items():
        n_train = int(PROBATION * len(v))
        idx = alerts_from(np.asarray(scores[name]), thr, n_train)
        days += (ts[-1] - ts[n_train]).total_seconds() / 86400
        for ws, we in wins:
            n_win += 1
            hit = [i for i in idx if ws <= ts[i] <= we]
            if hit:
                det += 1
                delays.append((ts[hit[0]] - ws).total_seconds() / 60)
        for i in idx:
            if not any(ws <= ts[i] <= we + GRACE for ws, we in wins):
                fa += 1
    return {"false_alerts": fa, "days": days, "fa_per_day": fa / max(days, 1e-9), "windows": n_win,
            "detected": det, "median_delay_min": float(np.median(delays)) if delays else None}


def tune(series: dict, scores: dict, budget: float, lo: float, hi: float, log: bool) -> float:
    """Smallest threshold whose pooled false-alert rate is within the budget (bisection; FA falls as threshold rises)."""
    mid = (lambda a, b: math.sqrt(a * b)) if log else (lambda a, b: (a + b) / 2)
    a, b, best = lo, hi, hi
    for _ in range(30):
        m = mid(a, b)
        if evaluate(series, scores, m)["fa_per_day"] <= budget:
            best, b = m, m
        else:
            a = m
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=1.0, help="pooled false alerts per day on the tuning series")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="results/exploratory/public_benchmark.json")
    a = ap.parse_args()
    t0 = time.perf_counter()
    names = TUNE + TEST
    data = {n: load_series(n) for n in names}
    dets = ["CUSUM", "IForest", "RRCF"]
    jobs = [(n, d) for d in dets for n in names]
    with ProcessPoolExecutor(a.workers) as ex:
        res = list(ex.map(score_series, jobs, chunksize=1))
    sc = {(r["det"], r["name"]): np.asarray(r["scores"]) for r in res}
    out = {"dataset": "NAB realAWSCloudwatch (Numenta, AGPL-3.0; not redistributed)", "budget_fa_per_day": a.budget,
           "tune_series": TUNE, "test_series": TEST, "detectors": {}}
    rng = {"CUSUM": (0.5, 200.0, True), "IForest": (0.3, 5.0, True), "RRCF": (0.3, 50.0, True)}
    for d in dets:
        lo, hi, log = rng[d]
        tr = {n: data[n] for n in TUNE}
        thr = tune(tr, {n: sc[(d, n)] for n in TUNE}, a.budget, lo, hi, log)
        e_tune = evaluate(tr, {n: sc[(d, n)] for n in TUNE}, thr)
        te = {n: data[n] for n in TEST}
        e_test = evaluate(te, {n: sc[(d, n)] for n in TEST}, thr)
        per = {n: evaluate({n: data[n]}, {n: sc[(d, n)]}, thr) for n in TEST}
        out["detectors"][d] = {"threshold": thr, "tune": e_tune, "test": e_test, "test_per_series": per}
        print(d, "thr", round(thr, 4), "tune FA/day", round(e_tune["fa_per_day"], 2), "test", e_test, flush=True)
    out["wall_s"] = time.perf_counter() - t0
    p = ROOT / a.out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1))
    lines = ["# Detector sanity check on non-payment server metrics", "",
             "NAB realAWSCloudwatch, thresholds set on tuning series to the same false-alert budget, scored on disjoint test series. "
             "Not payment data; never compare with the payment results.", "",
             "| Detector | threshold | tune FA/day | test windows detected | test median delay (min) | test FA/day |",
             "| --- | --- | --- | --- | --- | --- |"]
    for d, r in out["detectors"].items():
        t = r["test"]
        lines.append(f"| {d} | {r['threshold']:.4g} | {r['tune']['fa_per_day']:.2f} | {t['detected']}/{t['windows']} | "
                     f"{t['median_delay_min'] if t['median_delay_min'] is None else round(t['median_delay_min'], 0)} | {t['fa_per_day']:.2f} |")
    (p.parent / "public_benchmark.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
