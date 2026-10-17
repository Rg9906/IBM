"""Generate the cascade dataset for one set (blueprint 13.3). Usage: make_cascade_dataset.py --set A --n 300."""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qs.forecast.dataset import draw_config, run_config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=["A", "B", "C"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--bias", type=float, default=0.6, help="share of configs drawn near the stuck boundary")
    ap.add_argument("--out", default="artifacts/data")
    a = ap.parse_args()
    cfgs = [draw_config(a.set, i, a.bias) for i in range(a.n)]
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=a.workers or None) as ex:
        runs = list(ex.map(run_config, cfgs, chunksize=1))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"cascade_{a.set}.json").write_text(json.dumps(runs))
    st = sum(r["stuck"] for r in runs)
    print(f"set {a.set}: {len(runs)} runs, {st} stuck ({st / len(runs):.0%}), "
          f"{sum(len(r['samples']) for r in runs)} samples, {time.perf_counter() - t0:.0f}s")


if __name__ == "__main__":
    main()
