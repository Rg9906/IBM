"""Train and evaluate the Cascade Forecaster (blueprint 13.5, 13.7). Train on A, select on B, report on C."""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from qs.forecast.dataset import FEATURES
from qs.forecast.grid import StuckGrid
from qs.forecast.model import EMA, CalibratedEnsemble

IDX = {f: i for i, f in enumerate(FEATURES)}
NO_RETRY = [f for f in FEATURES if f not in ("orbit_frac", "raf", "timeout_ratio", "d_orbit")]
QUEUE_ONLY = ["q_frac", "delay_frac", "d_q"]


def load(path: str) -> list:
    return json.loads(Path(path).read_text())


def flatten(runs: list) -> tuple:
    X, y, rid, cid = [], [], [], []
    for k, r in enumerate(runs):
        for _, v in r["samples"]:
            X.append(v)
            y.append(int(r["stuck"]))
            rid.append(k)
            cid.append(r["config"]["config_id"])
    return np.array(X), np.array(y), np.array(rid), cid


def class_weights(y: np.ndarray) -> np.ndarray:
    w = {1: 0.5 / max(1, (y == 1).sum()), 0: 0.5 / max(1, (y == 0).sum())}
    return np.array([w[int(v)] for v in y]) * len(y)


def cols(X: np.ndarray, names: list) -> np.ndarray:
    return X[:, [IDX[n] for n in names]]


def fit_grid(X, y) -> StuckGrid:
    return StuckGrid().fit(X[:, IDX["delay_frac"]], X[:, IDX["orbit_frac"]], y, class_weights(y), monotone=False)


def fit_ensemble(X, y, Xv, yv) -> CalibratedEnsemble:
    clf = HistGradientBoostingClassifier(max_depth=4, max_iter=40, learning_rate=0.1, random_state=0)
    clf.fit(X, y, sample_weight=class_weights(y))
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(clf.predict_proba(Xv)[:, 1], yv)
    return CalibratedEnsemble(clf, iso)


def run_ema(p: np.ndarray, rid: np.ndarray) -> np.ndarray:
    out = p.copy()
    for k in np.unique(rid):
        idx = np.where(rid == k)[0]
        e = None
        for i in idx:
            e = p[i] if e is None else EMA * p[i] + (1 - EMA) * e
            out[i] = e
    return out


def bootstrap(metric, y, p, rid, n: int = 200, seed: int = 0) -> tuple:
    """95 percent interval resampling whole runs."""
    rng = np.random.default_rng(seed)
    ids = np.unique(rid)
    by = {k: np.where(rid == k)[0] for k in ids}
    vals = []
    for _ in range(n):
        s = np.concatenate([by[k] for k in rng.choice(ids, len(ids))])
        if len(set(y[s])) < 2:
            continue
        vals.append(metric(y[s], p[s]))
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def score(y, p, rid) -> dict:
    return {"auc": float(roc_auc_score(y, p)), "auc_ci": bootstrap(roc_auc_score, y, p, rid),
            "brier": float(brier_score_loss(y, p)), "brier_ci": bootstrap(brier_score_loss, y, p, rid)}


def reliability(y, p, bins: int = 10) -> list:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (p >= a) & (p < b if b < 1 else p <= b)
        out.append({"bin": [float(a), float(b)], "n": int(m.sum()),
                    "mean_p": float(p[m].mean()) if m.any() else None, "frac_pos": float(y[m].mean()) if m.any() else None})
    return out


def first_alarm(vals: np.ndarray, ts: np.ndarray, thr: float):
    hit = np.where(vals >= thr)[0]
    return int(ts[hit[0]]) if len(hit) else None


def lead_and_false(runs: list, p_by_run: dict, thr: float) -> dict:
    leads, leads_w, fc_nonstuck, n_nonstuck, caught = [], [], 0, 0, 0
    n_stuck = 0
    for k, r in enumerate(runs):
        ts = np.array([s[0] for s in r["samples"]])
        tf = first_alarm(p_by_run[k], ts, thr)
        if r["stuck"]:
            n_stuck += 1
            if r["t_collapse"] is not None and tf is not None:
                leads.append((r["t_collapse"] - tf) / 1000.0)
            if tf is not None:
                leads_w.append((r["t_end"] + 60_000 - tf) / 1000.0)
            caught += tf is not None
        else:
            n_nonstuck += 1
            fc_nonstuck += tf is not None
    return {"thr": thr, "n_stuck": n_stuck, "detected": caught, "lead_s_median": float(np.median(leads)) if leads else None,
            "lead_s_mean": float(np.mean(leads)) if leads else None, "n_lead": len(leads),
            "lead_to_stuck_window_s_median": float(np.median(leads_w)) if leads_w else None,
            "lead_to_stuck_window_s_p10": float(np.percentile(leads_w, 10)) if leads_w else None,
            "false_critical_per_nonstuck_run": fc_nonstuck / max(1, n_nonstuck)}


