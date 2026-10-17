"""Gateway routing weights with jittered, ramped shifts (blueprint 9.1, 9.4)."""
from __future__ import annotations

from typing import Optional

import numpy as np


class Router:
    def __init__(self, n_gateways: int, seed: int = 0) -> None:
        self.n = n_gateways
        self._w0 = [1.0 / n_gateways] * n_gateways
        self._w1 = list(self._w0)
        self._t0 = 0
        self._ramp = 1
        self._rng = np.random.default_rng(np.random.SeedSequence([seed, 7]))

    def weights(self, t: int) -> list:
        f = min(1.0, max(0.0, (t - self._t0) / self._ramp))
        return [a + (b - a) * f for a, b in zip(self._w0, self._w1)]

    def pick(self, u: float, t: int) -> int:
        acc = 0.0
        w = self.weights(t)
        for i, wi in enumerate(w):
            acc += wi
            if u < acc:
                return i
        return self.n - 1

    def shift(self, from_gw: int, fraction: float, ramp_ms: int, now: int, floor: float = 0.05) -> list:
        """Move up to `fraction` of total traffic away from from_gw, keeping `floor`; others share equally."""
        cur = self.weights(now)
        take = min(fraction, max(0.0, cur[from_gw] - floor))
        new = list(cur)
        new[from_gw] -= take
        others = [i for i in range(self.n) if i != from_gw]
        for i in others:
            new[i] += take / len(others)
        jitter = 1.0 + 0.2 * (float(self._rng.random()) - 0.5)   # +-10 percent on the ramp
        self._w0, self._w1 = cur, new
        self._t0, self._ramp = now, max(1, int(ramp_ms * jitter))
        return new

    def reset(self, now: int, ramp_ms: int = 10000) -> None:
        self._w0 = self.weights(now)
        self._w1 = [1.0 / self.n] * self.n
        self._t0, self._ramp = now, ramp_ms
