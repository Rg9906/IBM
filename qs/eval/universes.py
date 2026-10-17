"""Policy arena (blueprint 17.2, 17.3): the four universes U0-U3 on one random tape per seed, and the metrics.

U0 naive clients, no action. U1 naive clients plus a textbook reactive failover. U2 disciplined clients, no action.
U3 naive clients with QueueShield recommending and the simulated operator approving after d_op.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from qs.config import GovernorConfig, OperatorConfig, SimConfig
from qs.core.governor import UNREACHABLE, VerificationGovernor
from qs.eval.arena import SimulatedOperator
from qs.schema import Action, ActionId, EventType as ET, Status
from qs.sim import scenarios
from qs.sim.engine import Engine
from qs.sim.run import ARM_MIX
from qs.sim.scenarios import ScenarioParams
from qs.sim.tape import Tape

UNIVERSES = ("U0", "U1", "U2", "U3")
BIN_MS = 5000


class Collector:
    """Event sink that accumulates the policy metrics of 17.3 from the emitted events only."""

    def __init__(self) -> None:
        self.init: dict = {}
        self.first_ok: dict = {}
        self.good_bin: dict = {}
        self.retries = 0
        self.q: dict = {}
        self.checks_bin: dict = {}
        self.gw_out: dict = {}          # (gateway, bin) -> [successes, outcomes]

    def __call__(self, evs: list) -> None:
        for e in evs:
            et, ts = e.event_type, e.event_timestamp
            if et is ET.PAYMENT_INITIATED:
                self.init[e.payment_id] = ts
            elif et is ET.ATTEMPT_SENT:
                self.retries += bool(e.is_retry)
            elif et is ET.ATTEMPT_OUTCOME:
                b = ts // BIN_MS
                c = self.gw_out.setdefault((e.gateway_id, b), [0, 0])
                c[1] += 1
                if e.status is Status.SUCCESS:
                    c[0] += 1
                    self.good_bin[b] = self.good_bin.get(b, 0) + 1
                    self.first_ok.setdefault(e.payment_id, ts)
            elif et is ET.SERVICE_SNAPSHOT:
                self.q[ts] = self.q.get(ts, 0) + (e.queue_depth or 0)
            elif et is ET.STATUS_CHECK_SENT:
                self.checks_bin[ts // BIN_MS] = self.checks_bin.get(ts // BIN_MS, 0) + 1

    def gateway_ratio(self, gw: str, now_ms: int) -> tuple:
        b = now_ms // BIN_MS
        s = o = 0
        for k in (b - 1, b - 2):
            c = self.gw_out.get((gw, k))
            if c:
                s, o = s + c[0], o + c[1]
        return (s / o if o else None), o

    def metrics(self, onset: int, t_end: int, horizon: int, dup: int) -> dict:
        base_bins = [self.good_bin.get(b, 0) for b in range(20_000 // BIN_MS, onset // BIN_MS)]
        base = float(np.mean(base_bins)) if base_bins else 0.0
        rec = None
        need = 30_000 // BIN_MS
        run = 0
        for b in range(t_end // BIN_MS, horizon // BIN_MS):
            run = run + 1 if self.good_bin.get(b, 0) >= 0.95 * base else 0
            if run >= need:
                rec = (b - need + 1) * BIN_MS - t_end
                break
        ok_t = [t - self.init[p] for p, t in self.first_ok.items() if p in self.init and t - self.init[p] <= 30_000]
        return {"peak_queue": max(self.q.values(), default=0), "excess_retries": self.retries,
                "recovery_s": None if rec is None else max(0, rec) / 1000.0, "recovered": rec is not None,
                "unique_success": len(ok_t),
                "p95_completion_s": float(np.percentile(ok_t, 95)) / 1000 if ok_t else None,
                "duplicate_commits": dup, "status_check_peak": max(self.checks_bin.values(), default=0) / (BIN_MS / 1000),
                "payments": len(self.init), "baseline_goodput_per_s": base / (BIN_MS / 1000)}


class GovernorDriver:
    """U2: the disciplined clients' verification runs through the Governor (blueprint 9.3), enabled from t = 0."""

    def __init__(self, engine, seed: int) -> None:
        self.eng = engine
        self.gov = VerificationGovernor(GovernorConfig(), seed=seed)
        self.spare: dict = {}
        engine.apply_action(Action(id=ActionId.VERIFY_PENDING, scope=("platform", "*", "*")))

    def __call__(self, evs: list) -> None:
        for e in evs:
            et = e.event_type
            if et is ET.ATTEMPT_OUTCOME and e.status is Status.CLIENT_TIMEOUT:
                self.gov.on_timeout(e.payment_id, e.gateway_id, e.event_timestamp)
            elif et is ET.STATUS_CHECK_RESULT:
                st = UNREACHABLE if e.error_code == "E_STATUS_UNREACHABLE" else (e.status.value if e.status else "UNKNOWN")
                self.gov.on_status_result(e.payment_id, st, e.event_timestamp)
            elif et is ET.SERVICE_SNAPSHOT:
                cap = e.configured_capacity or 1
                self.spare[e.gateway_id] = {"spare_capacity": cap / 0.4 * max(0.0, 1 - (e.active_requests or 0) / cap)}

    def step(self, now_ms: int) -> None:
        for pid in self.gov.due_checks(now_ms, self.spare):
            self.eng.request_status_check(pid)