def split_by_run(p, rid) -> dict:
    return {int(k): p[rid == k] for k in np.unique(rid)}


def main(data: str = "artifacts/data", out: str = "artifacts/models/forecaster",
         results: str = "artifacts/results/forecaster_eval.json") -> dict:
    A, B, C = (load(f"{data}/cascade_{s}.json") for s in "ABC")
    ids = [set(r["config"]["config_id"] for r in s) for s in (A, B, C)]
    assert not (ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2]), "config ids leak across splits"
    XA, yA, rA, _ = flatten(A)
    XB, yB, rB, _ = flatten(B)
    XC, yC, rC, _ = flatten(C)
    grid = fit_grid(XA, yA)
    ens = fit_ensemble(XA, yA, XB, yB)
    lr_q = LogisticRegression(max_iter=500, class_weight="balanced").fit(cols(XA, QUEUE_ONLY), yA)
    ens_nr = fit_ensemble(cols(XA, NO_RETRY), yA, cols(XB, NO_RETRY), yB)
    models = {
        "grid": lambda X: grid.predict(X[:, IDX["delay_frac"]], X[:, IDX["orbit_frac"]]),
        "ensemble": lambda X: ens.predict_proba(X)[:, 1],
        "queue_only_logistic": lambda X: lr_q.predict_proba(cols(X, QUEUE_ONLY))[:, 1],
        "ensemble_no_retry_features": lambda X: ens_nr.predict_proba(cols(X, NO_RETRY))[:, 1],
    }
    rep = {"n_runs": {"A": len(A), "B": len(B), "C": len(C)},
           "stuck_share": {k: float(np.mean([r["stuck"] for r in s])) for k, s in zip("ABC", (A, B, C))},
           "val": {}, "test": {}, "reliability_test": {}}
    for name, f in models.items():
        rep["val"][name] = score(yB, f(XB), rB)
        rep["test"][name] = score(yC, f(XC), rC)
    for name in ("grid", "ensemble"):
        rep["reliability_test"][name] = reliability(yC, models[name](XC))
    best = min(("grid", "ensemble"), key=lambda n: (rep["val"][n]["brier"], -rep["val"][n]["auc"]))
    rep["selected"] = best
    # thresholds on B: smallest critical whose false-critical rate per non-stuck run stays within budget
    pB = split_by_run(run_ema(models[best](XB), rB), rB)
    budget = 0.06      # about 1 per simulated hour for ~3.5 minute runs
    crit = 0.95
    for thr in np.arange(0.4, 0.96, 0.05):
        if lead_and_false(B, pB, float(thr))["false_critical_per_nonstuck_run"] <= budget:
            crit = float(thr)
            break
    watch = round(0.6 * crit, 3)
    rep["thresholds"] = {"critical": crit, "watch": watch, "budget_false_per_nonstuck_run": budget}
    pC = split_by_run(run_ema(models[best](XC), rC), rC)
    rep["lead_test"] = {"forecaster": lead_and_false(C, pC, crit)}
    dC = split_by_run(XC[:, IDX["delay_frac"]], rC)
    rep["lead_test"]["baseline_A_delay_frac_0.8"] = lead_and_false(C, dC, 0.8)
    rep["lead_test_valB"] = lead_and_false(B, pB, crit)
    # inference time per tick for 3 gateways
    import time
    x3 = XC[:3]
    t0 = time.perf_counter()
    for _ in range(200):
        models["grid"](x3)
    rep["infer_ms_grid_3gw"] = (time.perf_counter() - t0) / 200 * 1000
    t0 = time.perf_counter()
    for _ in range(50):
        models["ensemble"](x3)
    rep["infer_ms_ensemble_3gw"] = (time.perf_counter() - t0) / 50 * 1000
    o = Path(out)
    o.mkdir(parents=True, exist_ok=True)
    (o / "forecaster.json").write_text(json.dumps({"grid": grid.to_dict(), "use": best, "watch": watch,
                                                   "critical": crit, "features": FEATURES}))
    with open(o / "ensemble.pkl", "wb") as fh:
        pickle.dump(ens, fh)
    rep["sha256"] = {n: hashlib.sha256((o / n).read_bytes()).hexdigest() for n in ("forecaster.json", "ensemble.pkl")}
    Path(results).parent.mkdir(parents=True, exist_ok=True)
    Path(results).write_text(json.dumps(rep, indent=1))
    return rep


if __name__ == "__main__":
    r = main()
    print(json.dumps({k: r[k] for k in ("n_runs", "stuck_share", "selected", "thresholds", "lead_test",
                                          "infer_ms_grid_3gw", "infer_ms_ensemble_3gw")}, indent=1))
    for split in ("val", "test"):
        for n, s in r[split].items():
            print(split, n, "AUC %.3f %s Brier %.3f" % (s["auc"], [round(x, 3) for x in s["auc_ci"]], s["brier"]))
