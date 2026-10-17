"""Verification Governor (blueprint 12): budgeted status checks and retry gating.

Randomness (jitter) comes from an explicitly seeded numpy Generator. Presets in configs/rails are analogies,
not rail implementations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from qs.config import GovernorConfig
from qs.schema import PaymentState as PS

UNREACHABLE = "UNREACHABLE"      # status path failed (E_STATUS_UNREACHABLE or timeout on the status call)


@dataclass
class _P:
    gateway: str
    timeout_ts: int
    next_ts: int
    state: PS = PS.PENDING_UNKNOWN
    checks: int = 0


class VerificationGovernor:
    def __init__(self, cfg: Optional[GovernorConfig] = None, seed: int = 0) -> None:
        self.cfg = cfg or GovernorConfig()
        self._rng = np.random.default_rng(seed)
        self._p: dict = {}
        self._tokens: dict = {}
        self._last_refill: dict = {}
        self._breaker_until: dict = {}
        self.reconcile: list = []          # payment ids on the operator's reconciliation list, in escalation order
        self.sent_log: list = []           # (now_ms, gateway) per released check, for rate assertions

    def _jit(self, delay_ms: float) -> int:
        j = self.cfg.jitter
        return int(delay_ms * (1.0 + self._rng.uniform(-j, j)))

    def on_timeout(self, payment_id: str, gateway_id: str, now_ms: int) -> None:
        if payment_id in self._p:
            return
        self._p[payment_id] = _P(gateway_id, now_ms, now_ms + self._jit(self.cfg.first_check_delay_ms))

    def _escalate(self, pid: str, p: _P) -> None:
        p.state = PS.ESCALATED_RECONCILE
        if pid not in self.reconcile:
            self.reconcile.append(pid)

    def _reschedule(self, pid: str, p: _P, now_ms: int) -> None:
        """Called after a check that did not resolve or could not be sent; it already counts against the cap."""
        if p.checks >= self.cfg.max_checks_per_payment:
            self._escalate(pid, p)
            return
        gap = self.cfg.first_check_delay_ms * self.cfg.check_backoff_factor ** max(0, p.checks - 1)
        p.state, p.next_ts = PS.PENDING_UNKNOWN, now_ms + self._jit(gap)

    def on_status_result(self, payment_id: str, status: str, now_ms: int) -> None:
        p = self._p.get(payment_id)
        if p is None or p.state != PS.STATUS_CHECKING:
            return
        if status == PS.CONFIRMED_SUCCESS.value:
            p.state = PS.CONFIRMED_SUCCESS
        elif status == PS.CONFIRMED_FAILED.value:
            p.state = PS.CONFIRMED_FAILED
        else:
            if status == UNREACHABLE:
                self._breaker_until[p.gateway] = now_ms + self.cfg.breaker_open_ms
            self._reschedule(payment_id, p, now_ms)

    def _bucket(self, gw: str, now_ms: int, state: dict) -> float:
        spare = float(state.get(gw, {}).get("spare_capacity", self.cfg.segment_budget_max_per_s * 10))
        rate = min(self.cfg.segment_budget_max_per_s,
                   max(self.cfg.segment_budget_min_per_s, self.cfg.segment_budget_fraction * spare))
        burst = rate * self.cfg.segment_budget_burst_s
        last = self._last_refill.get(gw, now_ms)
        tok = min(burst, self._tokens.get(gw, burst) + rate * max(0, now_ms - last) / 1000.0)
        self._last_refill[gw] = now_ms
        self._tokens[gw] = tok
        return tok

    def due_checks(self, now_ms: int, gateway_state: dict) -> list:
        """Payment ids to check now. Releases at most the bucket allowance per gateway, oldest first."""
        by_gw: dict = {}
        for pid, p in self._p.items():
            if p.state != PS.PENDING_UNKNOWN or p.next_ts > now_ms:
                continue
            if now_ms - p.timeout_ts > self.cfg.horizon_ms:      # horizon passed without a resolving answer
                self._escalate(pid, p)
                continue
            by_gw.setdefault(p.gateway, []).append((p.next_ts, pid))
        out = []
        for gw, items in by_gw.items():
            tok = self._bucket(gw, now_ms, gateway_state)
            items.sort()
            if now_ms < self._breaker_until.get(gw, 0):
                for _, pid in items:                 # cannot send: the attempt still counts against the cap
                    p = self._p[pid]
                    p.checks += 1
                    self._reschedule(pid, p, now_ms)
                continue
            for _, pid in items:
                if tok < 1.0:
                    break
                tok -= 1.0
                p = self._p[pid]
                p.checks += 1
                p.state = PS.STATUS_CHECKING
                out.append(pid)
                self.sent_log.append((now_ms, gw))
            self._tokens[gw] = tok
        return out

    def can_retry(self, payment_id: str) -> bool:
        p = self._p.get(payment_id)
        if p is None:
            return True                               # never timed out: not governed
        return p.state == PS.CONFIRMED_FAILED         # R1, R3, R4: everything else blocks

    def state_of(self, payment_id: str) -> Optional[PS]:
        p = self._p.get(payment_id)
        return p.state if p else None

    def checks_of(self, payment_id: str) -> int:
        return self._p[payment_id].checks

    def stats(self) -> dict:
        n: dict = {}
        for p in self._p.values():
            n[p.state.value] = n.get(p.state.value, 0) + 1
        return {"states": n, "checks_sent": len(self.sent_log), "escalated": len(self.reconcile),
                "bucket_tokens": dict(self._tokens), "breakers": dict(self._breaker_until)}
