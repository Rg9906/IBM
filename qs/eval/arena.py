"""Simulated operator for the policy arena (blueprint 15.6). The full arena (U0-U3 universes) is built in WP-13."""
from __future__ import annotations

from typing import Optional

from qs.config import OperatorConfig
from qs.schema import Decision, DecisionKind, Recommendation


class SimulatedOperator:
    """Approves a recommendation `approval_delay_ms` after first seeing it when confidence >= min_confidence.

    Below the confidence bar it waits one more tick (re-checking confidence on the refreshed recommendation).
    Blocked actions are never approved: approve_selected lists only eligible ones.
    """

    def __init__(self, cfg: Optional[OperatorConfig] = None, name: str = "sim-operator", tick_ms: int = 5000) -> None:
        self.cfg, self.name, self.tick_ms = cfg or OperatorConfig(), name, tick_ms
        self._seen: dict = {}

    def step(self, now_ms: int, pending: list) -> list:
        """Return [(rec_id, Decision)] for recommendations the operator acts on at this tick."""
        out = []
        for rec in pending:
            first = self._seen.setdefault(rec.rec_id, now_ms)
            if now_ms - first < self.cfg.approval_delay_ms:
                continue
            if rec.confidence < self.cfg.min_confidence:
                self._seen[rec.rec_id] = now_ms - self.cfg.approval_delay_ms + self.tick_ms   # wait one more tick
                continue
            ok = [a.id.value for a in rec.actions if a.eligible]
            if not ok:
                out.append((rec.rec_id, Decision(decision=DecisionKind.REJECT, operator=self.name, decided_ts=now_ms,
                                                 note="nothing eligible")))
            else:
                out.append((rec.rec_id, Decision(decision=DecisionKind.APPROVE_SELECTED, operator=self.name,
                                                 actions_selected=ok, decided_ts=now_ms)))
        return out
