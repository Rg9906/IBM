"""Run a simulated scenario through ingest, features and detectors in batch mode.

Created in WP-05 because model training and the detector tests need it; WP-13's harness builds on it.
Detectors are objects with update(now_ms, rows) -> list[Alert].
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from qs.config import FeatureConfig, SimConfig
from qs.core.features import FeatureEngine
from qs.core.ingest import Ingestor
from qs.schema import Alert
from qs.sim.run import RunResult, run_scenario
from qs.sim.scenarios import ScenarioParams


@dataclass
class ReplayResult:
    run: RunResult
    alerts: list = field(default_factory=list)       # Alert objects from all detectors, in time order
    ticks: list = field(default_factory=list)        # (tick_ts, rows) when collect_rows is true
    labels: list = field(default_factory=list)       # (tick_ts, {segment: label}) from detectors exposing .labels
    tick_ms: int = 5000


def replay(scenario: str, seed: int, arm: str, sim_cfg: SimConfig, params: Optional[ScenarioParams] = None,
           detectors: Optional[list] = None, collect_rows: bool = False, feature_cfg: Optional[FeatureConfig] = None,
           tick_ms: int = 5000, horizon_ms: Optional[int] = None, watermark_ms: int = 2000) -> ReplayResult:
    fe = FeatureEngine(feature_cfg, tick_ms=tick_ms)
    ing = Ingestor([lambda e, late: (fe.tracker.apply(e, late), fe.ingest(e, late))], watermark_ms=watermark_ms)
    dets = detectors or []
    res = ReplayResult(run=None, tick_ms=tick_ms)   # type: ignore[arg-type]
    state = {"next": tick_ms}

    def do_tick() -> None:
        t = state["next"]
        ing.release_until(t - 1)
        rows = fe.tick(t)
        if collect_rows:
            res.ticks.append((t, rows))
        for d in dets:
            res.alerts.extend(d.update(t, rows))
            if hasattr(d, "labels") and d.labels:
                res.labels.append((t, dict(d.labels)))
        state["next"] += tick_ms

    def sink(evs: list) -> None:
        ing.ingest(evs)
        while ing.max_ts >= state["next"] + watermark_ms:
            do_tick()

    res.run = run_scenario(scenario, seed, arm, sim_cfg, sink=sink, params=params, horizon_ms=horizon_ms)
    return res
