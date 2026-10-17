"""Fault Localizer (blueprint 14): attribute excess bad outcomes in the gateway x bank grid.

Input matrices have shape (n_gateways, n_banks). `bad` and `expected` are counts over the short window, so they
add across cells. Nothing here reads ground truth.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from qs.core.windows import BANKS, GATEWAYS
from qs.schema import ActionId, Localization, Verdict

MIN_EXCESS = 20.0
CONC = 0.67              # share needed to call a dimension concentrated
OTHER_MAX = 0.50         # the other dimension must stay below this for a single-sided verdict
CAND_MIN = 0.10
CELL_MIN = 0.50
PLATFORM_SURPRISE_MAX = 0.05
HYSTERESIS_TICKS = 2


def _jsd2(p: float, q: float) -> float:
    """Jensen-Shannon divergence (base 2) between the two-point distributions (p, 1-p) and (q, 1-q)."""
    def h(x: np.ndarray) -> float:
        x = x[x > 0]
        return float(-(x * np.log2(x)).sum())
    a, b = np.array([p, 1 - p]), np.array([q, 1 - q])
    return max(0.0, h((a + b) / 2) - 0.5 * h(a) - 0.5 * h(b))


def _candidates(share: np.ndarray) -> list:
    order = np.argsort(-share)
    out, cum = [], 0.0
    for i in order:
        if share[i] < CAND_MIN:
            break
        out.append(int(i))
        cum += share[i]
        if cum >= CONC:
            break
    return out


class FaultLocalizer:
    def __init__(self, gateways=GATEWAYS, banks=BANKS, hysteresis: int = HYSTERESIS_TICKS) -> None:
        self.gateways, self.banks, self.hysteresis = list(gateways), list(banks), hysteresis
        self._published: Optional[Localization] = None
        self._pending: Optional[Verdict] = None
        self._pending_n = 0

    def _surprise(self, bad: np.ndarray, exp: np.ndarray) -> np.ndarray:
        tb, te = bad.sum(), exp.sum()
        if tb <= 0 or te <= 0:
            return np.zeros_like(bad, dtype=float)
        return np.array([_jsd2(b / tb, e / te) for b, e in zip(bad, exp)])

    def raw(self, cell_bad: np.ndarray, cell_expected: np.ndarray) -> Localization:
        """Stateless verdict for one tick (steps 1 to 4 and 6 of 14.3, rules of 14.4)."""
        bad, exp = np.asarray(cell_bad, float), np.asarray(cell_expected, float)
        excess = np.maximum(0.0, bad - exp)
        total = float(excess.sum())
        if total < MIN_EXCESS:
            return Localization(verdict=Verdict.INSUFFICIENT_EVIDENCE, total_excess=total)
        ep_g, ep_b = excess.sum(axis=1) / total, excess.sum(axis=0) / total
        sur_g, sur_b = self._surprise(bad.sum(axis=1), exp.sum(axis=1)), self._surprise(bad.sum(axis=0), exp.sum(axis=0))
        cand_g, cand_b = _candidates(ep_g), _candidates(ep_b)
        gi, bi = int(ep_g.argmax()), int(ep_b.argmax())
        cell_share = float(excess[gi, bi] / total)
        g_conc, b_conc = ep_g[gi] >= CONC, ep_b[bi] >= CONC
        gseg = ("gateway", self.gateways[gi], "*")
        bseg = ("bank", "*", self.banks[bi])
        if g_conc and b_conc and cell_share >= CELL_MIN:
            verdict, primary, top = Verdict.ROUTE_SPECIFIC, ("route", self.gateways[gi], self.banks[bi]), cell_share
        elif g_conc and not b_conc and ep_b.max() <= OTHER_MAX:
            verdict, primary, top = Verdict.GATEWAY_SIDE, gseg, float(ep_g[gi])
        elif b_conc and not g_conc and ep_g.max() <= OTHER_MAX:
            verdict, primary, top = Verdict.BANK_SIDE, bseg, float(ep_b[bi])
        elif sur_g.sum() < PLATFORM_SURPRISE_MAX and sur_b.sum() < PLATFORM_SURPRISE_MAX and not (g_conc or b_conc):
            verdict, primary, top = Verdict.PLATFORM_WIDE, None, 0.0
        else:
            verdict, primary, top = Verdict.MULTI_FAULT, None, float(max(ep_g.max(), ep_b.max()))
        cands = [{"segment": ("gateway", self.gateways[i], "*"), "share": float(ep_g[i]), "surprise": float(sur_g[i])}
                 for i in range(len(self.gateways))]
        cands += [{"segment": ("bank", "*", self.banks[j]), "share": float(ep_b[j]), "surprise": float(sur_b[j])}
                  for j in range(len(self.banks))]
        cands.sort(key=lambda c: -c["share"])
        if verdict is Verdict.ROUTE_SPECIFIC:
            cands.insert(0, {"segment": primary, "share": cell_share, "surprise": float(sur_g[gi] + sur_b[bi])})
        return Localization(verdict=verdict, primary=primary, candidates=cands,
                            confidence=float(top * min(1.0, total / 100.0)), total_excess=total)

    def localize(self, cell_bad: np.ndarray, cell_expected: np.ndarray) -> Localization:
        """Published verdict with hysteresis: a new verdict must repeat for `hysteresis` consecutive ticks.

        INSUFFICIENT_EVIDENCE is published at once, since a stale accusation is worse than none.
        """
        r = self.raw(cell_bad, cell_expected)
        pub = self._published
        if pub is None or r.verdict is Verdict.INSUFFICIENT_EVIDENCE or r.verdict == pub.verdict:
            self._published, self._pending, self._pending_n = r, None, 0
            return r
        if self._pending == r.verdict:
            self._pending_n += 1
        else:
            self._pending, self._pending_n = r.verdict, 1
        if self._pending_n >= self.hysteresis:
            self._published, self._pending, self._pending_n = r, None, 0
            return r
        # keep the old verdict and primary, but refresh the numbers from this tick
        held = r.model_copy(update={"verdict": pub.verdict, "primary": pub.primary})
        self._published = held
        return held


# 14.5: action eligibility by verdict. "conditional:..." means the policy engine must check the stated condition.
_E, _B, _N = "eligible", "blocked", "not_recommended"


def eligibility(verdict: Verdict) -> dict:
    v = verdict
    thr_shed_fresh = _N if v is Verdict.INSUFFICIENT_EVIDENCE else _E
    if v is Verdict.GATEWAY_SIDE:
        shift = "conditional:spare_capacity_and_ramp"
    elif v is Verdict.ROUTE_SPECIFIC:
        shift = "conditional:bank_healthy_on_other_gateways"
    else:
        shift = _B
    return {ActionId.THROTTLE_RETRIES: thr_shed_fresh, ActionId.SHED_EXPIRED: thr_shed_fresh,
            ActionId.PRIORITIZE_FRESH: thr_shed_fresh, ActionId.VERIFY_PENDING: _E, ActionId.SHIFT_TRAFFIC: shift,
            ActionId.HOLD_MONITOR: _E, ActionId.ESCALATE_HUMAN: _E}
