"""S6 verification-storm contrast: naive verify vs no verification vs governed (blueprint 12.5).

Drives the engine directly so the Governor can issue platform-side status checks between steps.
Usage: python scripts/s6_contrast.py [--seeds 3] [--out artifacts/results/s6_contrast.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_configs import make_configs  # noqa: E402
from qs.config import GovernorConfig, SimConfig  # noqa: E402
from qs.core.governor import UNREACHABLE, VerificationGovernor  # noqa: E402
from qs.schema import Action, ActionId, EventType as ET, Status  # noqa: E402
from qs.sim import scenarios  # noqa: E402
from qs.sim.engine import Engine  # noqa: E402
from qs.sim.run import ARM_MIX  # noqa: E402
from qs.sim.scenarios import ScenarioParams  # noqa: E402
from qs.sim.tape import Tape  # noqa: E402

KEEP = {"scenario", "config_id", "gateway", "bank", "onset_ms", "duration_ms", "magnitude", "utilisation",
        "client_timeout_ms", "attempt_cap", "flash_mult"}
WIN_MS = 5000
SERVICE_S = 0.4          # nominal service time (blueprint 9.6), only used to turn workers into a request rate


def run_arm(arm: str, params: ScenarioParams, seed: int) -> dict:
    cfg = scenarios.effective_cfg(SimConfig(), params)
    spec = scenarios.build(params)
    horizon = int(cfg.run_length_s * 1000)
    tape = Tape(seed, cfg, horizon_ms=horizon, rate_fn=spec.rate_fn, rate_max_mult=spec.rate_max_mult)
    eng = Engine(cfg, tape, run_id=f"S6-{seed}", profile_mix=ARM_MIX[arm], modifiers=spec.modifiers,
                 horizon_ms=horizon, seed=seed, attempt_cap=params.attempt_cap)
    gov = VerificationGovernor(GovernorConfig(), seed=seed) if arm == "governed" else None
    if gov:
        eng.apply_action(Action(id=ActionId.VERIFY_PENDING, scope=("platform", "*", "*")))
    checks, qmax, spare, t_timeout, t_resolve = {}, {}, {}, {}, {}
    t = 0
    while t < horizon:
        t += 1000
        eng.run_until(t)
        for e in eng.drain():
            et, ts = e.event_type, e.event_timestamp
            if et is ET.STATUS_CHECK_SENT:
                checks[ts // WIN_MS] = checks.get(ts // WIN_MS, 0) + 1
            elif et is ET.SERVICE_SNAPSHOT:
                qmax[e.gateway_id] = max(qmax.get(e.gateway_id, 0), e.queue_depth or 0)
                cap = e.configured_capacity or 1
                spare[e.gateway_id] = {"spare_capacity": cap / SERVICE_S * max(0.0, 1 - (e.active_requests or 0) / cap)}
            elif et is ET.ATTEMPT_OUTCOME and e.status is Status.CLIENT_TIMEOUT:
                t_timeout.setdefault(e.payment_id, ts)
                if gov:
                    gov.on_timeout(e.payment_id, e.gateway_id, ts)
            elif et is ET.STATUS_CHECK_RESULT:
                if e.status in (Status.CONFIRMED_SUCCESS, Status.CONFIRMED_FAILED):
                    t_resolve.setdefault(e.payment_id, ts)
                if gov:
                    gov.on_status_result(e.payment_id, UNREACHABLE if e.error_code == "E_STATUS_UNREACHABLE"
                                         else e.status.value, ts)
        if gov:
            for pid in gov.due_checks(t, spare):
                eng.request_status_check(pid)
    gt = eng.ground_truth_stats()
    res = np.array([t_resolve[p] - t_timeout[p] for p in t_resolve if p in t_timeout]) / 1000.0
    peak = max(checks.values(), default=0) / (WIN_MS / 1000.0)
    return {"arm": arm, "peak_status_check_rate": peak, "total_checks": sum(checks.values()),
            "peak_queue_depth": max(qmax.values(), default=0), "duplicate_commits": gt["duplicate_commits"],
            "resolved": int(len(res)), "resolve_p50_s": float(np.median(res)) if len(res) else None,
            "resolve_p95_s": float(np.percentile(res, 95)) if len(res) else None,
            "escalated": len(gov.reconcile) if gov else None, "timeouts": len(t_timeout)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--out", default="artifacts/results/s6_contrast.json")
    a = ap.parse_args()
    cfgs = [c for c in make_configs("B", a.seeds, 0) if c["scenario"] == "S6"][: a.seeds]
    rows = []
    for c in cfgs:
        p = ScenarioParams.from_dict({k: v for k, v in c.items() if k in KEEP})
        for arm in ("no_verification", "naive_verify", "governed"):
            r = run_arm(arm, p, c["seed"])
            r["config_id"] = c["config_id"]
            rows.append(r)
            print(json.dumps(r))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rows, indent=2))
    by = {}
    for r in rows:
        by.setdefault(r["arm"], []).append(r["peak_status_check_rate"])
    print({k: float(np.mean(v)) for k, v in by.items()})


if __name__ == "__main__":
    main()
