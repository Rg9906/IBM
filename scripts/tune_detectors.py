"""Equal false-alert budget (blueprint 11.7): bisect each detector's one sensitivity parameter on set B so that it
raises at most BUDGET merged false alerts per simulated hour, then freeze the values.

Usage: tune_detectors.py [--set B] [--budget 1.0]. Writes configs/detectors_frozen.yaml (DetectorConfig keys) and
configs/detectors_frozen_arms.json (all arms), plus artifacts/results/tuning.json with the search trace.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qs.clock import utc_iso  # noqa: E402
from qs.eval import detection as D  # noqa: E402

MODEL = "artifacts/models/iforest.pkl"
# name: (spec, lo, hi, FA grows with param?, log scale)
SPECS = {
    "D0": ({"det": "D0"}, 0.30, 0.995, True, False),
    "D1": ({"det": "D1"}, 0.02, 20.0, True, True),
    "D2": ({"det": "D2"}, 1.0, 100.0, False, True),
    "D3:M": ({"det": "D3", "mode": "M", "name": "D3:M"}, 0.5, 1.0, False, False),
    "D3": ({"det": "D3", "mode": "RM"}, 0.5, 1.0, False, False),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="B")
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--iters", type=int, default=9)
    ap.add_argument("--arms", default=",".join(SPECS))
    a = ap.parse_args()
    paths = D.paths(a.set)
    if not paths:
        raise SystemExit("no cached rows; run scripts/cache_rows.py first")
    trace: dict = {}
    frozen: dict = {}
    with ProcessPoolExecutor() as ex:
        for name in a.arms.split(","):
            base, lo, hi, inc, log = SPECS[name]

            def f(p: float, base=base, name=name) -> float:
                spec = dict(base, param=p)
                res = list(ex.map(D.run_alerts, [(pt, spec, MODEL) for pt in paths]))
                fa = D.fa_per_hour([D.score_run(r) for r in res])
                trace.setdefault(name, []).append((p, fa))
                print(f"{name} param={p:.5g} FA/h={fa:.2f}", flush=True)
                return fa

            p, fa = D.bisect_param(f, lo, hi, a.budget, inc, a.iters, log)
            frozen[name] = {"param": p, "fa_per_hour_on_B": fa, "budget_met": fa <= a.budget}
    # D3:R has no tunable parameter: report its false-alert rate as is
    if "D3:R" not in frozen and "D3" in frozen:
        spec = {"det": "D3", "mode": "R", "name": "D3:R"}
        res = list(ExecutorSingle().map(D.run_alerts, [(pt, spec, MODEL) for pt in paths]))
        frozen["D3:R"] = {"param": None, "fa_per_hour_on_B": D.fa_per_hour([D.score_run(r) for r in res]),
                          "budget_met": None}
    out = Path("artifacts/results")
    out.mkdir(parents=True, exist_ok=True)
    (out / "tuning.json").write_text(json.dumps({"frozen": frozen, "trace": trace, "budget": a.budget,
                                                 "set": a.set, "n_runs": len(paths), "created": utc_iso()}, indent=1))
    (ROOT / "configs" / "detectors_frozen_arms.json").write_text(json.dumps(frozen, indent=1))
    lines = ["# Frozen on set B at the equal false-alert budget (blueprint 11.7). Do not edit after the eval-freeze tag.",
             f"# created {utc_iso()}, budget {a.budget} merged false alerts per simulated hour, {len(paths)} runs"]
    cur = {"theta0": frozen.get("D0", {}).get("param"), "sensitivity": frozen.get("D1", {}).get("param"),
           "cusum_h": frozen.get("D2", {}).get("param"), "tau": frozen.get("D3", {}).get("param")}
    for k, v in cur.items():
        if v is not None:
            lines.append(f"{k}: {v:.6g}")
    (ROOT / "configs" / "detectors_frozen.yaml").write_text("\n".join(lines) + "\n")
    print(json.dumps(frozen, indent=1))


class ExecutorSingle:
    def map(self, fn, it):
        return [fn(x) for x in it]


if __name__ == "__main__":
    main()
