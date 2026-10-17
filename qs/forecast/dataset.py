"""Cascade dataset: one S4 run per configuration, one sample per tick on the affected gateway (blueprint 13.3, 13.4).

Features use only observable quantities. Ground truth (trigger times, scenario) is used only for the label and
for choosing which ticks to log, never as a feature.
"""
from __future__ import annotations

import math
from dataclasses import replace

import numpy as np

from qs.config import SimConfig
from qs.eval.replay import replay
from qs.schema import EventType, Status
from qs.sim.scenarios import ScenarioParams

FEATURES = ["q_frac", "delay_frac", "orbit_frac", "load_ratio", "raf", "timeout_ratio", "d_q", "d_orbit",
            "expired_ratio"]
SEED_BASE = {"A": 1, "B": 1000, "C": 2000}
T_START = 60_000
TICK_MS = 5000
DELTA_TICKS = 3            # 15 s
EST_SERVICE_S = 0.4        # blueprint 9.6 nominal service time


def draw_config(name: str, i: int, boundary_bias: float = 0.6) -> dict:
    """Deterministic configuration i of set `name`. Biased toward the stuck boundary (m 0.3-0.6, d 20-150 s)."""
    rng = np.random.default_rng(SEED_BASE[name] * 100_003 + i)
    near = rng.random() < boundary_bias
    m = float(rng.uniform(0.3, 0.45)) if near else float(rng.uniform(0.3, 0.6))
    d = int(rng.integers(40, 121)) if near else int(rng.integers(20, 151))
    return {"config_id": f"{name}-cas-{i:04d}", "seed": SEED_BASE[name] + i, "m": round(m, 3), "duration_s": d,
            "utilisation": round(float(rng.uniform(0.45, 0.65)), 3),
            "client_timeout_ms": int(rng.integers(2500, 4001)), "attempt_cap": int(rng.integers(3, 7)),
            "gateway": "G1"}


def _goodput_sink(gw: str, sec: dict):
    def sink(events: list) -> None:
        for e in events:
            if e.event_type is EventType.ATTEMPT_OUTCOME and e.status is Status.SUCCESS and e.gateway_id == gw:
                s = e.event_timestamp // 1000
                sec[s] = sec.get(s, 0) + 1
    return sink


def _rate(sec: dict, a_ms: int, b_ms: int) -> float:
    a, b = a_ms // 1000, b_ms // 1000
    return sum(sec.get(s, 0) for s in range(a, b)) / max(1, b - a)


def _nz(x) -> float:
    return 0.0 if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def sample_features(rows_by_tick: list, gw_seg: tuple, timeout_s: float, qmax: float, cap_per_s: float) -> list:
    """rows_by_tick: [(tick_ts, FeatureRow for the gateway)]. Returns [(tick_ts, feature vector)]."""
    hist, out = [], []
    for ts, r in rows_by_tick:
        f = r.f
        q = _nz(f["queue_depth"]) / qmax
        orb = _nz(f["orbit_proxy"]) / cap_per_s
        hist.append((q, orb))
        pq, po = hist[max(0, len(hist) - 1 - DELTA_TICKS)]
        out.append((ts, np.array([q, min(_nz(f["queue_delay_est"]) / timeout_s, 5.0), orb, _nz(f["load_over_capacity"]),
                                  _nz(f["raf"]), _nz(f["timeout_ratio"]), q - pq, orb - po, _nz(f["expired_ratio"])])))
    return out


def run_config(cfg: dict) -> dict:
    """Run one S4 configuration (naive profile, no actions). Returns samples, label and collapse time."""
    base = SimConfig()
    t_s, t_e = T_START, T_START + cfg["duration_s"] * 1000
    p = ScenarioParams("S4", config_id=cfg["config_id"], gateway=cfg["gateway"], onset_ms=t_s,
                       duration_ms=cfg["duration_s"] * 1000, magnitude=round(1.0 - cfg["m"], 4),
                       utilisation=cfg["utilisation"], client_timeout_ms=cfg["client_timeout_ms"],
                       attempt_cap=cfg["attempt_cap"])
    sec: dict = {}
    sink = _goodput_sink(cfg["gateway"], sec)
    r = replay("S4", cfg["seed"], "U0", base, params=p, collect_rows=True, horizon_ms=t_e + 130_000, tap=sink)
    seg = ("gateway", cfg["gateway"], "*")
    rows = [(ts, next(x for x in rs if x.segment == seg)) for ts, rs in r.ticks]
    base_g, after_g = _rate(sec, 20_000, t_s), _rate(sec, t_e + 60_000, t_e + 120_000)
    stuck = bool(after_g < 0.5 * base_g)
    timeout_s = cfg["client_timeout_ms"] / 1000.0
    feats = sample_features([(ts, x) for ts, x in rows if t_s <= ts <= t_e + 60_000], seg, timeout_s,
                            float(base.queue_max), base.workers / EST_SERVICE_S)
    # collapse time: first tick queue_delay_est >= T and stays for 3 ticks
    t_c, run = None, 0
    for ts, x in rows:
        if ts < t_s:
            continue
        run = run + 1 if _nz(x.f["queue_delay_est"]) >= timeout_s else 0
        if run >= 3:
            t_c = ts - 2 * TICK_MS
            break
    return {"config": cfg, "stuck": stuck, "t_start": t_s, "t_end": t_e, "t_collapse": t_c,
            "base_goodput": base_g, "after_goodput": after_g,
            "samples": [(ts, v.tolist()) for ts, v in feats]}
