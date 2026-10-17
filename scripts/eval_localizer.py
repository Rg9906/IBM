"""Validate the Fault Localizer on simulated scenarios (blueprint 14.6).

Builds the gateway x bank bad/expected matrices from ATTEMPT_OUTCOME events (10 s window, 5 s tick), using a
per-cell baseline from the pre-onset period, then scores verdicts in the 20-60 s after onset.
Usage: python scripts/eval_localizer.py --set B --n 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.config import SimConfig  # noqa: E402
from qs.core.localizer import FaultLocalizer  # noqa: E402
from qs.core.windows import BANKS, GATEWAYS  # noqa: E402
from qs.schema import EventType, Status, Verdict  # noqa: E402
from qs.sim.run import run_scenario  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402

KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}
EXPECT = {"S2": Verdict.GATEWAY_SIDE, "S3": Verdict.BANK_SIDE, "S4": Verdict.GATEWAY_SIDE,
          "S6": Verdict.BANK_SIDE, "S7": Verdict.ROUTE_SPECIFIC}
WIN, TICK = 10_000, 5_000
GI, BI = {g: i for i, g in enumerate(GATEWAYS)}, {b: i for i, b in enumerate(BANKS)}


def one(cfg: dict) -> dict:
    p = ScenarioParams.from_dict({k: v for k, v in cfg.items() if k in KEEP})
    rec: list = []                                   # (ts, gi, bi, status, rt)

    def sink(evs: list) -> None:
        for e in evs:
            if e.event_type is EventType.ATTEMPT_OUTCOME:
                rec.append((e.event_timestamp, GI[e.gateway_id], BI[e.bank_id], e.status, e.response_time_ms or 0))

    horizon = p.onset_ms + (160_000 if p.scenario == 'S7' else 90_000)
    run_scenario(p.scenario, cfg["seed"], "U0", SimConfig(), sink=sink, params=p, horizon_ms=horizon)
    ts = np.array([r[0] for r in rec])
    gi, bi = np.array([r[1] for r in rec]), np.array([r[2] for r in rec])
    st = [r[3] for r in rec]
    rt = np.array([r[4] for r in rec], float)
    ok = np.array([s in (Status.SUCCESS, Status.DECLINED) for s in st])
    hard = np.array([s in (Status.CLIENT_TIMEOUT, Status.REJECTED_OVERLOAD) for s in st])
    base = (ts >= 20_000) & (ts < p.onset_ms)
    p95 = np.zeros((len(GATEWAYS), len(BANKS)))
    for g in range(len(GATEWAYS)):
        for b in range(len(BANKS)):
            m = base & (gi == g) & (bi == b) & ok
            p95[g, b] = np.percentile(rt[m], 95) if m.sum() >= 20 else np.inf
    bad = hard | (ok & (rt > 1.5 * p95[gi, bi]))
    rate = np.zeros_like(p95)
    for g in range(len(GATEWAYS)):
        for b in range(len(BANKS)):
            m = base & (gi == g) & (bi == b)
            rate[g, b] = max(0.005, bad[m].mean()) if m.sum() else 0.005
    loc = FaultLocalizer()
    out = []
    for t in range(p.onset_ms + 10_000, horizon + 1, TICK):
        w = (ts >= t - WIN) & (ts < t)
        n = np.zeros_like(rate)
        bd = np.zeros_like(rate)
        np.add.at(n, (gi[w], bi[w]), 1)
        np.add.at(bd, (gi[w], bi[w]), bad[w])
        r = loc.raw(bd, n * rate)
        pub = loc.localize(bd, n * rate)
        out.append((t - p.onset_ms, r.verdict.value, pub.verdict.value, r.primary,
                    [c["segment"] for c in r.candidates[:2]]))
    return {"config": cfg["config_id"], "scenario": cfg["scenario"], "gateway": cfg["gateway"], "bank": cfg["bank"],
            "ticks": out}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="B")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--out", default="artifacts/results/localizer_eval.json")
    a = ap.parse_args()
    cfgs = [c for c in make_configs(a.set, a.n, 0) if c["scenario"] in EXPECT]
    t0 = time.perf_counter()
    with ProcessPoolExecutor() as ex:
        runs = list(ex.map(one, cfgs))
    rep: dict = {}
    for sc, want in EXPECT.items():
        rs = [r for r in runs if r["scenario"] == sc]
        if not rs:
            continue
        PH = {"early": (10_000, 30_000), "late": (30_000, 61_000)} if sc != "S7" else {"early": (40_000, 80_000), "late": (80_000, 160_000)}
        def truth(r):
            return {Verdict.GATEWAY_SIDE: ("gateway", r["gateway"], "*"), Verdict.BANK_SIDE: ("bank", "*", r["bank"]),
                    Verdict.ROUTE_SPECIFIC: ("route", r["gateway"], r["bank"])}[want]
        for ph, (lo, hi) in PH.items():
            ticks = [(r, t) for r in rs for t in r["ticks"] if lo <= t[0] < hi]
            rep.setdefault(sc, {})[ph] = score(ticks, want, truth)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"summary": rep, "runs": runs}, default=str))
    print(f"{len(runs)} runs in {time.perf_counter() - t0:.0f}s")
    print(json.dumps(rep, indent=1))


def score(ticks, want, truth) -> dict:
    if not ticks:
        return {"ticks": 0}
    conf = Counter(t[1] for _, t in ticks)
    out = {"ticks": len(ticks), "expected": want.value,
           "verdict_correct": float(np.mean([t[1] == want.value for _, t in ticks])),
           "published_correct": float(np.mean([t[2] == want.value for _, t in ticks])),
           "primary_top1": float(np.mean([tuple(t[3] or ()) == truth(r) for r, t in ticks])),
           "top2": float(np.mean([truth(r) in [tuple(c) for c in t[4]] for r, t in ticks])),
           "verdict_counts": dict(conf)}
    if want is Verdict.BANK_SIDE:
        out["bank_called_gateway"] = conf.get("GATEWAY_SIDE", 0) / len(ticks)
    return out


if __name__ == "__main__":
    main()
