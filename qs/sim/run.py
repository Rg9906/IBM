"""Scenario runner (blueprint 19.4). Works in batch mode: the sink is any callable taking list[Event]."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from qs.config import SimConfig
from qs.schema import Action, Event
from qs.sim import scenarios
from qs.sim.engine import Engine
from qs.sim.scenarios import ScenarioParams
from qs.sim.tape import Tape


class EventSink(Protocol):
    def __call__(self, events: list) -> object: ...


class ActionSource(Protocol):
    def __call__(self) -> list: ...


# Client population per arm (blueprint 9.3). U0, U1, U3 keep naive clients; U2 uses disciplined clients.
ARM_MIX = {"U0": {"naive": 1.0}, "U1": {"naive": 1.0}, "U2": {"disciplined": 1.0}, "U3": {"naive": 1.0},
           "naive_verify": {"naive_verify": 1.0}, "no_verification": {"naive": 1.0},
           "governed": {"disciplined": 1.0}}


def null_sink(events: list) -> None:
    return None


def null_actions() -> list:
    return []


@dataclass
class RunResult:
    scenario: str
    seed: int
    arm: str
    ground_truth: list = field(default_factory=list)
    gt_stats: dict = field(default_factory=dict)
    n_events: int = 0
    wall_s: float = 0.0
    sim_s: float = 0.0
    engine: object = None   # kept for the evaluator and tests; not serialised


def run_scenario(scenario: str, seed: int, arm: str, cfg: SimConfig, sink: EventSink = null_sink,
                 action_source: ActionSource = null_actions, params: Optional[ScenarioParams] = None,
                 out_dir: Optional[str] = None, horizon_ms: Optional[int] = None, step_ms: int = 1000,
                 keep_engine: bool = False) -> RunResult:
    params = params or ScenarioParams(scenario=scenario)
    if params.scenario != scenario:
        raise ValueError("params.scenario does not match scenario")
    t0 = time.perf_counter()
    ecfg = scenarios.effective_cfg(cfg, params)
    spec = scenarios.build(params)
    horizon = int(horizon_ms if horizon_ms is not None else ecfg.run_length_s * 1000)
    tape = Tape(seed, ecfg, horizon_ms=horizon, rate_fn=spec.rate_fn, rate_max_mult=spec.rate_max_mult)
    eng = Engine(ecfg, tape, run_id=f"{scenario}-{seed}", profile_mix=ARM_MIX.get(arm, {"naive": 1.0}),
                 modifiers=spec.modifiers, horizon_ms=horizon, seed=seed, attempt_cap=params.attempt_cap)
    n_events = 0
    t = 0
    while t < horizon:
        t = min(horizon, t + step_ms)
        eng.run_until(t)
        evs = eng.drain()
        if evs:
            n_events += len(evs)
            sink(evs)
        for act in action_source():
            eng.apply_action(act if isinstance(act, Action) else Action.model_validate(act))
        late = eng.drain()   # ACTION_APPLIED events
        if late:
            n_events += len(late)
            sink(late)
    res = RunResult(scenario, seed, arm, ground_truth=spec.truth, gt_stats=eng.ground_truth_stats(),
                    n_events=n_events, wall_s=time.perf_counter() - t0, sim_s=horizon / 1000.0,
                    engine=eng if keep_engine else None)
    if out_dir:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "ground_truth.json").write_text(json.dumps(spec.truth, indent=2))
    return res
