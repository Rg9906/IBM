"""Exploratory ablation arms RM-RRCF and RM-EWS (blueprint 11.8), reported separately from the pre-registered P1 to P6.

Stages (each resumable; scores are cached on disk because model scores do not depend on tau):
  train   train the RRCF model on the same normal windows as the Isolation Forest -> artifacts/models/rrcf_train.npy (windows; refit per process)
  tune    bisect tau on set B to the equal false-alert budget -> results/exploratory/ablation_frozen.json
  score   score the arms on set C with those taus -> results/exploratory/ablation.json

The frozen payment configs (configs/detectors_frozen*) and results/detection.json are never written. Set C is read
for these two extra arms only; the headline arms were scored once (docs/EVAL.md) and are copied in unchanged.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.config import DetectorConfig  # noqa: E402
from qs.core.detectors.iforest import IForestModel, ews_features, model_vector  # noqa: E402
from qs.core.detectors.persistence import QueueShield  # noqa: E402
from qs.core.detectors.rrcf import RRCFModel  # noqa: E402
from qs.eval import detection as D  # noqa: E402
from qs.eval import rowcache  # noqa: E402

OUT = ROOT / "results" / "exploratory"
CACHE = ROOT / "artifacts" / "ablation"
MODELS = ROOT / "artifacts" / "models"
ARMS = {"RM-RRCF": MODELS / "rrcf_train.npy", "RM-EWS": MODELS / "iforest_ews.pkl"}


class Recorder:
    """Pass 1: returns a neutral score and records every vector the detector asks about."""

    def __init__(self) -> None:
        self.vecs: list = []

    def score(self, X: np.ndarray) -> np.ndarray:
        self.vecs.append(np.array(X[0], dtype=float))
        return np.array([0.0])


class Lookup:
    """Pass 2: serves precomputed scores by vector bytes."""

    def __init__(self, table: dict) -> None:
        self.t = table

    def score(self, X: np.ndarray) -> np.ndarray:
        return np.array([self.t.get(np.asarray(X[0], dtype=float).tobytes(), float("nan"))])


_RRCF: dict = {}


def load_model(arm: str):
    if arm == "RM-RRCF":                       # RCTree objects cannot be pickled: refit (seeded, deterministic) per process
        if "m" not in _RRCF:
            _RRCF["m"] = RRCFModel(seed=0).fit(np.load(MODELS / "rrcf_train.npy"))
        return _RRCF["m"]
    with open(ARMS[arm], "rb") as fh:
        blob = pickle.load(fh)
    cls = RRCFModel if arm == "RM-RRCF" else IForestModel
    m = cls()
    m.__dict__.update(blob)
    return m


def run_detector(path: str, arm: str, model, tau: float | None) -> dict:
    run = rowcache.load(path)
    pk = run["pk"]
    cfg = DetectorConfig()
    if tau is not None:
        from dataclasses import replace
        cfg = replace(cfg, tau=tau, tau_hi=max(cfg.tau_hi, tau))
    det = QueueShield(cfg, model=model, mode=arm, name=arm)
    alerts = []
    for i in range(len(pk["ts"])):
        alerts += det.update(int(pk["ts"][i]), rowcache.rows_at(pk, i))
    return {"config": run["config"], "truth": run["truth"], "last_ts": int(pk["ts"][-1]),
            "alerts": [(a.ts, a.segment, a.kind) for a in alerts]}


def scores_job(args: tuple) -> str:
    path, arm = args
    dst = CACHE / arm / (Path(path).parent.name + "_" + Path(path).stem + ".pkl")
    if dst.exists():
        return str(dst)
    rec = Recorder()
    run_detector(path, arm, rec, None)
    uniq = {v.tobytes(): v for v in rec.vecs}
    keys = list(uniq)
    model = load_model(arm)
    sc = model.score(np.vstack([uniq[k] for k in keys])) if arm == "RM-EWS" else \
        np.array([model.score(uniq[k][None, :])[0] for k in keys])
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as fh:
        pickle.dump(dict(zip(keys, map(float, sc))), fh, protocol=4)
    return str(dst)


def alerts_job(args: tuple) -> dict:
    path, arm, tau, table_path = args
    with open(table_path, "rb") as fh:
        table = pickle.load(fh)
    return run_detector(path, arm, Lookup(table), tau)


def cache_path(path: str, arm: str) -> str:
    return str(CACHE / arm / (Path(path).parent.name + "_" + Path(path).stem + ".pkl"))


def stage_train(a) -> None:
    from train_models import normal_vectors
    cfgs = make_configs("A", 12, 0)
    jobs = []
    for sc in ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]:
        jobs += [c for c in cfgs if c["scenario"] == sc][: 12 if sc == "S0" else 3]
    with ProcessPoolExecutor(a.workers) as ex:
        parts = list(ex.map(normal_vectors, jobs))
    X = np.array([pair[0] for p in parts for pair in p])
    np.save(MODELS / "rrcf_train.npy", X)
    RRCFModel(seed=0).fit(X)                   # smoke test that the fit works
    print(f"rrcf training windows saved: {len(X)} normal windows from {len(jobs)} runs")


def score_set(set_name: str, arm: str, workers: int) -> list:
    paths = D.paths(set_name)
    with ProcessPoolExecutor(workers) as ex:
        list(ex.map(scores_job, [(p, arm) for p in paths], chunksize=1))
    return paths


def stage_tune(a) -> None:
    frozen = {}
    for arm in a.arms.split(","):
        t0 = time.perf_counter()
        paths = score_set("B", arm, a.workers)
        with ProcessPoolExecutor(a.workers) as ex:
            def f(tau: float, arm=arm) -> float:
                res = list(ex.map(alerts_job, [(p, arm, tau, cache_path(p, arm)) for p in paths], chunksize=2))
                fa = D.fa_per_hour([D.score_run(r) for r in res])
                print(f"{arm} tau={tau:.4f} FA/h={fa:.2f}", flush=True)
                return fa
            tau, fa = D.bisect_param(f, 0.5, 1.0, a.budget, False, a.iters, False)
        frozen[arm] = {"tau": tau, "fa_per_hour_on_B": fa, "budget": a.budget, "budget_met": fa <= a.budget,
                       "wall_s": time.perf_counter() - t0}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ablation_frozen.json").write_text(json.dumps(frozen, indent=1))


def stage_score(a) -> None:
    from run_eval import DETECT_SCEN  # noqa: F401
    frozen = json.loads((OUT / "ablation_frozen.json").read_text())
    base = json.loads((ROOT / "results" / "detection.json").read_text())
    out = {"note": "exploratory; arms not in the pre-registered P1 to P6; D3 copied from results/detection.json", "arms": {}}
    for arm in frozen:
        paths = score_set(a.set, arm, a.workers)
        with ProcessPoolExecutor(a.workers) as ex:
            res = list(ex.map(alerts_job, [(p, arm, frozen[arm]["tau"], cache_path(p, arm)) for p in paths], chunksize=2))
        scored = [D.score_run(r) for r in res]
        per: dict = {}
        for s in scored:
            e = per.setdefault(s["scenario"], {"n_incidents": 0, "missed": 0, "delays": [], "false_groups": 0, "quiet_s": 0})
            e["n_incidents"] += s["n_incidents"]
            e["missed"] += s["missed"]
            e["delays"] += s["delays"]
            e["false_groups"] += s["false_groups"]
            e["quiet_s"] += s["quiet_s"]
        for e in per.values():
            e["median"] = float(np.median(e["delays"])) if e["delays"] else None
        allq = sum(s["quiet_s"] for s in scored)
        surge = [s for s in scored if s["scenario"] in ("S0", "S1")]
        out["arms"][arm] = {"tau": frozen[arm]["tau"], "by_scenario": per,
                            "fa_per_hour_all": 3600 * sum(s["false_groups"] for s in scored) / max(1.0, allq),
                            "fa_per_hour_S0_S1": 3600 * sum(s["false_groups"] for s in surge) / max(1.0, sum(s["quiet_s"] for s in surge))}
    out["reference"] = {k: {"fa_per_hour_all": v["fa_per_hour_all"], "fa_per_hour_S0_S1": v["fa_per_hour_S0_S1"],
                            "by_scenario": {s: {"n_incidents": e["n_incidents"], "missed": e["missed"],
                                                "median": (e["median_iqr"] or [None])[0]} for s, e in v["by_scenario"].items()}}
                        for k, v in base["arms"].items() if k in ("D3", "D3:R")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ablation.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: (v["fa_per_hour_all"], v["fa_per_hour_S0_S1"]) for k, v in out["arms"].items()}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["train", "tune", "score"])
    ap.add_argument("--set", default="C")
    ap.add_argument("--arms", default="RM-EWS,RM-RRCF")
    ap.add_argument("--budget", type=float, default=1.5)
    ap.add_argument("--iters", type=int, default=7)
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()
    {"train": stage_train, "tune": stage_tune, "score": stage_score}[a.stage](a)


if __name__ == "__main__":
    main()
