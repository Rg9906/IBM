"""Metastability calibration sweep (blueprint 9.9).

Sweeps the remaining-capacity multiplier m and trigger duration d with the naive profile and no
actions. A run is 'stuck' when goodput on the affected gateway in the window 60 to 120 s after the
trigger ends is below 50 percent of that gateway's pre-trigger goodput. Prints seconds per run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qs.config import SimConfig, load_yaml  # noqa: E402
from qs.schema import EventType, Status  # noqa: E402
from qs.sim.run import run_scenario  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402

T_START = 60_000
GATEWAY = "G1"


class _Goodput:
    def __init__(self, gateway: str) -> None:
        self.gw = gateway
        self.sec: dict = {}

    def __call__(self, events: list) -> None:
        gw = self.gw
        for e in events:
            if e.event_type is EventType.ATTEMPT_OUTCOME and e.status is Status.SUCCESS and e.gateway_id == gw:
                s = e.event_timestamp // 1000
                self.sec[s] = self.sec.get(s, 0) + 1

    def rate(self, a_ms: int, b_ms: int) -> float:
        a, b = a_ms // 1000, b_ms // 1000
        return sum(self.sec.get(s, 0) for s in range(a, b)) / max(1, b - a)


def one_run(args: tuple) -> dict:
    m, d_s, seed, cfg_dict, overrides = args
    cfg = replace(SimConfig(), **cfg_dict)
    t_e = T_START + d_s * 1000
    horizon = t_e + 130_000
    p = ScenarioParams("S4", config_id=f"cal-{m}-{d_s}-{seed}", gateway=GATEWAY, onset_ms=T_START,
                       duration_ms=d_s * 1000, magnitude=round(1.0 - m, 4),
                       utilisation=overrides.get("utilisation", 0.55),
                       client_timeout_ms=overrides.get("client_timeout_ms", cfg.client_timeout_ms),
                       attempt_cap=overrides.get("attempt_cap", 5))
    gp = _Goodput(GATEWAY)
    r = run_scenario("S4", seed, "U0", cfg, sink=gp, params=p, horizon_ms=horizon)
    base = gp.rate(20_000, T_START)
    after = gp.rate(t_e + 60_000, t_e + 120_000)
    return {"m": m, "d": d_s, "seed": seed, "base": base, "after": after,
            "stuck": bool(after < 0.5 * base), "wall_s": r.wall_s}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--ms", default="0.5,0.4,0.35,0.3")
    ap.add_argument("--ds", default="20,40,60,90,120")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--timeout-ms", type=int, default=0)
    ap.add_argument("--qmax", type=int, default=0)
    ap.add_argument("--attempt-cap", type=int, default=5)
    ap.add_argument("--utilisation", type=float, default=0.55)
    ap.add_argument("--out", default="artifacts/results/metastability")
    a = ap.parse_args()
    ms = [float(x) for x in a.ms.split(",")]
    ds = [int(x) for x in a.ds.split(",")]
    cfg_dict: dict = {}
    if a.timeout_ms:
        cfg_dict["client_timeout_ms"] = a.timeout_ms
    if a.qmax:
        cfg_dict["queue_max"] = a.qmax
    over = {"attempt_cap": a.attempt_cap, "utilisation": a.utilisation}
    if a.timeout_ms:
        over["client_timeout_ms"] = a.timeout_ms
    jobs = [(m, d, 100 + s, cfg_dict, over) for m in ms for d in ds for s in range(a.seeds)]
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=a.workers or None) as ex:
        rows = list(ex.map(one_run, jobs, chunksize=2))
    wall = time.perf_counter() - t0
    table: dict = {}
    for r in rows:
        table.setdefault((r["m"], r["d"]), []).append(r["stuck"])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "runs.json").write_text(json.dumps(rows))
    print(f"{len(rows)} runs in {wall:.0f}s wall; mean {sum(r['wall_s'] for r in rows) / len(rows):.2f} s/run "
          f"({wall / len(rows):.2f} s/run effective with parallelism)")
    print("stuck probability (rows m, columns trigger duration s):")
    print("m \\ d  " + "  ".join(f"{d:>5d}" for d in ds))
    grid = []
    for m in ms:
        row = [sum(table[(m, d)]) / len(table[(m, d)]) for d in ds]
        grid.append(row)
        print(f"{m:<6} " + "  ".join(f"{x:5.2f}" for x in row))
    ok = [(m, d_lo, d_hi) for i, m in enumerate(ms) for j, d_lo in enumerate(ds) for d_hi in ds[j + 1:]
          if grid[i][j] <= 0.2 and grid[i][ds.index(d_hi)] >= 0.8]
    print("acceptance (short recovers >=80% and long sticks >=80% at same m):", "PASS" if ok else "FAIL", ok[:5])
    (out / "heatmap.json").write_text(json.dumps({"ms": ms, "ds": ds, "grid": grid, "accepted": ok}))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(5.5, 3.6))
        im = ax.imshow(grid, vmin=0, vmax=1, cmap="viridis", aspect="auto", origin="lower")
        ax.set_xticks(range(len(ds)), [str(d) for d in ds])
        ax.set_yticks(range(len(ms)), [str(m) for m in ms])
        ax.set_xlabel("trigger duration (s)")
        ax.set_ylabel("remaining capacity m")
        for i, row in enumerate(grid):
            for j, x in enumerate(row):
                ax.text(j, i, f"{x:.2f}", ha="center", va="center", color="w" if x < 0.6 else "k", fontsize=8)
        fig.colorbar(im, label="P(stuck)")
        figdir = Path("artifacts/figures")
        figdir.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(figdir / "stuck_heatmap.png", dpi=150)
        print("heat map saved to", figdir / "stuck_heatmap.png")
    except ImportError:
        print("matplotlib not installed; heat map available as heatmap.json only")


if __name__ == "__main__":
    main()
