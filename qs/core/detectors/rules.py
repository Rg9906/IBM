"""D3 rule layer: five corroborating signals, the surge gate and reason codes (blueprint 11.2)."""
from __future__ import annotations

import math
from typing import Optional

from qs.config import DetectorConfig

SIGNALS = ("S_lat", "S_to", "S_q", "S_raf", "S_rej")


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def signals(row, cfg: DetectorConfig) -> list:
    """Names of the corroborating signals that are true for this FeatureRow."""
    f, z, b = row.f, row.z, row.base
    out = []
    if _ok(z["latency_ratio"]) and z["latency_ratio"] >= cfg.z_lat and _ok(f["latency_ratio"]) \
            and f["latency_ratio"] >= cfg.lat_min_ratio:
        out.append("S_lat")
    if _ok(z["timeout_ratio"]) and z["timeout_ratio"] >= cfg.z_to and _ok(b["timeout_ratio"]) \
            and f["timeout_ratio"] - b["timeout_ratio"] >= cfg.to_abs:
        out.append("S_to")
    qd, qb = f["queue_delay_est"], b["queue_delay_est"]
    if _ok(f["queue_growth"]) and f["queue_growth"] > cfg.queue_growth_eps and _ok(qd) and _ok(qb) \
            and qd > cfg.q_mult * qb and qd - qb >= cfg.q_min_delay_s:
        out.append("S_q")
    if _ok(f["raf"]) and _ok(b["raf"]) and f["raf"] >= cfg.raf_mult * b["raf"]:
        out.append("S_raf")
    if _ok(z["reject_ratio"]) and z["reject_ratio"] >= cfg.z_rej and _ok(b["reject_ratio"]) \
            and f["reject_ratio"] - b["reject_ratio"] >= cfg.rej_abs:
        out.append("S_rej")
    return out


class SurgeGate:
    """Label a segment SURGE when arrivals jump, at most one signal is true and the queue is not growing.

    The queue condition is "queue_growth not positive for two consecutive ticks" (this tick and the last).
    A surge that drains is legitimate demand and raises no alert.
    """

    def __init__(self, cfg: DetectorConfig) -> None:
        self.cfg = cfg
        self._prev_nonpos: dict = {}

    def label(self, row, n_signals: int) -> Optional[str]:
        qg = row.f["queue_growth"]
        nonpos = (not _ok(qg)) or qg <= self.cfg.queue_growth_eps
        prev = self._prev_nonpos.get(row.segment, False)
        self._prev_nonpos[row.segment] = nonpos
        za = row.z["arrival_rate"]
        if _ok(za) and za >= self.cfg.surge_z and n_signals <= self.cfg.surge_max_signals and nonpos and prev:
            return "SURGE"
        return None


_REASONS = (("timeout_ratio", "TIMEOUT_UP", 1), ("latency_ratio", "LATENCY_UP", 1), ("raf", "RAF_UP", 1),
            ("queue_growth", "QUEUE_GROWING", 1), ("queue_delay_est", "QUEUE_GROWING", 1),
            ("pending_frac", "PENDING_BACKLOG", 1), ("reject_ratio", "REJECT_UP", 1))


def reason_codes(row, top: int = 4) -> list:
    """Reason codes ranked by absolute z-score, with observed and baseline values. Supporting signals only."""
    seen, ranked = set(), []
    for feat, code, sign in _REASONS:
        z = row.z.get(feat)
        if not _ok(z) or z * sign <= 0 or code in seen:
            continue
        ranked.append((abs(z), code, feat))
    out = []
    for az, code, feat in sorted(ranked, reverse=True):
        if code in seen:
            continue
        seen.add(code)
        obs, base = row.f[feat], row.base.get(feat)
        out.append(f"{code} z={row.z[feat]:+.1f} ({obs:.3g} vs {base:.3g})")
        if len(out) >= top:
            break
    return out
