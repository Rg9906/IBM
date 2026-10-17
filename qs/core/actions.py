"""Decision handling, action application and rollback proposals (blueprint 15.4, SI-3 and SI-8).

Nothing is applied without a recorded decision. Rollbacks are proposed automatically when their condition holds,
but still need an operator decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from qs.config import PolicyConfig
from qs.schema import Action, ActionId, Decision, DecisionKind, Recommendation

ROLLBACK_ACTIONS = (ActionId.THROTTLE_RETRIES, ActionId.SHED_EXPIRED, ActionId.PRIORITIZE_FRESH,
                    ActionId.VERIFY_PENDING, ActionId.SHIFT_TRAFFIC)


class ApprovalError(ValueError):
    pass


@dataclass
class Applied:
    action: Action
    rec_id: str
    applied_ts: int
    operator: str
    clean_ticks: int = 0
    rolled_back: bool = False


@dataclass
class DecisionRecord:
    rec_id: str
    decision: DecisionKind
    operator: str
    decided_ts: int
    note: str
    applied: list = field(default_factory=list)


class ActionManager:
    """`apply` is a callable taking (Action, rollback: bool) such as Engine.apply_action."""

    def __init__(self, apply: Callable, cfg: Optional[PolicyConfig] = None, store=None) -> None:
        self._apply = apply
        self.cfg = cfg or PolicyConfig()
        self.store = store
        self.recs: dict = {}
        self.log: list = []
        self.applied: list = []
        self.rollback_proposals: dict = {}

    def add(self, rec: Recommendation) -> None:
        self.recs[rec.rec_id] = rec
        if self.store is not None:
            for a in rec.actions:
                self.store.append("recommendations", (rec.rec_id, rec.incident_id, a.id.value, str(a.params),
                                                      str(rec.reasons), rec.confidence, rec.status))

    def pending(self) -> list:
        return [r for r in self.recs.values() if r.status == "pending"]

    def decide(self, rec_id: str, decision: Decision, now_ms: int) -> list:
        rec = self.recs.get(rec_id)
        if rec is None or rec.status != "pending":
            raise ApprovalError(f"{rec_id} is not pending")
        if not decision.operator:
            raise ApprovalError("a decision needs an operator (SI-3)")
        decided = decision.decided_ts if decision.decided_ts is not None else now_ms
        if decision.decision is DecisionKind.REJECT:
            chosen: list = []
        elif decision.decision is DecisionKind.APPROVE_ALL:
            chosen = [a for a in rec.actions if a.eligible]
        else:
            want = set(decision.actions_selected)
            unknown = want - {a.id.value for a in rec.actions}
            if unknown:
                raise ApprovalError(f"actions not in the recommendation: {sorted(unknown)}")
            blocked = [a.id.value for a in rec.actions if a.id.value in want and not a.eligible]
            if blocked:
                raise ApprovalError(f"blocked actions cannot be approved: {blocked}")
            chosen = [a for a in rec.actions if a.id.value in want]
        rec.status = "rejected" if not chosen else "approved"
        out = []
        for a in chosen:
            if a.id in (ActionId.HOLD_MONITOR, ActionId.ESCALATE_HUMAN):
                continue                                   # no simulator control behind these
            self._apply(a, False)
            self.applied.append(Applied(a, rec_id, now_ms, decision.operator))
            out.append(a)
        self.log.append(DecisionRecord(rec_id, decision.decision, decision.operator, decided, decision.note,
                                       [a.id.value for a in out]))
        if self.store is not None:
            self.store.append("decisions", (rec_id, decision.decision.value, decision.operator, decided, decision.note))
            for a in out:
                self.store.append("actions_applied", (rec_id, now_ms, str(a.params), "applied"))
        return out

    # ------------------------------------------------------------------ rollback (SI-8)
    def tick(self, now_ms: int, active_segments: set, pending_back_to_baseline: bool = False) -> list:
        """Count clean ticks for applied actions and propose rollback recommendations when the condition holds."""
        from qs.core.lifecycle import overlaps
        proposals = []
        for ap in self.applied:
            if ap.rolled_back or ap.action.rec_id in self.rollback_proposals:
                continue
            scope = ap.action.scope or ("platform", "*", "*")
            dirty = any(overlaps(scope, s) for s in active_segments)
            ap.clean_ticks = 0 if dirty else ap.clean_ticks + 1
            if ap.action.id is ActionId.VERIFY_PENDING:
                ready = pending_back_to_baseline and not dirty
            else:
                ready = ap.clean_ticks >= self.cfg.rollback_clean_ticks
            if ready:
                key = f"{ap.rec_id}:{ap.action.id.value}"
                if key in self.rollback_proposals:
                    continue
                self.rollback_proposals[key] = ap
                proposals.append(ap)
        return proposals

    def approve_rollback(self, ap: Applied, operator: str, now_ms: int) -> None:
        if not operator:
            raise ApprovalError("rollback needs an operator (SI-3)")
        if ap.rolled_back:
            raise ApprovalError("already rolled back")
        self._apply(ap.action, True)
        ap.rolled_back = True
        if self.store is not None:
            self.store.append("actions_applied", (ap.rec_id, now_ms, str(ap.action.params), "rolled_back"))
