"""Deterministic reactive discrete-event simulator (blueprint 9).

Plain heapq loop, integer milliseconds, a sequence counter for ties. The engine emits only what
a real platform could observe (qs.schema.Event); commits, true attempt states and duplicate
counts stay inside the engine and are read through ground_truth_stats() by the evaluator.
"""
from __future__ import annotations

import heapq
import math
from collections import deque
from typing import Optional

from qs import ids
from qs.config import SimConfig
from qs.schema import (BANKS, GATEWAYS, Action, ActionId, Event, EventType, Status)
from qs.sim import clients as cl
from qs.sim import status_service
from qs.sim.controls import Controls, Throttle, scope_match
from qs.sim.entities import (ENDED, QUEUED, SERVICE, UNRESOLVED, Attempt, Gateway, Modifier, Payment)
from qs.sim.routing import Router
from qs.sim.tape import Tape

# event kinds (blueprint 9.10)
(ARRIVAL, SERVICE_DONE, CLIENT_TIMEOUT, RETRY_DUE, STATUS_DONE, STATUS_TIMEOUT, SNAPSHOT, KICK,
 REJECT_DONE, RESOLVE, STATUS_REQUEST, STATUS_FAIL) = range(12)


def _lognorm_params(mean: float, cv: float) -> tuple:
    s2 = math.log(1.0 + cv * cv)
    return math.log(mean) - s2 / 2.0, math.sqrt(s2)


