"""Feature engine: one numeric row per segment every tick (blueprint 10)."""
from __future__ import annotations

from typing import Optional

import numpy as np

from qs.config import FeatureConfig
from qs.core import windows as W
from qs.core.baselines import Baselines
from qs.core.payments import PaymentTracker
from qs.schema import Event, FeatureRow

FEATURES = ["arrival_rate", "fresh_rate", "raf", "success_ratio", "timeout_ratio", "reject_ratio", "p50", "p95",
            "p99", "latency_ratio", "pending_count", "pending_frac", "queue_depth", "queue_growth", "utilisation",
            "queue_delay_est", "orbit_proxy", "status_check_rate", "load_over_capacity", "expired_ratio"]
FI = {f: i for i, f in enumerate(FEATURES)}
QUEUE_DELAY_CAP_S = 60.0


class FeatureEngine:
    def __init__(self, cfg: Optional[FeatureConfig] = None, tick_ms: int = 5000,
                 tracker: Optional[PaymentTracker] = None, priors: Optional[dict] = None) -> None:
        self.cfg = cfg or FeatureConfig()
        self.tick_ms = tick_ms
        self.tracker = tracker or PaymentTracker(self.cfg.orbit_window_s * 1000)
        self.w = W.Windows(self.cfg.ring_buckets)
        self.bl = Baselines(FEATURES, W.N_SEGMENTS, self.cfg.ewma_alpha,
                            warm_ticks=max(1, self.cfg.warmup_s * 1000 // tick_ms), z_clip=self.cfg.z_clip,
                            prior_weight=self.cfg.prior_weight)
        if priors:
            self.bl.load_priors(priors, W.SEGMENT_INDEX)
        self.last_f: Optional[np.ndarray] = None
        self.last_z: Optional[np.ndarray] = None
        self._ticks = 0

    def ingest(self, event: Event, late: bool = False) -> None:
        self.w.add(event, late)

    def set_frozen(self, segments: set) -> None:
        """Freeze baselines of the given segment keys while incidents are open (blueprint 10.3)."""
        rows = np.zeros(W.N_SEGMENTS, dtype=bool)
        for k in segments:
            rows[W.SEGMENT_INDEX[tuple(k)]] = True
        self.bl.set_frozen(rows)

    def tick(self, now_ms: int) -> list:
        c = self.cfg
        w = self.w
        end = now_ms // 1000
        w.advance(end)
        Ws, Wl = c.short_window_s, c.long_window_s
        sent, fresh = w.sum(W.SENT, end, Ws), w.sum(W.FRESH, end, Ws)
        out, succ, decl = w.sum(W.OUTCOME, end, Ws), w.sum(W.SUCCESS, end, Ws), w.sum(W.DECLINED, end, Ws)
        tmo, rej = w.sum(W.TIMEOUT, end, Ws), w.sum(W.REJECT, end, Ws)
        out_l, succ_l = w.sum(W.OUTCOME, end, Wl), w.sum(W.SUCCESS, end, Wl)
        nan = np.nan
        with np.errstate(invalid="ignore", divide="ignore"):
            arrival, fresh_rate = sent / Ws, fresh / Ws
            raf = np.where(fresh > 0, sent / np.maximum(fresh, 1), nan)
            success_ratio = np.where(out_l > 0, succ_l / np.maximum(out_l, 1), nan)
            timeout_ratio = np.where(out > 0, tmo / np.maximum(out, 1), nan)
            reject_ratio = np.where(out > 0, rej / np.maximum(out, 1), nan)
        p50, p95, p99 = W.quantiles(w.hist_sum(end, Ws), (0.5, 0.95, 0.99), c.min_latency_samples)
        j95 = FI["p95"]
        base95 = np.where(self.bl.n[:, j95] > 0, self.bl.mean[:, j95], nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            latency_ratio = p95 / base95

        pending = self.tracker.pending_by_segment()
        with np.errstate(invalid="ignore", divide="ignore"):
            pending_frac = np.where(arrival > 0, pending / (arrival * 60.0), nan)

        # gateway snapshots mapped to segments
        qg_all = np.nan_to_num(w.snap_series(end, c.queue_growth_window_s))   # (3 fields, gateways, 30)
        R = sent[W.ROUTE0:].reshape(W.N_GW, W.N_BANK)
        Wn = np.zeros((W.N_SEGMENTS, W.N_GW))
        Wn[0] = 1.0 / W.N_GW
        for g in range(W.N_GW):
            Wn[1 + g, g] = 1.0
            for b in range(W.N_BANK):
                Wn[W.ROUTE0 + g * W.N_BANK + b, g] = 1.0
        for b in range(W.N_BANK):
            col = R[:, b]
            Wn[1 + W.N_GW + b] = col / col.sum() if col.sum() > 0 else 1.0 / W.N_GW
        Wq = Wn.copy()
        Wq[0] = 1.0                                       # platform queue is the sum over gateways
        q_series = Wq @ qg_all[0]                         # (24, 30)
        queue_depth = q_series[:, -Ws:].mean(axis=1)
        x = np.arange(q_series.shape[1], dtype=float)
        xc = x - x.mean()
        queue_growth = ((q_series - q_series.mean(axis=1, keepdims=True)) * xc).sum(axis=1) / (xc @ xc)
        act_g, conf_g = qg_all[1][:, -Ws:].mean(axis=1), qg_all[2][:, -Ws:].mean(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            utilisation = np.where(Wn @ conf_g > 0, (Wn @ act_g) / (Wn @ conf_g), nan)
            cps = (succ + decl) / Ws
            queue_delay = np.minimum(queue_depth / np.maximum(cps, 0.1), QUEUE_DELAY_CAP_S)
        orbit = self.tracker.orbit_by_segment(now_ms)
        status_rate = w.sum(W.STATUS, end, Ws) / Ws

        cap_g = conf_g / (c.est_service_ms / 1000.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            gsum = R.sum(axis=1, keepdims=True)
            share = np.where(gsum > 0, R / np.maximum(gsum, 1), 0.0)
            cap_route = cap_g[:, None] * share
            cap = np.concatenate([[cap_g.sum()], cap_g, cap_route.sum(axis=0), cap_route.ravel()])
            load = np.where(cap > 0, arrival / np.where(cap > 0, cap, 1), nan)

        F = np.full((W.N_SEGMENTS, len(FEATURES)), nan)
        for name, arr in (("arrival_rate", arrival), ("fresh_rate", fresh_rate), ("raf", raf),
                          ("success_ratio", success_ratio), ("timeout_ratio", timeout_ratio),
                          ("reject_ratio", reject_ratio), ("p50", p50), ("p95", p95), ("p99", p99),
                          ("latency_ratio", latency_ratio), ("pending_count", pending),
                          ("pending_frac", pending_frac), ("queue_depth", queue_depth),
                          ("queue_growth", queue_growth), ("utilisation", utilisation),
                          ("queue_delay_est", queue_delay), ("orbit_proxy", orbit),
                          ("status_check_rate", status_rate), ("load_over_capacity", load)):
            F[:, FI[name]] = arr
        # expired_ratio stays NaN: late completions are not observable (docs/DEVIATIONS.md)

        no_data = (sent == 0) & (out == 0)
        low_vol = np.zeros(W.N_SEGMENTS, dtype=bool)
        low_vol[W.ROUTE0:] = sent[W.ROUTE0:] < c.route_min_attempts
        warming = self.bl.warming_up()
        Z = self.bl.z(F)
        B = np.where(self.bl.n > 0, self.bl.mean, np.nan)
        eligible = ~(warming | no_data | low_vol)
        self.bl.update(F, ~no_data)
        self.last_f, self.last_z = F, Z
        self._ticks += 1
        if self._ticks % 12 == 0:
            self.tracker.prune(now_ms)

        rows = []
        Fl, Zl, Bl = F.tolist(), Z.tolist(), B.tolist()
        for i in range(W.N_SEGMENTS):
            flags = []
            if warming[i]:
                flags.append("warming_up")
            if no_data[i]:
                flags.append("no_data")
            if low_vol[i]:
                flags.append("low_volume")
            rows.append(FeatureRow.model_construct(
                tick_ts=now_ms, segment=W.SEGMENT_KEYS[i], eligible=bool(eligible[i]), flags=flags,
                f=dict(zip(FEATURES, Fl[i])), z=dict(zip(FEATURES, Zl[i])),
                base=dict(zip(FEATURES, Bl[i]))))
        return rows
