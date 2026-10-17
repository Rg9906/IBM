"""Live runner: simulator -> Pipeline -> simulated operator -> approved actions back into the simulator.

Lives in qs/api because qs/sim and qs/core must not import each other (rule 4); this layer may import both.
`step()` advances one simulated second, so a caller (test, evaluation harness or the HTTP app) controls pacing.
"""
from __future__ import annotations

from typing import Callable, Optional

from qs.config import PipelineConfig, SimConfig
from qs.core.pipeline import Pipeline
from qs.eval.arena import SimulatedOperator
from qs.schema import Action
from qs.sim import scenarios
from qs.sim.engine import Engine
from qs.sim.run import ARM_MIX
from qs.sim.scenarios import ScenarioParams
from qs.sim.tape import Tape


class LiveRun:
    def __init__(self, scenario: str, seed: int, arm: str = "U3", sim_cfg: Optional[SimConfig] = None,
                 params: Optional[ScenarioParams] = None, pipeline: Optional[Pipeline] = None,
                 operator: Optional[SimulatedOperator] = None, horizon_ms: Optional[int] = None,
                 on_tick: Optional[Callable] = None, models_dir: str = "artifacts/models") -> None:
        params = params or ScenarioParams(scenario=scenario)
        cfg = scenarios.effective_cfg(sim_cfg or SimConfig(), params)
        spec = scenarios.build(params)
        self.horizon = int(horizon_ms if horizon_ms is not None else cfg.run_length_s * 1000)
        tape = Tape(seed, cfg, horizon_ms=self.horizon, rate_fn=spec.rate_fn, rate_max_mult=spec.rate_max_mult)
        self.engine = Engine(cfg, tape, run_id=f"{scenario}-{seed}", profile_mix=ARM_MIX.get(arm, {"naive": 1.0}),
                             modifiers=spec.modifiers, horizon_ms=self.horizon, seed=seed, attempt_cap=params.attempt_cap)
        self.truth = spec.truth
        self.pipe = pipeline or Pipeline(PipelineConfig(), models_dir=models_dir)
        self.operator = operator
        self.on_tick = on_tick
        self.t = 0
        self.tick_ms = self.pipe.cfg.tick_ms
        self._next_tick = self.tick_ms
        self.results: list = []
        self.applied: list = []        # (ts, action_id, rollback)
        self.events = 0

    def done(self) -> bool:
        return self.t >= self.horizon

    def step(self, dt_ms: int = 1000) -> None:
        self.t = min(self.horizon, self.t + dt_ms)
        self.engine.run_until(self.t)
        evs = self.engine.drain()
        self.events += len(evs)
        if evs:
            self.pipe.ingest(evs)
        while self.t >= self._next_tick + 2000 or (self.done() and self._next_tick <= self.t):
            self._tick(self._next_tick)
            self._next_tick += self.tick_ms
        self._apply_pending()

    def _tick(self, ts: int) -> None:
        r = self.pipe.tick(ts)
        self.results.append(r)
        if self.operator is not None:
            for rec_id, dec in self.operator.step(ts, self.pipe.actions.pending()):
                self.pipe.decide(rec_id, dec)
            for ap in r.rollbacks:
                self.pipe.approve_rollback(ap, self.operator.name)
        for pid in r.status_checks:
            self.engine.request_status_check(pid)
        if self.on_tick:
            self.on_tick(r)

    def _apply_pending(self) -> None:
        for a in self.pipe.pending_actions():
            rb = bool(a.params.get("_rollback"))
            clean = a.model_copy(update={"params": {k: v for k, v in a.params.items() if k != "_rollback"}})
            self.engine.apply_action(clean, rollback=rb)
            self.applied.append((self.t, a.id.value, rb))
        late = self.engine.drain()          # ACTION_APPLIED events
        if late:
            self.events += len(late)
            self.pipe.ingest(late)

    def run(self) -> "LiveRun":
        while not self.done():
            self.step()
        return self