class Engine:
    def __init__(self, cfg: SimConfig, tape: Tape, run_id: str = "run",
                 profile_mix: Optional[dict] = None, modifiers: Optional[list] = None,
                 horizon_ms: Optional[int] = None, seed: int = 0,
                 attempt_cap: Optional[int] = None) -> None:
        self.attempt_cap = attempt_cap
        self.cfg = cfg
        self.tape = tape
        self.horizon = int(horizon_ms if horizon_ms is not None else cfg.run_length_s * 1000)
        self.mix = profile_mix or {"naive": 1.0}
        self.modifiers: list = list(modifiers or [])
        self.ids = ids.IdGenerator(run_id)
        self.controls = Controls()
        self.router = Router(cfg.n_gateways, seed)
        self.gws = [Gateway(i, cfg.workers, cfg.queue_max) for i in range(cfg.n_gateways)]
        self.now = 0
        self._heap: list = []
        self._seq = 0
        self._qseq = 0
        self._events: list = []
        self._next_arrival = 0
        self._payments: dict = {}
        self._fresh_log: deque = deque()   # (t, gw_idx, bank_idx) of fresh attempts in last 30 s
        self._gmu = _lognorm_params(cfg.gateway_service_mean_ms, cfg.gateway_service_cv)
        self._bmu = _lognorm_params(cfg.bank_service_mean_ms, cfg.bank_service_cv)
        self._status_cost = cfg.status_check_cost_frac * (cfg.gateway_service_mean_ms + cfg.bank_service_mean_ms)
        self._handlers = [self._h_arrival, self._h_service_done, self._h_client_timeout, self._h_retry_due,
                          self._h_status_done, self._h_status_timeout, self._h_snapshot, self._h_kick,
                          self._h_reject_done, self._h_resolve, self._h_status_request, self._h_status_fail]
        self.stats = {"payments": 0, "attempts": 0, "late_completions": 0, "events": 0}
        if self.tape.n:
            self._push(int(self.tape.arrivals[0]), ARRIVAL, 0)
        self._push(0, SNAPSHOT, None)
        for m in self.modifiers:
            if m.kind == "capacity":
                self._push(m.end_ms, KICK, None)

    # ------------------------------------------------------------------ plumbing
    def _push(self, t: int, kind: int, obj) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (int(t), self._seq, kind, obj))

    def run_until(self, t_end: int) -> None:
        h = self._heap
        handlers = self._handlers
        while h and h[0][0] <= t_end:
            t, _, kind, obj = heapq.heappop(h)
            self.now = t
            handlers[kind](obj)
        self.now = max(self.now, int(t_end))

    def drain(self) -> list:
        ev, self._events = self._events, []
        return ev

    def _emit(self, etype: EventType, p: Optional[Payment], a: Optional[Attempt], gw: int, bank: int,
              *, status: Optional[Status] = None, error: Optional[str] = None, rt: Optional[int] = None,
              k: int = 0, snapshot: bool = True, timeout_ms: Optional[int] = None) -> None:
        g = self.gws[gw]
        retry_count = k
        pid = p.pid if p else "none"
        ev = Event.model_construct(
            schema_version=1, event_id=self.ids.next_event_id(), event_type=etype,
            payment_id=pid, attempt_id=ids.attempt_id(pid, k), attempt_no=k,
            idempotency_key=p.ik if p else None, gateway_id=GATEWAYS[gw], bank_id=BANKS[bank],
            event_timestamp=self.now, received_timestamp=None, response_time_ms=rt, status=status,
            error_code=error, retry_count=retry_count, is_retry=retry_count > 0,
            queue_depth=g.qlen() if snapshot else None,
            active_requests=g.busy if snapshot else None,
            configured_capacity=self._eff_workers(gw) if snapshot else None,
            client_timeout_ms=timeout_ms)
        self._events.append(ev)
        self.stats["events"] += 1

    # ------------------------------------------------------------------ fault multipliers
    def _mult(self, kind: str, gw: Optional[str], bank: Optional[str], t: int) -> float:
        m = 1.0
        for mod in self.modifiers:
            if mod.kind == kind and mod.active(t) and (mod.gateway in (None, gw)) and (mod.bank in (None, bank)):
                m *= mod.value(t)
        return m

    def _eff_workers(self, gw: int) -> int:
        m = self._mult("capacity", GATEWAYS[gw], None, self.now)
        return max(1, int(self.cfg.workers * m))

    def _blackhole(self, bank: str, t: int) -> Optional[Modifier]:
        for mod in self.modifiers:
            if mod.kind == "blackhole" and mod.active(t) and mod.bank == bank:
                return mod
        return None

    # ------------------------------------------------------------------ arrivals and attempts
    def _h_arrival(self, i: int) -> None:
        tp = self.tape
        row = tp.row(i)
        n = self.ids.next_payment_n()
        prof = cl.pick_profile(self.mix, row["u_profile"])
        spec = cl.PROFILES[prof]
        pid = ids.payment_id(n)
        p = Payment(n, pid, row["bank"], prof, row, ids.idempotency_key(n) if spec.uses_ik else None,
                    self.attempt_cap or spec.cap, self.now)
        self._payments[pid] = p
        self.stats["payments"] += 1
        if i + 1 < tp.n:
            self._push(int(tp.arrivals[i + 1]), ARRIVAL, i + 1)
        self._send_attempt(p, 0)

    def _send_attempt(self, p: Payment, k: int) -> None:
        now = self.now
        gwi = self.router.pick(p.row["u_route"][k], now)
        p.last_gw = gwi
        a = Attempt(p, k, gwi, now)
        p.attempts.append(a)
        self.stats["attempts"] += 1
        g = self.gws[gwi]
        T = self.cfg.client_timeout_ms
        if k == 0:
            self._emit(EventType.PAYMENT_INITIATED, p, a, gwi, p.bank, k=0, snapshot=False)
            self._fresh_log.append((now, gwi, p.bank))
        self._emit(EventType.ATTEMPT_SENT, p, a, gwi, p.bank, k=k, timeout_ms=T)
        if k > 0:
            ra = self.controls.check_retry(GATEWAYS[gwi], BANKS[p.bank], now) if self.controls.throttles else None
            if ra is not None:
                a.state = ENDED
                a.client_done = True
                self._push(now + 1, REJECT_DONE, (a, "E_RETRY_LATER", ra))
                return
        if g.qlen() >= g.qmax:
            a.state = ENDED
            a.client_done = True
            self._push(now + self.cfg.overload_reject_ms, REJECT_DONE, (a, "E_OVERLOAD", 0))
            return
        self._enqueue(g, a, fresh=(k == 0))
        self._push(now + T, CLIENT_TIMEOUT, a)
        self._try_start(g)

    def _enqueue(self, g: Gateway, a: Attempt, fresh: bool) -> None:
        g.touch(self.now)
        self._qseq += 1
        a.seq = self._qseq
        a.t_enq = self.now
        a.state = QUEUED
        (g.q_fresh if fresh else g.q_retry).append(a)
        g.entered += 1

    def _pop(self, g: Gateway) -> Optional[Attempt]:
        qf, qr = g.q_fresh, g.q_retry
        if not qf and not qr:
            return None
        if GATEWAYS[g.idx] in self.controls.fresh_first:
            if qr and (self.now - qr[0].t_enq > self.cfg.starvation_guard_ms or not qf):
                return qr.popleft()
            return qf.popleft() if qf else qr.popleft()
        if qf and qr:
            return qf.popleft() if qf[0].seq < qr[0].seq else qr.popleft()
        return qf.popleft() if qf else qr.popleft()

    def _try_start(self, g: Gateway) -> None:
        shed = GATEWAYS[g.idx] in self.controls.shed
        cap = self._eff_workers(g.idx)
        while g.busy < cap:
            a = self._pop(g)
            if a is None:
                return
            g.touch(self.now)
            g.wait_sum += self.now - a.t_enq
            if shed and a.timed_out and not a.is_status:
                a.state = ENDED          # dropped at the front of the queue; no commit
                continue
            self._start_service(g, a)

    def _start_service(self, g: Gateway, a: Attempt) -> None:
        now = self.now
        p = a.pay
        gname, bname = GATEWAYS[g.idx], BANKS[p.bank]
        g.touch(now)
        g.busy += 1
        a.state = SERVICE
        gm = self._mult("gw_service", gname, None, now)
        if a.is_status:
            svc = self._status_cost * gm
            self._push(now + max(1, round(svc)), STATUS_DONE, a)
            return
        zg, zb = p.row["z_gw"][a.k], p.row["z_bank"][a.k]
        gpart = math.exp(self._gmu[0] + self._gmu[1] * zg) * gm
        bpart = math.exp(self._bmu[0] + self._bmu[1] * zb) * self._mult("bank_latency", None, bname, now) \
            * self._mult("route_latency", gname, bname, now)
        bh = self._blackhole(bname, now)
        if bh is not None and p.row["u_bh"][a.k] < bh.v0:
            a.blackholed = True
            a.resolve_ms = bh.resolve_ms
        self._push(now + max(1, round(gpart + bpart)), SERVICE_DONE, a)

    def _release_worker(self, g: Gateway) -> None:
        g.touch(self.now)
        g.busy -= 1

    def _h_kick(self, _obj) -> None:
        for g in self.gws:
            self._try_start(g)

    def _h_service_done(self, a: Attempt) -> None:
        g = self.gws[a.gw]
        self._release_worker(g)
        if a.blackholed:
            a.state = UNRESOLVED
            self._push(max(self.now, a.resolve_ms), RESOLVE, a)
        else:
            self._complete(a)
        self._try_start(g)

    def _h_resolve(self, a: Attempt) -> None:
        a.blackholed = False
        self._complete(a)

    def _complete(self, a: Attempt) -> None:
        """Server-side completion: commit or decline (ground truth), then tell the client if it is still waiting."""
        p = a.pay
        a.state = ENDED
        declined = p.row["decline"]
        if not declined and not (p.ik is not None and p.commits > 0):
            a.committed = True
            p.commits += 1
        if a.timed_out or a.client_done:
            self.stats["late_completions"] += 1
            return
        a.client_done = True
        rt = self.now - a.t_send
        st = Status.DECLINED if declined else Status.SUCCESS
        if a.committed is False and not declined:
            st = Status.SUCCESS   # deduplicated retry reports the original success
        self._emit(EventType.ATTEMPT_OUTCOME, p, a, a.gw, p.bank, status=st, rt=rt, k=a.k,
                   error="E_DECLINED" if declined else None)
        p.done = True

    def _h_reject_done(self, obj) -> None:
        a, code, retry_after = obj
        p = a.pay
        self._emit(EventType.ATTEMPT_OUTCOME, p, a, a.gw, p.bank, status=Status.REJECTED_OVERLOAD,
                   error=code, rt=self.now - a.t_send, k=a.k)
        self._after_failure(p, a.k, timeout=False, retry_after=retry_after)

    def _h_client_timeout(self, a: Attempt) -> None:
        if a.client_done:
            return
        a.timed_out = True
        a.client_done = True
        p = a.pay
        self._emit(EventType.ATTEMPT_OUTCOME, p, a, a.gw, p.bank, status=Status.CLIENT_TIMEOUT,
                   error="E_CLIENT_TIMEOUT", rt=self.cfg.client_timeout_ms, k=a.k)
        self._after_failure(p, a.k, timeout=True, retry_after=0)

    # ------------------------------------------------------------------ client behaviour
    def _after_failure(self, p: Payment, k: int, timeout: bool, retry_after: int) -> None:
        spec = cl.PROFILES[p.profile]
        if p.done:
            return
        if timeout and spec.verify != "none":
            gname, bname = GATEWAYS[p.last_gw], BANKS[p.bank]
            if p.profile == "disciplined" and self.controls.is_governed(gname, bname):
                p.awaiting = "platform"     # the Governor schedules the checks (core side)
                return
            p.awaiting = "verify" if spec.verify == "self_1s" else "self"
            p.checks = 0
            self._push(self.now + cl.check_delay_ms(p.profile, 0, p.row["u_delay"][k]), STATUS_REQUEST, p)
            return
        self._decide_retry(p, k, retry_after)

    def _decide_retry(self, p: Payment, k: int, retry_after: int = 0) -> None:
        if k + 1 >= p.cap or p.row["u_pretry"][k] >= self.cfg.p_retry:
            p.abandoned = True
            return
        if retry_after and p.row["u_honour"][k] < self.cfg.retry_throttle_honour_frac:
            delay = retry_after + int(p.row["u_jit"][k] * (8000 - retry_after)) if retry_after < 8000 else retry_after
        else:
            delay = cl.retry_delay_ms(p.profile, k, p.row["u_delay"][k])
        self._emit(EventType.RETRY_SCHEDULED, p, None, p.last_gw, p.bank, k=k + 1, snapshot=False)
        self._push(self.now + delay, RETRY_DUE, (p, k + 1))

    def _h_retry_due(self, obj) -> None:
        p, k = obj
        if not p.done:
            self._send_attempt(p, k)

    # ------------------------------------------------------------------ status service
    def request_status_check(self, payment_id: str) -> bool:
        """Platform-side status inquiry (used by the Governor through the live runner)."""
        p = self._payments.get(payment_id)
        if p is None or p.done:
            return False
        self._issue_status(p)
        return True

    def _h_status_request(self, p: Payment) -> None:
        if p.done or p.awaiting is None:
            return
        self._issue_status(p)

    def _issue_status(self, p: Payment) -> None:
        now = self.now
        last = p.attempts[-1]
        g = self.gws[last.gw]
        s = Attempt(p, last.k, last.gw, now, is_status=True)
        p.checks += 1
        self._emit(EventType.STATUS_CHECK_SENT, p, s, last.gw, p.bank, k=last.k)
        if g.qlen() >= g.qmax:
            self._push(now + self.cfg.overload_reject_ms, STATUS_FAIL, s)
            return
        self._enqueue(g, s, fresh=True)
        self._push(now + self.cfg.client_timeout_ms, STATUS_TIMEOUT, s)
        self._try_start(g)

    def _h_status_done(self, s: Attempt) -> None:
        g = self.gws[s.gw]
        self._release_worker(g)
        s.state = ENDED
        if not s.timed_out:
            s.client_done = True
            self._status_result(s, status_service.answer(s.pay), None)
        self._try_start(g)

    def _h_status_timeout(self, s: Attempt) -> None:
        if s.client_done:
            return
        s.timed_out = True
        s.client_done = True
        self._status_result(s, Status.UNKNOWN, "E_STATUS_UNREACHABLE")

    def _h_status_fail(self, s: Attempt) -> None:
        s.client_done = True
        self._status_result(s, Status.UNKNOWN, "E_STATUS_UNREACHABLE")

    def _status_result(self, s: Attempt, res: Status, err: Optional[str]) -> None:
        p = s.pay
        self._emit(EventType.STATUS_CHECK_RESULT, p, s, s.gw, p.bank, status=res, error=err,
                   rt=self.now - s.t_send, k=s.k)
        if p.awaiting is None or p.done:
            return
        if res is Status.CONFIRMED_SUCCESS:
            p.done = True
            p.awaiting = None
        elif res is Status.CONFIRMED_FAILED:
            p.awaiting = None
            self._decide_retry(p, p.attempts[-1].k)
        elif p.awaiting in ("verify", "self"):
            spec = cl.PROFILES[p.profile]
            if p.checks >= spec.max_checks:
                p.abandoned = True
                p.awaiting = None
            else:
                self._push(self.now + cl.check_delay_ms(p.profile, p.checks, p.row["u_delay"][p.attempts[-1].k]),
                           STATUS_REQUEST, p)
        # awaiting == "platform": the Governor decides when to check again

    # ------------------------------------------------------------------ snapshots and actions
    def _h_snapshot(self, _obj) -> None:
        for g in self.gws:
            self._emit(EventType.SERVICE_SNAPSHOT, None, None, g.idx, 0, k=0)
        nxt = self.now + self.cfg.snapshot_interval_ms
        if nxt <= self.horizon:
            self._push(nxt, SNAPSHOT, None)
        cutoff = self.now - 30000
        fl = self._fresh_log
        while fl and fl[0][0] < cutoff:
            fl.popleft()

    def fresh_rate(self, scope: tuple) -> float:
        """Observed fresh attempts per second in the last 30 s for a segment scope."""
        n = sum(1 for (_, gi, bi) in self._fresh_log if scope_match(scope, GATEWAYS[gi], BANKS[bi]))
        return n / 30.0

    def apply_action(self, action: Action, rollback: bool = False) -> None:
        """Apply (or roll back) an approved action at the current simulator time (blueprint 9.4)."""
        scope = tuple(action.scope) if action.scope else ("platform", "*", "*")
        gws = [g for g in GATEWAYS if scope[1] in ("*", g)]
        c = self.controls
        aid = action.id
        if aid is ActionId.THROTTLE_RETRIES:
            if rollback:
                c.throttles.pop(scope, None)
            else:
                frac = float(action.params.get("rate_fraction", 0.10))
                c.throttles[scope] = Throttle(scope, max(0.1, frac * self.fresh_rate(scope)),
                                              int(action.params.get("retry_after_ms", 2000)), self.now)
        elif aid is ActionId.SHED_EXPIRED:
            (c.shed.difference_update if rollback else c.shed.update)(gws)
        elif aid is ActionId.PRIORITIZE_FRESH:
            (c.fresh_first.difference_update if rollback else c.fresh_first.update)(gws)
        elif aid is ActionId.VERIFY_PENDING:
            (c.governed.discard if rollback else c.governed.add)(scope)
        elif aid is ActionId.SHIFT_TRAFFIC:
            if rollback:
                self.router.reset(self.now)
            else:
                src = GATEWAYS.index(str(action.params["from_gateway"]))
                self.router.shift(src, float(action.params.get("fraction", 0.25)),
                                  int(action.params.get("ramp_ms", 10000)), self.now)
        else:
            return
        pid = "none"
        gw_i = GATEWAYS.index(gws[0]) if gws else 0
        ev = Event.model_construct(
            schema_version=1, event_id=self.ids.next_event_id(), event_type=EventType.ACTION_APPLIED,
            payment_id=pid, attempt_id=ids.attempt_id(pid, 0), attempt_no=0, idempotency_key=None,
            gateway_id=GATEWAYS[gw_i], bank_id=scope[2] if scope[2] != "*" else BANKS[0],
            event_timestamp=self.now, received_timestamp=None, response_time_ms=None, status=None,
            error_code=("ROLLBACK:" if rollback else "") + aid.value, retry_count=0, is_retry=False,
            queue_depth=None, active_requests=None, configured_capacity=None, client_timeout_ms=None)
        self._events.append(ev)
        self.stats["events"] += 1
        for g in self.gws:
            self._try_start(g)

    # ------------------------------------------------------------------ ground truth (evaluator only)
    def finalize(self) -> None:
        """Close time integrals at the current time and add the waits of still-queued items."""
        for g in self.gws:
            g.touch(self.now)

    def ground_truth_stats(self) -> dict:
        """Evaluator-only: commits, duplicates, utilisation, Little's law inputs. Never emitted as events."""
        self.finalize()
        T = max(1, self.now)
        pays = self._payments.values()
        dup = sum(p.commits - 1 for p in pays if p.commits > 1)
        out = {"payments": len(self._payments), "committed_payments": sum(1 for p in pays if p.commits > 0),
               "duplicate_commits": dup, "late_completions": self.stats["late_completions"], "per_gateway": []}
        for g in self.gws:
            waiting_now = sum(self.now - a.t_enq for a in list(g.q_fresh) + list(g.q_retry))
            out["per_gateway"].append({
                "utilisation": g.busy_int / (self.cfg.workers * T),
                "mean_queue": g.q_int / T,
                "arrival_rate": g.entered / (T / 1000.0),
                "mean_wait_ms": (g.wait_sum + waiting_now) / max(1, g.entered)})
        return out

    def attempt_outcome_counts(self) -> dict:
        """Evaluator-only: attempts by terminal state for the conservation test."""
        c = {"total": 0, "ended": 0, "open": 0}
        for p in self._payments.values():
            for a in p.attempts:
                c["total"] += 1
                c["ended" if a.state == ENDED else "open"] += 1
        return c
