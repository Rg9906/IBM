"""Exploratory sensitivity sweeps on the retry-storm scenario S4 (blueprint 17.7), on tuning set B (not the test set).

Sweeps: operator approval delay d_op, client timeout, baseline utilisation, attempt cap. Each setting runs the
do-nothing universe U0 and QueueShield U3 on the same configurations (paired by seed). Output:
results/exploratory/sweeps.json and sweeps.md. Surge-size and low-volume-route sweeps are not run.

Usage: run_sweeps.py [--set B] [--n 12] [--workers 6]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from run_eval import arena_job  # noqa: E402

SWEEPS = {
    "d_op_s": ("operator_delay_ms", [5, 15, 30, 60, 120], 1000),
    "client_timeout_ms": ("client_timeout_ms", [2500, 3000, 4000], 1),
    "utilisation": ("utilisation", [0.45, 0.55, 0.65], 1),
    "attempt_cap": ("attempt_cap", [3, 4, 5, 6], 1),
}


def job(args: tuple) -> dict:
    sweep, value, cfg, uni, delay = args
    r = arena_job((cfg, uni, delay))
    r.update({"sweep": sweep, "value": value})
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="B")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    cfgs = [c for c in make_configs(a.set, 30, 0) if c["scenario"] == "S4"][: a.n]
    jobs = []
    for sweep, (key, values, mult) in SWEEPS.items():
        for v in values:
            for c in cfgs:
                if sweep == "d_op_s":
                    jobs.append((sweep, v, c, "U3", v * mult))
                    if v == values[0]:
                        jobs.append((sweep, "U0", c, "U0", 20_000))
                else:
                    c2 = dict(c, **{key: v})
                    jobs += [(sweep, v, c2, "U0", 20_000), (sweep, v, c2, "U3", 20_000)]
    print(f"{len(jobs)} runs on {len(cfgs)} configs", flush=True)
    t0 = time.perf_counter()
    out = []
    with ProcessPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(job, jobs, chunksize=1)):
            out.append(r)
            if i % 25 == 0:
                print(f"{i + 1}/{len(jobs)} {time.perf_counter() - t0:.0f}s", flush=True)
    dst = ROOT / "results" / "exploratory"
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "sweeps.json").write_text(json.dumps({"set": a.set, "scenario": "S4", "n_configs": len(cfgs), "runs": out,
                                                 "wall_s": time.perf_counter() - t0}, indent=1, default=str))

    def med(rs, k):
        v = [r[k] for r in rs if r.get(k) is not None]
        return float(np.median(v)) if v else None

    lines = ["# Sensitivity sweeps on S4 (set B, exploratory)", "",
             f"{len(cfgs)} paired configurations per cell. Medians; recovery excludes runs that never recovered (count shown). In simulation.", "",
             "| sweep | value | universe | peak queue | excess retries | recovery s | not recovered |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for sweep in SWEEPS:
        cells = sorted({(str(r["value"]), r["universe"]) for r in out if r["sweep"] == sweep})
        for v, u in cells:
            rs = [r for r in out if r["sweep"] == sweep and str(r["value"]) == v and r["universe"] == u]
            f = lambda x: "-" if x is None else f"{x:.0f}"  # noqa: E731
            lines.append(f"| {sweep} | {v} | {u} | {f(med(rs, 'peak_queue'))} | {f(med(rs, 'excess_retries'))} | "
                         f"{f(med(rs, 'recovery_s'))} | {sum(1 for r in rs if not r['recovered'])}/{len(rs)} |")
    (dst / "sweeps.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