def _engine(params: ScenarioParams, seed: int, arm: str, horizon: int, sim_cfg: Optional[SimConfig] = None):
    cfg = scenarios.effective_cfg(sim_cfg or SimConfig(), params)
    spec = scenarios.build(params)
    tape = Tape(seed, cfg, horizon_ms=horizon, rate_fn=spec.rate_fn, rate_max_mult=spec.rate_max_mult)
    eng = Engine(cfg, tape, run_id=f"{params.scenario}-{seed}", profile_mix=ARM_MIX[arm], modifiers=spec.modifiers,
                 horizon_ms=horizon, seed=seed, attempt_cap=params.attempt_cap)
    return eng


class TextbookFailover:
    """U1: move a gateway's traffic to the others when success_ratio < 0.90 for 2 ticks; restore above 0.97 for 3."""

    def __init__(self, engine, collector) -> None:
        self.eng, self.col = engine, collector
        self.low: dict = {}
        self.high: dict = {}
        self.shifted: set = set()
        self.log: list = []

    def step(self, now_ms: int) -> None:
        for gw in ("G1", "G2", "G3"):
            ratio, n = self.col.gateway_ratio(gw, now_ms)
            if ratio is None or n < 20:
                continue
            if gw not in self.shifted:
                self.low[gw] = self.low.get(gw, 0) + 1 if ratio < 0.90 else 0
                if self.low[gw] >= 2:
                    self.eng.apply_action(Action(id=ActionId.SHIFT_TRAFFIC, scope=("gateway", gw, "*"),
                                                 params={"from_gateway": gw, "fraction": 1.0, "ramp_ms": 2000}))
                    self.shifted.add(gw)
                    self.log.append((now_ms, gw, "shift"))
            else:
                self.high[gw] = self.high.get(gw, 0) + 1 if ratio > 0.97 else 0
                if self.high[gw] >= 3:
                    self.eng.apply_action(Action(id=ActionId.SHIFT_TRAFFIC, scope=("gateway", gw, "*"),
                                                 params={"from_gateway": gw}), rollback=True)
                    self.shifted.discard(gw)
                    self.low[gw] = self.high[gw] = 0
                    self.log.append((now_ms, gw, "restore"))


def run_universe(universe: str, params: ScenarioParams, seed: int, tail_ms: int = 180_000,
                 operator_delay_ms: int = 20_000, sim_cfg: Optional[SimConfig] = None) -> dict:
    """One run of one universe on the same tape (same seed). Returns policy metrics and bookkeeping."""
    t_end = params.onset_ms + (60_000 if params.scenario == "S6" else params.duration_ms)
    horizon = t_end + tail_ms
    col = Collector()
    first_alert = None
    if universe == "U3":
        from qs.api.live import LiveRun
        lr = LiveRun(params.scenario, seed, "U3", sim_cfg=sim_cfg, params=params, sink=col, horizon_ms=horizon,
                     operator=SimulatedOperator(OperatorConfig(approval_delay_ms=operator_delay_ms)))
        lr.run()
        eng, applied = lr.engine, lr.applied
        first_alert = next((a.ts for r in lr.results for a in r.alerts if a.detector == "D3"), None)
        shifted = any(a[1] == "SHIFT_TRAFFIC" and not a[2] for a in applied)
        n_actions = len(applied)
    else:
        eng = _engine(params, seed, "U2" if universe == "U2" else "U0", horizon, sim_cfg)
        fo = TextbookFailover(eng, col) if universe == "U1" else None
        gd = GovernorDriver(eng, seed) if universe == "U2" else None
        t = 0
        while t < horizon:
            t = min(horizon, t + 1000)
            eng.run_until(t)
            evs = eng.drain()
            col(evs)
            if gd:
                gd(evs)
                gd.step(t)
            if fo and t % 5000 == 0:
                fo.step(t)
                col(eng.drain())
        shifted = bool(fo and any(k == "shift" for _, _, k in fo.log))
        n_actions = len(fo.log) if fo else 0
    gt = eng.ground_truth_stats()
    m = col.metrics(params.onset_ms, t_end, horizon, gt["duplicate_commits"])
    m.update({"universe": universe, "scenario": params.scenario, "config_id": params.config_id, "seed": seed,
              "n_actions": n_actions, "shift_applied": shifted, "first_alert_ms": first_alert,
              "onset_ms": params.onset_ms})
    return m
