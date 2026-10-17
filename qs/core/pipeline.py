"""Pipeline: ingest -> features -> detectors -> forecast -> localizer -> lifecycle -> policy (blueprint 19.4).

The pipeline never touches the simulator. Approved actions leave through pending_actions(); a rollback is an
Action whose params carry "_rollback": True.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from qs.config import PipelineConfig
from qs.core.actions import Applied, ActionManager
from qs.core.cells import CellTracker
from qs.core.detectors.iforest import IForestModel, model_vector
from qs.core.detectors.persistence import QueueShield
from qs.core.detectors.threshold import PlatformThreshold
from qs.core.features import FeatureEngine
from qs.core.governor import UNREACHABLE, VerificationGovernor
from qs.core.ingest import Ingestor
from qs.core.lifecycle import DegradedMonitor, Lifecycle, overlaps
from qs.core.localizer import FaultLocalizer
from qs.core.policy import GatewayInfo, PolicyContext, PolicyEngine, features_hash
from qs.forecast.dataset import EST_SERVICE_S
from qs.forecast.model import CascadeForecaster
from qs.schema import (Action, ActionId, Decision, Event, EventType, ForecastState, IncidentState, IngestResult,
                       Localization, OperatingState, Recommendation, Status, Verdict)

SINGLE = (Verdict.GATEWAY_SIDE, Verdict.BANK_SIDE, Verdict.ROUTE_SPECIFIC)


@dataclass
class TickResult:
    tick_ts: int
    rows: list
    alerts: list = field(default_factory=list)
    incidents: list = field(default_factory=list)
    recs: list = field(default_factory=list)
    rollbacks: list = field(default_factory=list)
    p_stuck: dict = field(default_factory=dict)
    scores: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    degraded: list = field(default_factory=list)
    state: OperatingState = OperatingState.MONITORING
    localization: Optional[Localization] = None
    status_checks: list = field(default_factory=list)       # payment ids the Governor releases now


class Pipeline:
    def __init__(self, cfg: Optional[PipelineConfig] = None, store: Optional[object] = None,
                 models_dir: str = "artifacts/models", detector_mode: Optional[str] = None) -> None:
        self.cfg = cfg or PipelineConfig()
        self.store = store
        tick = self.cfg.tick_ms
        self.fe = FeatureEngine(self.cfg.features, tick_ms=tick)
        self.cells = CellTracker(tick_ms=tick)
        self.ing = Ingestor([self._on_event])
        md = Path(models_dir)
        model = IForestModel.load(str(md / "iforest.pkl")) if (md / "iforest.pkl").exists() else None
        mode = detector_mode or ("RM" if model is not None else "R")
        self.d3 = QueueShield(self.cfg.detectors, model=model if mode != "R" else None, mode=mode)
        self.d0 = PlatformThreshold(self.cfg.detectors)
        self.forecaster = CascadeForecaster(self.cfg.detectors.client_timeout_s)
        if (md / "forecaster" / "forecaster.json").exists():
            self.forecaster.load(str(md / "forecaster"))
        self.localizer = FaultLocalizer()
        self.lifecycle = Lifecycle(self.cfg.lifecycle)
        self.degraded_monitor = DegradedMonitor(self.cfg.lifecycle)
        self.policy = PolicyEngine(self.cfg.policy)
        self.governor = VerificationGovernor(self.cfg.governor)
        self.actions = ActionManager(self._queue_action, self.cfg.policy, store)
        self.model_versions = self._versions(md)
        self._queue: list = []
        self._governed: set = set()
        self._last_event_ts: Optional[int] = None
        self._now = 0
        self._rec_key: dict = {}          # incident id -> (severity, verdict) of the live recommendation
        self._degraded_rec = False
        self.recommendations: dict = {}
        self.last: Optional[TickResult] = None
        self._next_tick = self.cfg.tick_ms

    @staticmethod
    def _versions(md: Path) -> dict:
        try:
            reg = json.loads((md / "registry.json").read_text())
            return {"detector": reg["iforest"]["version"], "forecaster": reg["cascade_forecaster"]["version"]}
        except (OSError, KeyError, ValueError):
            return {"detector": "none", "forecaster": "none"}

    # ------------------------------------------------------------------ event path (hot)
    def _on_event(self, e: Event, late: bool) -> None:
        self.fe.tracker.apply(e, late)
        self.fe.ingest(e, late)
        self.cells.on_event(e, late)
        if self._last_event_ts is None or e.event_timestamp > self._last_event_ts:
            self._last_event_ts = e.event_timestamp
        if self._governed:
            if e.event_type is EventType.ATTEMPT_OUTCOME and e.status is Status.CLIENT_TIMEOUT:
                self.governor.on_timeout(e.payment_id, e.gateway_id, e.event_timestamp)
            elif e.event_type is EventType.STATUS_CHECK_RESULT:
                st = UNREACHABLE if e.error_code == "E_STATUS_UNREACHABLE" else (e.status.value if e.status else "UNKNOWN")
                self.governor.on_status_result(e.payment_id, st, e.event_timestamp)
        if self.store is not None:
            from qs.store.log import log_event
            log_event(self.store, e)

    def ingest(self, events: list) -> IngestResult:
        return self.ing.ingest(events)

    # ------------------------------------------------------------------ actions
    def _queue_action(self, a: Action, rollback: bool) -> None:
        if a.id is ActionId.VERIFY_PENDING:
            scope = tuple(a.scope or ("platform", "*", "*"))
            (self._governed.discard if rollback else self._governed.add)(scope)
        b = a.model_copy(update={"params": {**a.params, "_rollback": True}}) if rollback else a
        self._queue.append(b)

    def pending_actions(self) -> list:
        out, self._queue = self._queue, []
        return out

    def decide(self, rec_id: str, decision: Decision) -> list:
        rec = self.actions.recs[rec_id]
        acts = self.actions.decide(rec_id, decision, self._now)
        if acts and rec.incident_id in self.lifecycle.incidents:
            self.lifecycle.approve(rec.incident_id)
        return acts

    def approve_rollback(self, ap: Applied, operator: str) -> None:
        self.actions.approve_rollback(ap, operator, self._now)

    # ------------------------------------------------------------------ tick
    def _gateway_infos(self, rows: list, p_stuck: dict) -> dict:
        gw_rows = {r.segment[1]: r for r in rows if r.segment[0] == "gateway"}
        total = sum(max(0.0, r.f["arrival_rate"]) for r in gw_rows.values() if r.f["arrival_rate"] == r.f["arrival_rate"]) or 1.0
        live = self.lifecycle._live()
        out = {}
        for g, r in gw_rows.items():
            arr = r.f["arrival_rate"] if r.f["arrival_rate"] == r.f["arrival_rate"] else 0.0
            load = r.f["load_over_capacity"]
            cap = arr / load if load == load and load and load > 0 else 40 / EST_SERVICE_S
            fs = self.forecaster.state(p_stuck.get(r.segment, 0.0))
            out[g] = GatewayInfo(arrival_rate=arr, est_capacity=cap, forecast=fs,
                                 open_incident=any(overlaps(i.segment, r.segment) for i in live),
                                 traffic_share=arr / total)
        return out

    def _bank_healthy_elsewhere(self, gateway: str, bank: str, bad: np.ndarray, exp: np.ndarray) -> bool:
        from qs.core.cells import BI, GI
        g0, b = GI[gateway], BI[bank]
        others = [g for g in range(bad.shape[0]) if g != g0]
        return bool(exp[others, b].sum() > 0 and bad[others, b].sum() <= 1.5 * exp[others, b].sum() + 3)

    def advance(self, watermark_ms: int = 2000) -> list:
        """Run every tick whose watermark has passed, driven by event time. For callers that only ingest."""
        out = []
        while self.ing.max_ts >= self._next_tick + watermark_ms:
            out.append(self.tick(self._next_tick))
        return out

    def tick(self, now_ms: int) -> TickResult:
        self._now = now_ms
        self._next_tick = now_ms + self.cfg.tick_ms
        self.ing.release_until(now_ms - 1)
        rows = self.fe.tick(now_ms)
        res = TickResult(tick_ts=now_ms, rows=rows)
        # 1. forecast and detect
        res.p_stuck = self.forecaster.predict(rows) if self.forecaster.grid is not None else {}
        a3 = self.d3.update(now_ms, rows, res.p_stuck)
        a0 = self.d0.update(now_ms, rows)
        res.alerts, res.scores, res.labels = a3 + a0, dict(self.d3.scores), dict(self.d3.labels)
        # 2. degraded monitoring (never silently healthy)
        vec_ok = {r.segment: model_vector(r, self.cfg.detectors.client_timeout_s) is not None
                  for r in rows if r.eligible} if self.d3.mode != "R" else {}
        res.degraded = self.degraded_monitor.check(now_ms, self._last_event_ts, rows, vec_ok)
        res.state = DegradedMonitor.state(res.degraded)
        # 3. localise and open or merge incidents
        bad, exp = self.cells.matrices(now_ms)
        loc = self.localizer.localize(bad, exp)
        res.localization = loc
        touched = {}
        for al in res.alerts:
            primary = loc.primary if (al.kind != "platform" and loc.verdict in SINGLE and loc.primary
                                      and overlaps(loc.primary, al.segment)) else None
            inc = self.lifecycle.on_alert(al, primary)
            if inc is not None:
                touched[inc.incident_id] = inc
        for inc in self.lifecycle._live():            # forecast escalation: alerts fire once, severity can still rise
            p = max([v for k, v in res.p_stuck.items() if overlaps(inc.segment, k)] or [0.0])
            if inc.severity is not OperatingState.CRITICAL and p >= self.cfg.detectors.forecast_critical:
                inc.severity = OperatingState.CRITICAL
                touched[inc.incident_id] = inc
        live_segs = {i.segment for i in self.lifecycle._live()}
        self.fe.set_frozen(live_segs)
        changed = self.lifecycle.tick(now_ms, set(self.d3.active))
        for i in changed:
            touched[i.incident_id] = i
        res.incidents = list(touched.values())
        # 4. recommendations
        gws = self._gateway_infos(rows, res.p_stuck)
        row_by = {r.segment: r for r in rows}
        for inc in self.lifecycle._live():
            key = (inc.severity, loc.verdict)
            if self._rec_key.get(inc.incident_id) == key:
                continue
            rec = self._recommend(inc, loc, res, gws, row_by, bad, exp, rows)
            self._rec_key[inc.incident_id] = key
            res.recs.append(rec)
        if res.degraded and not self._degraded_rec:
            res.recs.append(self._degraded_recommendation(res, loc, rows))
        self._degraded_rec = bool(res.degraded)
        # 5. rollback proposals, governor releases, baselines, storage
        pend = [r.z["pending_frac"] for r in rows if r.segment[0] == "platform"]
        pend_ok = not pend or not (pend[0] == pend[0]) or pend[0] < 1.0       # pending share near its baseline
        res.rollbacks = self.actions.tick(now_ms, set(self.d3.active), pend_ok)
        if self._governed:
            spare = {g: {"spare_capacity": max(0.0, i.est_capacity - i.arrival_rate)} for g, i in gws.items()}
            res.status_checks = self.governor.due_checks(now_ms, spare)
            for pid in self.governor.reconcile:
                self.fe.tracker.escalate(pid, now_ms)
        self.cells.learn(now_ms, calm=not self.d3.active and not live_segs)
        if self.store is not None:
            from qs.store.log import log_features, log_incident
            log_features(self.store, now_ms, rows, res.scores, res.p_stuck)
            for i in res.incidents:
                log_incident(self.store, i)
            self.store.flush_if_due()
        self.last = res
        return res

    def _recommend(self, inc, loc, res, gws, row_by, bad, exp, rows) -> Recommendation:
        for r in self.actions.pending():
            if r.incident_id == inc.incident_id:
                r.status = "superseded"
        seg_row = row_by.get(inc.segment)
        p = max([res.p_stuck.get(s, 0.0) for s in res.p_stuck if overlaps(inc.segment, s)] or [0.0])
        gname = inc.segment[1] if inc.segment[1] != "*" else None
        healthy = None
        if loc.verdict is Verdict.ROUTE_SPECIFIC and loc.primary:
            healthy = self._bank_healthy_elsewhere(loc.primary[1], loc.primary[2], bad, exp)
        alert = next((a for a in res.alerts if overlaps(inc.segment, a.segment)), None)
        reasons = list(alert.reasons) if alert else []
        conf = alert.confidence if alert else 0.5
        cd = self.forecaster.countdown_s(("gateway", gname, "*")) if gname else None
        ctx = PolicyContext(
            incident_id=inc.incident_id, segment=inc.segment, severity=inc.severity, localization=loc,
            confidence=conf, degraded=bool(res.degraded), p_stuck=p or None, countdown_s=cd, reasons=reasons,
            model_versions=self.model_versions, gateways=gws, bank_healthy_elsewhere=healthy, now_ms=res.tick_ts,
            features_sha256=features_hash([seg_row] if seg_row else rows[:1]))
        rec = self.policy.recommend(ctx)
        self.actions.add(rec)
        self.recommendations[rec.rec_id] = rec
        return rec

    def _degraded_recommendation(self, res, loc, rows) -> Recommendation:
        seg = ("platform", "*", "*")
        ctx = PolicyContext(incident_id="monitoring", segment=seg, severity=OperatingState.DEGRADED_MONITORING,
                            localization=loc, confidence=1.0, degraded=True, reasons=["monitoring impaired: " + ", ".join(res.degraded)],
                            model_versions=self.model_versions, features_sha256=features_hash(rows[:1]), now_ms=res.tick_ts)
        rec = self.policy.recommend(ctx)
        self.actions.add(rec)
        self.recommendations[rec.rec_id] = rec
        return rec
