"""Simulate every scenario once (naive clients, no actions) and cache the per-tick feature rows.

Usage: cache_rows.py --set B --n 15 [--scenarios S0,S1,...]. Output: artifacts/rows/<set>/<config_id>.pkl
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.config import SimConfig  # noqa: E402
from qs.eval import rowcache  # noqa: E402
from qs.eval.replay import replay  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402

KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}


def one(job: tuple) -> str:
    cfg, out = job
    path = Path(out) / f"{cfg['config_id']}.pkl"
    if path.exists():
        return str(path)
    p = ScenarioParams.from_dict({k: v for k, v in cfg.items() if k in KEEP})
    r = replay(p.scenario, cfg["seed"], "U0", SimConfig(), params=p, collect_rows=True)
    rowcache.save(str(path), cfg, r.run.ground_truth, r.ticks,
                  {"wall_s": r.run.wall_s, "gt_stats": r.run.gt_stats})
    return str(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--scenarios", default="S0,S1,S2,S3,S4,S5,S6,S7")
    ap.add_argument("--out", default="artifacts/rows")
    a = ap.parse_args()
    want = set(a.scenarios.split(","))
    out = f"{a.out}/{a.set}"
    jobs = [(c, out) for c in make_configs(a.set, a.n, 0) if c["scenario"] in want]
    t0 = time.perf_counter()
    with ProcessPoolExecutor() as ex:
        done = list(ex.map(one, jobs, chunksize=1))
    print(f"set {a.set}: {len(done)} runs cached in {time.perf_counter() - t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()
