"""Build disjoint configuration sets A (train), B (validation), C (test) (blueprint 9.8).

Disjointness is by construction: onset times are on different grids (A: 160..300 s step 20,
B: 150..290 s step 20, C: 155..305 s step 10 with odd 5 s offset), seeds come from separate ranges
(A 1-999, B 1000-1999, C 2000-2999, blueprint 21.4), and set C alone uses gateway G3, banks B4/B5,
utilisation 0.65, timeout 2500 ms and wider magnitude ranges.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

SCENARIOS = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
SEED_BASE = {"A": 1, "B": 1000, "C": 2000, "D": 3000}
SEED_MAX = {"A": 999, "B": 1999, "C": 2999, "D": 3999}
ONSETS = {"A": list(range(160, 301, 20)), "B": list(range(150, 291, 20)), "C": list(range(155, 306, 10)),
          "D": list(range(152, 303, 10))}      # D: confirmation set, same distribution as C, new seeds and onsets
SET_CODE = {"A": 11, "B": 12, "C": 13, "D": 14}


def _draw(rng, name: str, sc: str, i: int) -> dict:
    c = name in ("C", "D")
    gws = ["G1", "G2", "G3"] if c else ["G1", "G2"]
    banks = ["B1", "B2", "B3", "B4", "B5"] if c else ["B1", "B2", "B3"]
    gw = str(rng.choice(gws))
    bank = str(rng.choice(banks))
    onset = int(rng.choice(ONSETS[name])) * 1000
    util = float(rng.choice([0.55, 0.65] if c else [0.50, 0.55, 0.60]))
    timeout = int(rng.choice([2500, 3000] if c else [3000, 4000]))
    cap = int(rng.choice([4, 5, 6] if c else [4, 5]))
    dur = int(rng.integers(100, 151)) * 1000
    mag = 1.0
    if sc in ("S2", "S5"):
        mag = float(rng.uniform(1.6, 3.4 if c else 3.0))
    elif sc == "S3":
        mag = float(rng.uniform(4.0, 9.0 if c else 8.0))
    elif sc == "S4":
        mag = float(rng.uniform(0.45, 0.75) if c else rng.uniform(0.5, 0.7))
        dur = int(rng.integers(40, 91)) * 1000
    elif sc == "S7":
        mag = float(rng.uniform(2.0, 2.4))
    elif sc == "S1":
        mag = 1.6
    return {"scenario": sc, "gateway": gw, "bank": bank, "onset_ms": onset, "duration_ms": dur,
            "magnitude": round(mag, 4), "utilisation": util, "client_timeout_ms": timeout, "attempt_cap": cap}


def make_configs(name: str, n: int, seed: int = 0) -> list:
    """n configurations per scenario for set `name`; deterministic given (name, n, seed)."""
    if name not in SEED_BASE:
        raise ValueError("set must be A, B, C or D")
    out = []
    k = 0
    for si, sc in enumerate(SCENARIOS):
        rng = np.random.default_rng(np.random.SeedSequence([seed, SET_CODE[name], si]))
        for i in range(n):
            d = _draw(rng, name, sc, i)
            run_seed = SEED_BASE[name] + k
            if run_seed > SEED_MAX[name]:
                raise ValueError(f"set {name}: more than {SEED_MAX[name] - SEED_BASE[name] + 1} configurations")
            d.update({"config_id": f"{name}-{sc}-{i:03d}", "seed": run_seed, "set": name})
            out.append(d)
            k += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-a", type=int, default=100)
    ap.add_argument("--n-b", type=int, default=50)
    ap.add_argument("--n-c", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="artifacts/configs")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, n in (("A", a.n_a), ("B", a.n_b), ("C", a.n_c)):
        cfgs = make_configs(name, n, a.seed)
        (out / f"set_{name}.json").write_text(json.dumps(cfgs, indent=1))
        print(f"set {name}: {len(cfgs)} configurations -> {out / f'set_{name}.json'}")


if __name__ == "__main__":
    main()
