"""Common-random-number tape (blueprint 9.5).

Every draw is indexed by (payment index, attempt number), never consumed in order, so every
policy universe for one seed sees identical underlying randomness. Service times are
lognormal and are built from standard-normal draws: exp(mu + sigma * z) is F_inverse(quantile).
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from qs.config import SimConfig

MAX_ATTEMPTS = 6  # largest attempt cap used anywhere (blueprint 13.3: caps 3 to 6)


class Tape:
    def __init__(self, seed: int, cfg: SimConfig, horizon_ms: Optional[int] = None,
                 rate_fn: Optional[Callable[[np.ndarray], np.ndarray]] = None,
                 rate_max_mult: float = 1.0) -> None:
        """rate_fn maps arrival times (ms, ndarray) to a rate multiplier in [0, rate_max_mult]."""
        self.seed = int(seed)
        self.cfg = cfg
        horizon = int(horizon_ms if horizon_ms is not None else cfg.run_length_s * 1000)
        self.horizon_ms = horizon

        rng = np.random.default_rng(np.random.SeedSequence([self.seed, 1]))
        lam_max = cfg.fresh_rate_per_s * rate_max_mult
        n0 = int(rng.poisson(lam_max * horizon / 1000.0))
        times = np.sort(rng.random(n0) * horizon)
        u_thin = rng.random(n0)
        if rate_fn is not None:
            keep = u_thin < (rate_fn(times) / rate_max_mult)
            times = times[keep]
        self.arrivals = times.astype(np.int64)
        n = len(self.arrivals)
        self.n = n

        r = np.random.default_rng(np.random.SeedSequence([self.seed, 2]))
        A = MAX_ATTEMPTS
        self.u_bank = r.random(n)
        self.u_decline = r.random(n)
        self.u_profile = r.random(n)
        self.u_pretry = r.random((n, A))
        self.u_delay = r.random((n, A))
        self.u_honour = r.random((n, A))
        self.u_jit = r.random((n, A))
        self.u_route = r.random((n, A))
        self.u_bh = r.random((n, A))
        self.z_gw = r.standard_normal((n, A))
        self.z_bank = r.standard_normal((n, A))

        shares = np.cumsum(np.asarray(cfg.bank_shares, dtype=float))
        shares[-1] = 1.0 + 1e-12
        self.bank_idx = np.searchsorted(shares, self.u_bank, side="right").astype(np.int64)

    def row(self, i: int) -> dict:
        """Per-payment draws as plain Python lists (fast to index in the event loop)."""
        return {
            "bank": int(self.bank_idx[i]),
            "decline": bool(self.u_decline[i] < self.cfg.hard_decline_rate),
            "u_profile": float(self.u_profile[i]),
            "u_pretry": self.u_pretry[i].tolist(),
            "u_delay": self.u_delay[i].tolist(),
            "u_honour": self.u_honour[i].tolist(),
            "u_jit": self.u_jit[i].tolist(),
            "u_route": self.u_route[i].tolist(),
            "u_bh": self.u_bh[i].tolist(),
            "z_gw": self.z_gw[i].tolist(),
            "z_bank": self.z_bank[i].tolist(),
        }
