"""Scenarios S0 to S7 and ground truth (blueprint 9.7). Ground truth never enters the event stream."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Optional

import numpy as np

from qs.config import SimConfig
from qs.sim.entities import Modifier

BASE_UTILISATION = 0.55


@dataclass
class ScenarioParams:
    """One draw of scenario parameters (a 'configuration', blueprint 9.8)."""
    scenario: str
    config_id: str = "manual"
    gateway: str = "G1"
    bank: str = "B1"
    onset_ms: int = 240_000
    duration_ms: int = 120_000
    magnitude: float = 2.0
    utilisation: float = BASE_UTILISATION
    client_timeout_ms: int = 3000
    attempt_cap: int = 5
    flash_mult: float = 2.0     # blueprint 9.7 says 2.5; see docs/DEVIATIONS.md

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScenarioParams":
        return cls(**d)


@dataclass
class ScenarioSpec:
    modifiers: list = field(default_factory=list)
    rate_fn: Optional[Callable] = None
    rate_max_mult: float = 1.0
    truth: list = field(default_factory=list)


def effective_cfg(base: SimConfig, p: ScenarioParams) -> SimConfig:
    return replace(base, fresh_rate_per_s=base.fresh_rate_per_s * p.utilisation / BASE_UTILISATION,
                   client_timeout_ms=p.client_timeout_ms)


def _truth(p: ScenarioParams, idx: int, onset: int, end: int, level: str, gw: str, bank: str, kind: str,
           mag: float, is_incident: bool) -> dict:
    return {"incident_id": f"{p.config_id}-i{idx}", "onset_ms": int(onset), "end_ms": int(end), "level": level,
            "gateway_id": gw, "bank_id": bank, "kind": kind, "magnitude": float(mag), "is_incident": is_incident}


def _surge_rate_fn(onset: int, flash_start: int, flash_mult: float):
    def fn(t: np.ndarray) -> np.ndarray:
        m = np.ones_like(t, dtype=float)
        m[(t >= onset) & (t < onset + 180_000)] = 1.6
        m[(t >= flash_start) & (t < flash_start + 20_000)] = flash_mult
        return m
    return fn


def build(p: ScenarioParams) -> ScenarioSpec:
    s, on, d = p.scenario, p.onset_ms, p.duration_ms
    sp = ScenarioSpec()
    if s == "S0":
        return sp
    if s in ("S1", "S5"):
        sp.rate_fn = _surge_rate_fn(on, on + 80_000, p.flash_mult)
        sp.rate_max_mult = max(1.6, p.flash_mult)
        sp.truth.append(_truth(p, 0, on, on + 180_000, "platform", "*", "*", "surge", 1.6, False))
    if s in ("S2", "S5"):
        sp.modifiers.append(Modifier("gw_service", on, on + d, v0=p.magnitude, gateway=p.gateway))
        sp.truth.append(_truth(p, 1, on, on + d, "gateway", p.gateway, "*", "gateway_slowdown", p.magnitude, True))
    elif s == "S3":
        sp.modifiers.append(Modifier("bank_latency", on, on + d, v0=p.magnitude, bank=p.bank))
        sp.truth.append(_truth(p, 1, on, on + d, "bank", "*", p.bank, "bank_slowdown", p.magnitude, True))
    elif s == "S4":
        sp.modifiers.append(Modifier("capacity", on, on + d, v0=1.0 - p.magnitude, gateway=p.gateway))
        sp.truth.append(_truth(p, 1, on, on + d, "gateway", p.gateway, "*", "capacity_loss", p.magnitude, True))
    elif s == "S6":
        sp.modifiers.append(Modifier("blackhole", on, on + 60_000, v0=0.4, bank=p.bank, resolve_ms=on + 60_000 + 90_000))
        sp.truth.append(_truth(p, 1, on, on + 60_000, "bank", "*", p.bank, "verification_storm", 0.4, True))
    elif s == "S7":
        sp.modifiers.append(Modifier("route_latency", on, on + 120_000, v0=1.0, v1=p.magnitude,
                                     gateway=p.gateway, bank=p.bank))
        # onset is when the multiplier first reaches x1.3
        onset = on + round(120_000 * 0.3 / (p.magnitude - 1.0))
        sp.truth.append(_truth(p, 1, onset, on + 120_000, "route", p.gateway, p.bank, "silent_drift", p.magnitude, True))
    elif s not in ("S1", "S5"):
        raise ValueError(f"unknown scenario {s!r}")
    return sp
