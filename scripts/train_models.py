"""Train models. This file holds the Isolation Forest part (WP-05); the forecaster part is added by WP-08.

Normal windows only (blueprint 11.3): all eligible ticks of S0 runs plus the pre-onset ticks of every
other training scenario. Train this on the LinuxONE box for the demo; do not copy pickles across
endianness (blueprint 18.4).
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.clock import utc_iso  # noqa: E402
from qs.config import DetectorConfig, SimConfig, load_yaml  # noqa: E402
from qs.core.detectors.iforest import IForestModel, ews_features, model_vector  # noqa: E402
from qs.eval.replay import replay  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402

KEEP_KEYS = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
             "client_timeout_ms", "attempt_cap", "flash_mult"}


def normal_vectors(job: tuple) -> list:
    cfg_dict = job
    params = ScenarioParams.from_dict({k: v for k, v in cfg_dict.items() if k in KEEP_KEYS})
    r = replay(params.scenario, cfg_dict["seed"], "U0", SimConfig(), params=params, collect_rows=True)
    onset = min([t["onset_ms"] for t in r.run.ground_truth] or [10 ** 12])
    dcfg = DetectorConfig()
    out, hist = [], {}
    for ts, rows in r.ticks:
        for row in rows:
            h = hist.setdefault(row.segment, [])
            h.append(row.f["queue_depth"])
            if not row.eligible or ts >= onset:
                continue
            v = model_vector(row, dcfg.client_timeout_s)
            if v is None:
                continue
            out.append((v, np.concatenate([v, ews_features(h[-dcfg.ews_window:])])))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-s0", type=int, default=12)
    ap.add_argument("--n-per-scenario", type=int, default=3)
    ap.add_argument("--ews", action="store_true", help="also train the RM-EWS variant (10 inputs)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--out", default="artifacts/models")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    cfgs = make_configs("A", max(a.n_s0, a.n_per_scenario), 0)
    jobs = []
    for sc in ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]:
        mine = [c for c in cfgs if c["scenario"] == sc]
        jobs += mine[: a.n_s0 if sc == "S0" else a.n_per_scenario]
    variants = [False] + ([True] if a.ews else [])
    with ProcessPoolExecutor(max_workers=a.workers or None) as ex:
        parts = list(ex.map(normal_vectors, jobs))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    registry = {}
    dcfg = DetectorConfig()
    for ews in variants:
        X = np.array([pair[1 if ews else 0] for p in parts for pair in p])
        name = "iforest_ews" if ews else "iforest"
        m = IForestModel(dcfg.iforest_trees, dcfg.iforest_max_samples, a.seed, ews=ews).fit(X)
        sha = m.save(str(out / f"{name}.pkl"))
        m.version = f"if-{sha[:8]}"
        sha = m.save(str(out / f"{name}.pkl"))
        registry[name] = {"model_name": name, "version": m.version, "sha256": sha, "n_train": int(len(X)),
                          "trained_on_seed_set": "A", "seeds": [j["seed"] for j in jobs], "created_ts": utc_iso()}
        print(f"{name}: {len(X)} normal windows from {len(jobs)} runs; version {m.version}")
    (out / "registry.json").write_text(json.dumps(registry, indent=1))


if __name__ == "__main__":
    main()
